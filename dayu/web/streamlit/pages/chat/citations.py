"""聊天引用标记的严格解析与安全 Markdown 链接转换。

仅消费工具给出的机器标识，不从自然语言标题推测文档或章节；本模块不依赖
Streamlit，也不读取财报。调用方在渲染边界提供当前查询参数。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import quote, unquote, urlencode

_COMPONENT_MAX_LENGTH = 256
# 一个 Unicode 字符最多占四个 UTF-8 字节，每个字节转义后占三个字符。
_ENCODED_COMPONENT_MAX_LENGTH = _COMPONENT_MAX_LENGTH * 4 * 3
_MARKER_PREFIX = "[[source:"
_MARKER_SUFFIX = "]]"
_COMPONENT_SEPARATOR = "|"
_DOCUMENT_COMPONENT_COUNT = 2
_SECTION_COMPONENT_COUNT = 3
_MARKER_MAX_LENGTH = (
    len(_MARKER_PREFIX) + len(_MARKER_SUFFIX)
    + _SECTION_COMPONENT_COUNT * _ENCODED_COMPONENT_MAX_LENGTH
    + _SECTION_COMPONENT_COUNT - 1
)
_SOURCE_QUERY_PREFIX = "source_"
_SOURCE_TICKER_KEY = "source_ticker"
_SOURCE_DOCUMENT_KEY = "source_document"
_SOURCE_REF_KEY = "source_ref"
_DOCUMENT_LABEL = "文档来源"
_SECTION_LABEL = "章节来源"
_IDENTIFIER_PUNCTUATION = frozenset("._-")
_FENCE_LINE = re.compile(
    r"^(?:[ \t]*>[ \t]?)*[ \t]*(?P<list_prefix>(?:(?:[-+*]|[0-9]+[.)])[ \t]+)?)"
    r"(?P<fence>`{3,}|~{3,})(?P<rest>[^\r\n]*)(?:\r?\n)?$"
)
_TEXT_TOKEN = re.compile(r"`+|\[\[source:[^\[\]\r\n]*\]\]")


@dataclass(frozen=True)
class SourceCitation:
    """已验证的来源身份；章节引用保留工具给出的原始 ref。"""

    ticker: str
    document_id: str
    ref: str | None = None


def _decode_component(encoded: str) -> str | None:
    """解码并验证单个引用分量。

    参数:
        encoded: 使用 ``quote(value, safe='')`` 编码的机器标识。
    返回值:
        合法的原始标识；非规范编码、危险字符或长度越界时返回空值。
    异常:
        无；非法 UTF-8 会被拒绝。
    """
    if not encoded or len(encoded) > _ENCODED_COMPONENT_MAX_LENGTH:
        return None
    try:
        decoded = unquote(encoded, encoding="utf-8", errors="strict")
    except UnicodeError:
        return None
    if not decoded or len(decoded) > _COMPONENT_MAX_LENGTH:
        return None
    # 使用正向字符白名单，拒绝路径分隔符、百分号二次解码、URL 与 Markdown 注入。
    if not decoded[0].isalnum() or ".." in decoded:
        return None
    if any(not char.isalnum() and char not in _IDENTIFIER_PUNCTUATION for char in decoded):
        return None
    if quote(decoded, safe="") != encoded:
        return None
    return decoded


def parse_source_marker(marker: str) -> SourceCitation | None:
    """严格解析完整文档级或章节级来源标记。

    参数:
        marker: ``[[source:TICKER|DOCUMENT_ID]]`` 或带第三个 ``REF`` 分量的标记。
            各分量必须按 ``quote(..., safe='')`` 规范编码，解码后最长 256 字符。
    返回值:
        已验证的来源身份；空分量、非法编码、路径穿越、注入或未完成标记返回空值。
    异常:
        无；不合法的文本保留给调用方原样展示。
    """
    if len(marker) > _MARKER_MAX_LENGTH:
        return None
    if not marker.startswith(_MARKER_PREFIX) or not marker.endswith(_MARKER_SUFFIX):
        return None
    components = marker[len(_MARKER_PREFIX):-len(_MARKER_SUFFIX)].split(_COMPONENT_SEPARATOR)
    if len(components) not in (_DOCUMENT_COMPONENT_COUNT, _SECTION_COMPONENT_COUNT):
        return None
    decoded: list[str] = []
    for component in components:
        value = _decode_component(component)
        if value is None:
            return None
        decoded.append(value)
    ref = decoded[2] if len(decoded) == _SECTION_COMPONENT_COUNT else None
    return SourceCitation(ticker=decoded[0], document_id=decoded[1], ref=ref)


def _citation_link(citation: SourceCitation, query_params: Mapping[str, str]) -> str:
    """生成相对当前应用的引用链接并保留非来源查询参数。

    参数:
        citation: 已通过严格解析的来源身份。
        query_params: 渲染边界取得的当前查询参数，包含聊天会话身份。
    返回值:
        固定中文标签的 Markdown 链接；旧的 ``source_`` 参数全部替换。
    异常:
        无；调用方传入符合类型约定的 Unicode 查询参数。
    """
    params = {key: value for key, value in query_params.items() if not key.startswith(_SOURCE_QUERY_PREFIX)}
    params[_SOURCE_TICKER_KEY] = citation.ticker
    params[_SOURCE_DOCUMENT_KEY] = citation.document_id
    if citation.ref is not None:
        params[_SOURCE_REF_KEY] = citation.ref
    label = _SECTION_LABEL if citation.ref is not None else _DOCUMENT_LABEL
    # query-only URL 留在当前应用路径；所有参数都重新编码，不能插入括号或 HTML。
    query = urlencode(params, quote_via=quote, safe="")
    return f"[{label}](?{query})"


def _convert_prose_line(line: str, inline_ticks: int, query_params: Mapping[str, str]) -> tuple[str, int]:
    """转换一行正文中的标记，并跨行保存行内代码边界。

    参数:
        line: 含原始换行符的一行正文。
        inline_ticks: 当前行内代码的反引号数量，零表示不在代码中。
        query_params: 当前应用查询参数。
    返回值:
        转换后的文本和更新后的行内代码边界；未闭合代码保守地不转换。
    异常:
        无。
    """
    parts: list[str] = []
    previous_end = 0
    for match in _TEXT_TOKEN.finditer(line):
        parts.append(line[previous_end:match.start()])
        token = match.group()
        replacement = token
        backslash_start = match.start()
        while backslash_start > 0 and line[backslash_start - 1] == "\\":
            backslash_start -= 1
        escaped = (match.start() - backslash_start) % 2 != 0
        # 反斜杠仅在代码外转义 Markdown；代码内的反斜杠不妨碍反引号闭合。
        if inline_ticks == 0 and escaped:
            if token.startswith("`") and len(token) > 1:
                inline_ticks = len(token) - 1
            parts.append(token)
            previous_end = match.end()
            continue
        if token.startswith("`"):
            if inline_ticks == 0:
                inline_ticks = len(token)
            elif inline_ticks == len(token):
                inline_ticks = 0
        elif inline_ticks == 0 and token.startswith(_MARKER_PREFIX):
            citation = parse_source_marker(token)
            if citation is not None:
                replacement = _citation_link(citation, query_params)
        parts.append(replacement)
        previous_end = match.end()
    parts.append(line[previous_end:])
    return "".join(parts), inline_ticks


def render_citation_links(text: str, query_params: Mapping[str, str]) -> str:
    """将助手正文中完整有效的来源标记转为 Markdown 链接。

    参数:
        text: 历史正文或当前流式累计全文，而非孤立的增量 chunk。
        query_params: 由渲染边界 ``st.query_params.to_dict()`` 提供的查询参数。
    返回值:
        转换后的正文；重复引用各自链接，非法或未完成标记保持原样。
        反引号行内代码、反引号或波浪号围栏代码（含未闭合围栏）不转换。
    异常:
        无；不修改输入参数，不读取或写入会话状态。
    """
    lines: list[str] = []
    fence_char = ""
    fence_length = 0
    inline_ticks = 0
    for line in text.splitlines(keepends=True):
        fence_match = _FENCE_LINE.fullmatch(line)
        if fence_char:
            lines.append(line)
            if fence_match is not None:
                fence = fence_match.group("fence")
                if (
                    fence[0] == fence_char
                    and len(fence) >= fence_length
                    and not fence_match.group("rest").strip()
                    and not fence_match.group("list_prefix")
                ):
                    fence_char = ""
                    fence_length = 0
            continue
        if fence_match is not None and inline_ticks == 0:
            fence = fence_match.group("fence")
            # CommonMark 不允许反引号围栏的信息字符串含反引号。
            if fence[0] != "`" or "`" not in fence_match.group("rest"):
                fence_char = fence[0]
                fence_length = len(fence)
                lines.append(line)
                continue
        converted, inline_ticks = _convert_prose_line(line, inline_ticks, query_params)
        lines.append(converted)
    return "".join(lines)
