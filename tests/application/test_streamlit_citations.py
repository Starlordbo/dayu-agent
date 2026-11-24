"""聊天来源标记解析、流式转换与真实渲染边界接线测试。"""

from __future__ import annotations

from contextlib import nullcontext
from unittest.mock import MagicMock, call
from urllib.parse import parse_qs, quote, urlsplit

import pytest

from dayu.web.streamlit.pages import chat_tab
from dayu.web.streamlit.pages.chat.citations import SourceCitation, parse_source_marker, render_citation_links
from dayu.web.streamlit.pages.chat.stream_runtime import ChatStreamFrameState

_DOCUMENT_MARKER = "[[source:AAPL|mat_xxx]]"
_SECTION_MARKER = "[[source:AAPL|mat_xxx|s_0001]]"
_DOCUMENT_LINK = "[文档来源](?source_ticker=AAPL&source_document=mat_xxx)"
_SECTION_LINK = "[章节来源](?source_ticker=AAPL&source_document=mat_xxx&source_ref=s_0001)"


@pytest.mark.parametrize(
    ("marker", "expected"),
    [
        (_DOCUMENT_MARKER, SourceCitation("AAPL", "mat_xxx")),
        (_SECTION_MARKER, SourceCitation("AAPL", "mat_xxx", "s_0001")),
        ("[[source:BRK.B|mat-2026_01|Item_1.2]]", SourceCitation("BRK.B", "mat-2026_01", "Item_1.2")),
        ("[[source:600519.SH|mat_xxx]]", SourceCitation("600519.SH", "mat_xxx")),
        (f"[[source:AAPL|{quote('材料甲', safe='')}|{quote('章节一', safe='')}]]", SourceCitation("AAPL", "材料甲", "章节一")),
        (f"[[source:AAPL|{'a' * 256}]]", SourceCitation("AAPL", "a" * 256)),
    ],
)
def test_parse_valid_marker(marker: str, expected: SourceCitation) -> None:
    """验证机器身份原样解析，不推断或规范化章节标题。

    参数:
        marker: 编码后的完整来源标记。
        expected: 预期的来源身份。
    返回值:
        无。
    异常:
        AssertionError: 解析结果与身份不一致时抛出。
    """
    assert parse_source_marker(marker) == expected


@pytest.mark.parametrize(
    "marker",
    [
        "", "[[source:AAPL]]", "[[source:|mat_xxx]]", "[[source:AAPL|]]",
        "[[source:AAPL|mat_xxx|]]", "[[source:AAPL|mat_xxx|s_1|extra]]",
        "[[Source:AAPL|mat_xxx]]", " [[source:AAPL|mat_xxx]]",
        "[[source:AAPL|mat_xxx]] trailing", "[[source:AAPL|mat_xxx]",
        "[[source:AAPL|材料甲]]", "[[source:%41APL|mat_xxx]]",
        "[[source:AAPL|mat%ZZ]]", "[[source:AAPL|mat%]]", "[[source:AAPL|%FF]]",
        "[[source:AAPL|%E4%b8%AD]]", "[[source:AAPL|%ED%A0%80]]",
        "[[source:AAPL|..]]", "[[source:AAPL|mat..xxx]]",
        "[[source:AAPL|%2E%2E%2Fetc]]", "[[source:AAPL|%252e%252e%252fetc]]",
        "[[source:AAPL|mat%2Fxxx]]", "[[source:AAPL|mat%5Cxxx]]",
        "[[source:AAPL|%2Fabsolute]]", "[[source:AAPL|C%3A%5Cfile]]",
        "[[source:AAPL|https%3A%2F%2Fevil.test]]",
        "[[source:AAPL|javascript%3Aalert%281%29]]",
        "[[source:AAPL|mat%26source_ref%3Devil]]", "[[source:AAPL|mat%23fragment]]",
        "[[source:AAPL|mat%3Fquery]]", "[[source:AAPL|mat%7Cref]]",
        "[[source:AAPL|mat%29%5D%28https%3A%2F%2Fevil.test%29]]",
        "[[source:AAPL|mat%00xxx]]", "[[source:AAPL|mat%0Axxx]]",
        "[[source:AAPL|mat%20xxx]]", "[[source:AAPL|mat+xxx]]",
        "[[source:AAPL|mat%E2%80%8Bxxx]]", "[[source:AAPL|_hidden]]",
        f"[[source:{'A' * 257}|mat_xxx]]", f"[[source:AAPL|{'a' * 257}]]",
        f"[[source:AAPL|mat_xxx|{'s' * 257}]]",
        f"[[source:AAPL|{'a' * 3073}]]", f"[[source:AAPL|{'a' * 10000}]]",
    ],
)
def test_invalid_markers_remain_plain_text(marker: str) -> None:
    """拒绝非法完整标记，且不把它们转换为来源链接。

    参数:
        marker: 待拒绝的标记文本。
    返回值:
        无。
    异常:
        AssertionError: 接受非法身份或生成错误链接时抛出。
    """
    assert parse_source_marker(marker) is None
    # 完整标记前后的普通文字不是标记自身；转换器仍应识别其中独立合法标记。
    if marker in (" [[source:AAPL|mat_xxx]]", "[[source:AAPL|mat_xxx]] trailing"):
        return
    assert render_citation_links(marker, {}) == marker


def test_query_parameters_preserve_session_and_escape_url() -> None:
    """验证来源参数替换、会话保留及 URL/Markdown 元字符编码。

    参数:
        无。
    返回值:
        无。
    异常:
        AssertionError: 参数丢失、修改输入或产生非相对 URL 时抛出。
    """
    params = {
        "chat_tab_AAPL_session": "session (a)&b#片段",
        "tab": "聊天 分析",
        "next": "https://example.test/a?q=1&x=2",
        "source_ticker": "OLD",
        "source_document": "old_document",
        "source_ref": "old_ref",
        "source_scope": "section",
        "source_unknown": "remove",
        "other_source_key": "keep",
    }
    original = params.copy()
    converted = render_citation_links(_DOCUMENT_MARKER, params)
    url = converted.removeprefix("[文档来源](").removesuffix(")")
    assert url.startswith("?")
    assert not urlsplit(url).scheme and not urlsplit(url).netloc
    assert " " not in url and "(" not in url and ")" not in url and "#" not in url
    assert parse_qs(urlsplit(url).query) == {
        "chat_tab_AAPL_session": [params["chat_tab_AAPL_session"]],
        "tab": [params["tab"]], "next": [params["next"]],
        "other_source_key": ["keep"], "source_ticker": ["AAPL"], "source_document": ["mat_xxx"],
    }
    assert params == original


def test_chinese_english_and_repeated_markers() -> None:
    """验证中英文正文、同一引用重复出现与章节标签。

    参数:
        无。
    返回值:
        无。
    异常:
        AssertionError: 正文变化或重复标记未独立转换时抛出。
    """
    text = f"财报 Annual report：{_DOCUMENT_MARKER}，收入 Revenue {_SECTION_MARKER}。重复{_SECTION_MARKER}"
    expected = f"财报 Annual report：{_DOCUMENT_LINK}，收入 Revenue {_SECTION_LINK}。重复{_SECTION_LINK}"
    assert render_citation_links(text, {}) == expected
    encoded = quote("材料甲", safe="")
    assert render_citation_links(f"[[source:AAPL|{encoded}]]", {}) == (
        f"[文档来源](?source_ticker=AAPL&source_document={encoded})"
    )
    assert render_citation_links("见 Apple 年报营业收入章节", {}) == "见 Apple 年报营业收入章节"
    assert render_citation_links("", {}) == ""


@pytest.mark.parametrize(
    "protected",
    [
        f"`{_SECTION_MARKER}`", f"``code `{_SECTION_MARKER}` code``",
        f"``line\n{_SECTION_MARKER}\nline``", f"`{_SECTION_MARKER}",
        f"```python\n{_SECTION_MARKER}\n```", f"~~~text\n{_SECTION_MARKER}\n~~~",
        f"````text\n```\n{_SECTION_MARKER}\n````",
        f"```text\n~~~\n{_SECTION_MARKER}\n```",
        f"```text\n{_SECTION_MARKER}\n``` trailing\n{_SECTION_MARKER}\n```",
        f"```text\n{_SECTION_MARKER}", f"~~~text\n{_SECTION_MARKER}",
        f"   ```python\r\n{_SECTION_MARKER}\r\n   ````\r\n",
        f"> ```text\n> {_SECTION_MARKER}\n> ```",
        f"- 列表\n    ~~~text\n    {_SECTION_MARKER}\n    ~~~",
        f"- ~~~text\n  {_SECTION_MARKER}\n  ~~~",
        f"1. ~~~text\n   {_SECTION_MARKER}\n   ~~~",
        f"~~~text\n- ~~~\n{_SECTION_MARKER}\n~~~",
        f"\\{_SECTION_MARKER}",
        "\\``" + _SECTION_MARKER + "`",
    ],
)
def test_code_and_escaped_markers_stay_unchanged(protected: str) -> None:
    """验证围栏、行内代码、未闭合代码和显式转义标记不生成链接。

    参数:
        protected: 应原样展示的代码或转义文本。
    返回值:
        无。
    异常:
        AssertionError: 代码内容被替换时抛出。
    """
    assert render_citation_links(protected, {}) == protected


@pytest.mark.parametrize(
    "prefix",
    [
        f"`{_DOCUMENT_MARKER}` ",
        f"``{_DOCUMENT_MARKER} ` `` ",
        f"```python\n{_DOCUMENT_MARKER}\n```\n",
        f"~~~text\n{_DOCUMENT_MARKER}\n~~~~\n",
        f"`{_DOCUMENT_MARKER}\\` ",
        "\\` ",
        "\\\\",
        "```not`fence``` ",
    ],
)
def test_code_boundaries_allow_following_prose(prefix: str) -> None:
    """验证离开代码后继续转换正文，不误用反斜杠转义规则。

    参数:
        prefix: 代码块、行内代码或转义前缀。
    返回值:
        无。
    异常:
        AssertionError: 正文被错误当成代码时抛出。
    """
    assert render_citation_links(prefix + _SECTION_MARKER, {}) == prefix + _SECTION_LINK


@pytest.mark.parametrize("marker", [_DOCUMENT_MARKER, _SECTION_MARKER])
def test_streaming_accumulated_chunks(marker: str) -> None:
    """模拟每个字符跨 chunk 到达，完整闭合之前一直保留普通文本。

    参数:
        marker: 文档级或章节级完整标记。
    返回值:
        无。
    异常:
        AssertionError: 未完成标记被提前链接或终态不一致时抛出。
    """
    for boundary in range(len(marker)):
        current = "当前结果：" + marker[:boundary]
        assert render_citation_links(current, {}) == current
    complete = "当前结果：" + marker
    expected = _SECTION_LINK if marker == _SECTION_MARKER else _DOCUMENT_LINK
    assert render_citation_links(complete, {}) == "当前结果：" + expected


def test_history_and_stream_frames_share_conversion(monkeypatch: pytest.MonkeyPatch) -> None:
    """在真实渲染函数边界验证历史、流式帧和终态共享转换且不处理思考。

    参数:
        monkeypatch: pytest 的依赖替换工具，仅替换 Streamlit UI。
    返回值:
        无。
    异常:
        AssertionError: 转换入口不同、会话参数丢失或思考被转换时抛出。
    """
    ui = MagicMock()
    ui.columns.return_value = (nullcontext(), nullcontext())
    ui.chat_message.return_value = nullcontext()
    ui.expander.return_value = nullcontext()
    query = {"chat_tab_AAPL_session": "existing-session", "source_ref": "stale"}
    ui.query_params.to_dict.return_value = query
    monkeypatch.setattr(chat_tab, "st", ui)
    content = "结论：" + _SECTION_MARKER
    reasoning = "内部思考 " + _DOCUMENT_MARKER
    expected = render_citation_links(content, query | {"ticker": "MSFT", "view": "analysis"})
    message = chat_tab.ChatMessage(role="assistant", content=content, reasoning_content=reasoning)
    chat_tab._render_message_history(
        [chat_tab.ChatMessage(role="user", content=_DOCUMENT_MARKER), message], ticker="MSFT",
    )
    assert ui.markdown.call_args_list == [call(_DOCUMENT_MARKER), call(reasoning), call(expected)]
    assert message.content == content
    for done in (False, True):
        ui.markdown.reset_mock()
        state = ChatStreamFrameState(
            trace_id="trace", session_id="existing-session", reasoning_text=reasoning,
            answer_text=content, side_messages=[], filtered_flags=[], done=done, error_message="",
        )
        chat_tab._render_stream_frame(title="思考内容", state=state, ticker="MSFT")
        assert ui.markdown.call_args_list == [call(reasoning), call(expected)]
        assert state.answer_text == content
    assert ui.query_params.to_dict.call_count == 3
