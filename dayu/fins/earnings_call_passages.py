"""从电话会原文保守派生发言片段，不进行存储或上层调用。

仅完整独立身份行可建立角色：姓名与职位用破折号分隔，或独立职位行。
识别 Markdown 标题、引用、列表和强调包装；证据保留原始行（不含换行符）。
未识别的显式标记及行内冒号标签会切断身份继承，不依据姓名、机构或 Q&A 章节猜测角色。
"""

from __future__ import annotations

# pyright: strict

from dataclasses import dataclass
import re
from typing import Literal

_Role = Literal["analyst", "management", "operator", "unknown"]
_Kind = Literal["question", "statement", "unknown"]

_ROLE_LABELS: dict[str, _Role] = {
    "analyst": "analyst",
    "financial analyst": "analyst",
    "equity analyst": "analyst",
    "分析师": "analyst",
    "management": "management",
    "chief executive officer": "management",
    "chief financial officer": "management",
    "ceo": "management",
    "cfo": "management",
    "管理层": "management",
    "首席执行官": "management",
    "首席财务官": "management",
    "operator": "operator",
    "moderator": "operator",
    "主持人": "operator",
}
_MARKDOWN_PREFIX = re.compile(r"^(?:>\s*|#{1,6}\s+|[-+*]\s+|\d+[.)]\s+)")
_MARKDOWN_HEADING = re.compile(r"^#{1,6}\s+")
_MARKDOWN_CLOSING_HEADING = re.compile(r"\s+#+$")
_SPEAKER_SEPARATOR = re.compile(r"\s+[-–—]\s+|\s*[–—]\s*")
_ROLE_SEPARATOR = re.compile(r"\s*(?:/|&|、|\band\b)\s*", re.IGNORECASE)
_MARKDOWN_EMPHASIS = re.compile(r"(?<!\w)(\*\*|__|\*|_)(?=\S)(.+?)(?<=\S)\1(?!\w)")
_LINE_ENDINGS = "\r\n\v\f\x1c\x1d\x1e\x85\u2028\u2029"
_QUESTION_MARKS = ("?", "？")
_STATEMENT_ENDINGS = (".", "。", "!", "！")
_CLOSING_QUOTES = "\"'”’」』）)]"
_LABEL_COLONS = ":："
# 仅接受文字及常见姓名分隔符组成的标签；排除数字时间和冒号后紧接双斜线的 URL。
_INLINE_LABEL = re.compile(r"^[^\W\d_](?:[^\W\d_]|[ \t.'’&()（）/–—-])*[:：](?!//)\s*\S")


@dataclass(frozen=True)
class EarningsCallPassage:
    """原文中的不可变连续片段；范围为 Python 字符索引的左闭右开区间。

    ``text`` 始终等于原文 ``content[start:end]``，并包含身份行及原换行。
    ``speaker`` 仅保留显式标记中的姓名，不查名册、不推测身份。
    ``role`` 与 ``kind`` 独立；缺少相应证据时为 ``unknown``。
    ``evidence`` 为建立角色的准确原始身份行（不含行结束符），否则为空。
    """

    start: int
    end: int
    text: str
    speaker: str | None
    role: Literal["analyst", "management", "operator", "unknown"]
    kind: Literal["question", "statement", "unknown"]
    evidence: str | None


@dataclass(frozen=True)
class _Identity:
    """单个显式标记的临时身份状态，不跨未识别标记传播。"""

    speaker: str | None
    role: _Role
    evidence: str | None
    has_inline_body: bool = False


_UNKNOWN_IDENTITY = _Identity(speaker=None, role="unknown", evidence=None)


def _normalize_marker(line: str) -> tuple[str, bool]:
    """去除识别用 Markdown 包装，不修改原始证据。

    Args:
        line: 不含行结束符的原始行。
    Returns:
        规范化标记与是否为明确标题或整行强调标记。
    Raises:
        无。
    """
    marker = line.strip()
    explicit = False
    while prefix := _MARKDOWN_PREFIX.match(marker):
        explicit = explicit or _MARKDOWN_HEADING.match(marker) is not None
        marker = marker[prefix.end() :].strip()
    marker = _MARKDOWN_CLOSING_HEADING.sub("", marker)
    explicit = explicit or _MARKDOWN_EMPHASIS.fullmatch(marker) is not None
    # 只移除成对包装，残缺标记与单词内部下划线不能凭空变成职位。
    while _MARKDOWN_EMPHASIS.search(marker) is not None:
        marker = _MARKDOWN_EMPHASIS.sub(r"\2", marker)
    return marker.strip().rstrip(_LABEL_COLONS).strip(), explicit


def _role_from_label(label: str) -> _Role:
    """仅当所有明确职位均属于同一角色时接受身份。

    Args:
        label: 已去除 Markdown 包装的完整职位标签。
    Returns:
        明确一致的角色；未知职位或身份冲突返回 unknown。
    Raises:
        无。
    """
    roles: set[_Role] = {_ROLE_LABELS.get(part.strip().casefold(), "unknown") for part in _ROLE_SEPARATOR.split(label)}
    if len(roles) == 1:
        return next(iter(roles))
    return "unknown"


def _parse_identity(line: str) -> _Identity | None:
    """识别独立身份行或必须切断角色继承的未知标记。

    Args:
        line: 保留空白与 Markdown 的原始行，不含行结束符。
    Returns:
        标记对应身份；普通正文返回 None，保留当前发言状态。
    Raises:
        无。
    """
    marker, explicit = _normalize_marker(line)
    if not marker:
        return None
    role = _role_from_label(marker)
    if role != "unknown":
        return _Identity(speaker=None, role=role, evidence=line)
    if _INLINE_LABEL.match(marker) is not None:
        # 冒号仅证明存在行内边界，不证明身份；保留同行正文供独立类型判断。
        return _Identity(speaker=None, role="unknown", evidence=None, has_inline_body=True)
    separator = _SPEAKER_SEPARATOR.search(marker)
    if separator is not None:
        speaker = marker[: separator.start()].strip()
        label = marker[separator.end() :].strip()
        role = _role_from_label(label)
        if speaker and role != "unknown":
            return _Identity(speaker=speaker, role=role, evidence=line)
        # 未知职位或职位冲突也属于边界，不能沿用上一位管理层身份。
        return _UNKNOWN_IDENTITY
    if explicit or line.rstrip().endswith(tuple(_LABEL_COLONS)):
        return _UNKNOWN_IDENTITY
    return None


def _kind_from_body(body: str) -> _Kind:
    """独立根据正文标点推导片段类型，不使用角色或章节名称。

    Args:
        body: 正文；独立标记行已排除，未知行内标签所在行保留。
    Returns:
        含问号为 question，陈述结束标点为 statement，否则 unknown。
    Raises:
        无。
    """
    if any(mark in body for mark in _QUESTION_MARKS):
        return "question"
    if body.rstrip().rstrip(_CLOSING_QUOTES).endswith(_STATEMENT_ENDINGS):
        return "statement"
    return "unknown"


def _make_passage(content: str, start: int, end: int, body_start: int, identity: _Identity) -> EarningsCallPassage:
    """从原文切片构造不可变值，不规范化字符或空白。

    Args:
        content: 完整原文。
        start: 片段起始字符位置。
        end: 片段结束字符位置，不包含该位置。
        body_start: 正文起点；跳过独立标记行，但保留未知行内标签所在行。
        identity: 本片段明确身份或未知身份。
    Returns:
        保留原文范围和身份证据的片段。
    Raises:
        无。
    """
    return EarningsCallPassage(
        start=start,
        end=end,
        text=content[start:end],
        speaker=identity.speaker,
        role=identity.role,
        kind=_kind_from_body(content[body_start:end]),
        evidence=identity.evidence,
    )


def parse_earnings_call_passages(content: str) -> tuple[EarningsCallPassage, ...]:
    """按明确原文行标记派生电话会片段，完整覆盖输入且不推测身份。

    姓名不用于跨段查找身份；独立未知标题或发言标签将身份重置为 unknown。
    身份识别只支持已列明的中英文完整职位，不解析人员名册或语义。
    行内冒号标签仅建立未知边界，不建立身份；该行仍参与正文类型判断。
    类型仅依赖正文问号与结束标点，不保证语义上的问答识别。

    Args:
        content: 原始电话会文本，字符索引按 Python str 计算。
    Returns:
        有序、连续、无重叠且可拼回原文的冻结片段元组；空输入返回空元组。
    Raises:
        无；调用方应按类型契约传入字符串。
    """
    passages: list[EarningsCallPassage] = []
    start = 0
    body_start = 0
    offset = 0
    identity = _UNKNOWN_IDENTITY
    for raw_line in content.splitlines(keepends=True):
        next_identity = _parse_identity(raw_line.rstrip(_LINE_ENDINGS))
        if next_identity is not None:
            if offset > start:
                passages.append(_make_passage(content, start, offset, body_start, identity))
            start = offset
            body_start = offset if next_identity.has_inline_body else offset + len(raw_line)
            identity = next_identity
        offset += len(raw_line)
    if offset > start:
        passages.append(_make_passage(content, start, offset, body_start, identity))
    return tuple(passages)
