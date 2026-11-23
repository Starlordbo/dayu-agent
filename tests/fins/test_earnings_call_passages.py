"""验证 W7 电话会片段的身份证据、独立语义与原文无损范围。"""

# pyright: strict

from dataclasses import FrozenInstanceError

import pytest

from dayu.fins.earnings_call_passages import EarningsCallPassage, parse_earnings_call_passages


def _assert_lossless(content: str, passages: tuple[EarningsCallPassage, ...]) -> None:
    """检查片段完整覆盖原文且范围连续。

    Args:
        content: 完整原文。
        passages: 待检查的不可变片段序列。
    Returns:
        无。
    Raises:
        AssertionError: 片段有丢失、重叠或范围错误。
    """
    assert "".join(passage.text for passage in passages) == content
    cursor = 0
    for passage in passages:
        assert passage.start == cursor
        assert passage.end > passage.start
        assert passage.text == content[passage.start : passage.end]
        cursor = passage.end
    assert cursor == len(content)


@pytest.mark.parametrize("content", ["", " ", "\n\r\n", "未知正文", "😊财报\r\n增长。", "Q&A\nNo identity?"])
def test_unmarked_content_is_lossless_and_unknown(content: str) -> None:
    """无身份的空白、中英文与 Unicode 原文不得丢失或猜身份。

    Args:
        content: 未标身份的参数化原文。
    Returns:
        无。
    Raises:
        AssertionError: 原文或未知身份契约被破坏。
    """
    passages = parse_earnings_call_passages(content)
    _assert_lossless(content, passages)
    assert len(passages) == bool(content)
    assert all(passage.role == "unknown" and passage.evidence is None for passage in passages)
    assert all(passage.speaker is None for passage in passages)


def test_mixed_english_speakers_split_inside_one_qa_section() -> None:
    """同章问答按身份行拆分，问题与角色不互相推断。

    Args:
        无。
    Returns:
        无。
    Raises:
        AssertionError: 角色、证据或问题类型不符合原文。
    """
    content = (
        "## Q&A\n"
        "Alex Smith - Analyst\nThank you for the update.\n"
        "Jane Doe - Chief Executive Officer/CFO\nCould you clarify your question?\n"
        "Operator\nThe next question is from Alex.\n"
        "Alex Smith - Analyst\nWhat is the margin outlook?\n"
    )
    passages = parse_earnings_call_passages(content)
    _assert_lossless(content, passages)
    assert [passage.role for passage in passages] == ["unknown", "analyst", "management", "operator", "analyst"]
    assert [passage.kind for passage in passages] == ["unknown", "statement", "question", "statement", "question"]
    assert passages[1].speaker == "Alex Smith"
    assert passages[2].speaker == "Jane Doe"
    assert passages[2].evidence == "Jane Doe - Chief Executive Officer/CFO"
    assert passages[3].speaker is None


@pytest.mark.parametrize(
    "identity,role",
    [
        ("主持人", "operator"),
        ("分析师", "analyst"),
        ("首席执行官", "management"),
        ("管理层", "management"),
        ("张三 — 首席财务官", "management"),
        ("张三—分析师", "analyst"),
        ("张三 - 首席执行官、首席财务官", "management"),
    ],
)
def test_chinese_identity_marks(identity: str, role: str) -> None:
    """中文明确身份与全角问号产生独立角色和问题类型。

    Args:
        identity: 中文身份行。
        role: 期望角色。
    Returns:
        无。
    Raises:
        AssertionError: 中文身份识别错误或正文丢失。
    """
    content = f"{identity}\r\n本季度如何增长？\r\n谢谢。"
    passages = parse_earnings_call_passages(content)
    _assert_lossless(content, passages)
    assert len(passages) == 1
    assert passages[0].role == role
    assert passages[0].kind == "question"
    assert passages[0].evidence == identity


@pytest.mark.parametrize(
    "identity",
    [
        "  ### **Jane Doe - CFO**  ",
        "> **Jane Doe** - **CFO**",
        "- Jane Doe - CFO",
        "1. Jane Doe - CFO",
        "__Jane Doe - CFO__",
        "Jane Doe - CFO:",
        "### Jane Doe - CFO ###",
        "Jane Doe - CEO and CFO",
        "Jane Doe - ceo & cfo",
        "***Jane Doe - CFO***",
    ],
)
def test_markdown_identity_preserves_exact_evidence(identity: str) -> None:
    """Markdown 仅用于识别，不重写证据、正文和字符偏移。

    Args:
        identity: 带空格或 Markdown 的身份行。
    Returns:
        无。
    Raises:
        AssertionError: Markdown 识别或原始证据错误。
    """
    content = f"前言。\r\n{identity}\r\nRevenue grew.\r\n"
    passages = parse_earnings_call_passages(content)
    _assert_lossless(content, passages)
    assert len(passages) == 2
    assert passages[1].role == "management"
    assert passages[1].speaker == "Jane Doe"
    assert passages[1].evidence == identity
    assert passages[1].start == len("前言。\r\n")


@pytest.mark.parametrize(
    "label",
    [
        "## Q&A",
        "**Alex Smith**",
        "Alex Smith:",
        "Alex Smith - Example Bank",
        "Alex Smith - Analyst/CEO",
        "Alex Smith - Former CFO",
        "Alex Smith - CFO of Example",
        "Analyst expectations are high.",
        "Jane Doe - _CFO",
        "Jane Doe - C_F_O",
        "Jane Doe - *CFO",
        "Jane Doe - CFO but not management",
        "主持人：欢迎参加会议。",
        "Jane Doe - CFO; Revenue grew.",
    ],
)
def test_unknown_labels_never_establish_identity(label: str) -> None:
    """名字、机构、身份冲突和正文角色词都不能建立身份。

    Args:
        label: 不足以证明身份的行。
    Returns:
        无。
    Raises:
        AssertionError: 从不足证据推测身份。
    """
    content = f"{label}\nCan you explain?"
    passages = parse_earnings_call_passages(content)
    _assert_lossless(content, passages)
    assert all(passage.role == "unknown" and passage.evidence is None for passage in passages)
    assert passages[-1].kind == "question"


@pytest.mark.parametrize("label", ["## Q&A", "**Alex Smith**", "Alex Smith:", "Alex Smith - Example Bank"])
def test_unrecognized_marker_resets_previous_role(label: str) -> None:
    """显式未知发言标记切断前一角色，不将后续问题归入管理层。

    Args:
        label: 未知发言或章节标记。
    Returns:
        无。
    Raises:
        AssertionError: 身份跨未知标记泄漏。
    """
    content = f"Jane Doe - CEO\nRevenue grew.\n{label}\nWhy?\n"
    passages = parse_earnings_call_passages(content)
    _assert_lossless(content, passages)
    assert [passage.role for passage in passages] == ["management", "unknown"]
    assert passages[-1].evidence is None


@pytest.mark.parametrize(
    "body,kind",
    [
        ("", "unknown"),
        ("\n", "unknown"),
        ("Revenue", "unknown"),
        ("Revenue grew.", "statement"),
        ("增长！", "statement"),
        ("Really? Yes.", "question"),
        ("增长？是的。", "question"),
    ],
)
def test_kind_requires_body_evidence(body: str, kind: str) -> None:
    """片段类型独立取自正文，身份行本身不是陈述。

    Args:
        body: 不同标点证据的正文。
        kind: 期望片段类型。
    Returns:
        无。
    Raises:
        AssertionError: 类型依赖角色或身份行。
    """
    content = f"Analyst\n{body}"
    passages = parse_earnings_call_passages(content)
    _assert_lossless(content, passages)
    assert passages[0].kind == kind
    assert passages[0].role == "analyst"


def test_consecutive_markers_and_final_line_have_valid_ranges() -> None:
    """连续身份行与无末尾换行的输入仍产生完整非空范围。

    Args:
        无。
    Returns:
        无。
    Raises:
        AssertionError: 空片段、末尾遗漏或角色错误。
    """
    content = "Operator\rAnalyst\nJane Doe - CEO"
    passages = parse_earnings_call_passages(content)
    _assert_lossless(content, passages)
    assert [passage.role for passage in passages] == ["operator", "analyst", "management"]
    assert all(passage.kind == "unknown" for passage in passages)


@pytest.mark.parametrize(
    "identity,role",
    [("Jane Doe - CEO", "management"), ("Analyst", "analyst"), ("主持人", "operator")],
)
@pytest.mark.parametrize(
    "inline,kind",
    [
        ("Analyst: Could revenue reach 100 million next year?", "question"),
        ("CFO: Revenue grew.", "statement"),
        ("主持人：下一位分析师请提问。", "statement"),
        ("分析师：明年收入能到一亿元吗？", "question"),
        ("张三：明年收入能到一亿元吗？", "question"),
        ("Alex Smith: Could revenue reach 100 million next year?", "question"),
        ("**Alex Smith:** Could revenue reach 100 million next year?", "question"),
        ("**Alex Smith**: Could revenue reach 100 million next year?", "question"),
        ("> **Alex Smith:** Could revenue reach 100 million next year?", "question"),
        ("Alex Smith - Example Bank: Could revenue reach 100 million next year?", "question"),
        ("Alex Smith: Could revenue double? - CEO", "question"),
        ("Alex Smith: Revenue", "unknown"),
    ],
)
def test_inline_label_resets_role_and_preserves_body_kind(identity: str, role: str, inline: str, kind: str) -> None:
    """已知角色后的行内标签只建立未知边界，同行正文仍决定类型。

    Args:
        identity: 前一段明确身份行。
        role: 前一段期望角色。
        inline: 带正文的中英文或 Markdown 显式行内标签。
        kind: 行内正文独立提供的期望类型。
    Returns:
        无。
    Raises:
        AssertionError: 身份泄漏、同行正文被丢弃或原文范围错误。
    """
    content = f"{identity}\nRevenue grew.\n{inline}\n"
    passages = parse_earnings_call_passages(content)
    _assert_lossless(content, passages)
    assert [passage.role for passage in passages] == [role, "unknown"]
    assert [passage.kind for passage in passages] == ["statement", kind]
    assert passages[0].evidence == identity
    assert passages[1].speaker is None
    assert passages[1].evidence is None
    assert passages[1].text == f"{inline}\n"


@pytest.mark.parametrize(
    "line",
    [
        "http://example.com/transcript",
        "https://example.com/transcript?year=2026",
        "12:30",
        "12:30 Meeting begins.",
        "Revenue grew at 12:30.",
        "See http://example.com/transcript for details.",
    ],
)
def test_urls_and_times_do_not_create_inline_boundaries(line: str) -> None:
    """普通链接与时间不因冒号变成发言标签或额外片段。

    Args:
        line: 含 URL 或时间冒号的普通正文。
    Returns:
        无。
    Raises:
        AssertionError: 普通正文被错误分段或角色丢失。
    """
    content = f"Jane Doe - CEO\nRevenue grew.\n{line}\n"
    passages = parse_earnings_call_passages(content)
    _assert_lossless(content, passages)
    assert len(passages) == 1
    assert passages[0].role == "management"
    assert passages[0].evidence == "Jane Doe - CEO"


def test_mixed_consecutive_inline_boundaries_are_lossless_and_restore_only_explicit_roles() -> None:
    """混合连续中英文行内标签切断身份，只有后续独立身份行恢复角色。

    Args:
        无。
    Returns:
        无。
    Raises:
        AssertionError: 连续边界重叠、漏字、错误继承身份或遗漏行内问句。
    """
    content = (
        "Jane Doe - CEO\r\nRevenue grew.\r\n"
        "Analyst: Could revenue reach 100 million next year?\r\n"
        "主持人：下一位分析师请提问。\n"
        "**Alex Smith:** Could revenue double?\nThank you.\n"
        "张三：能解释增长吗？\r"
        "Jane Doe - CFO\nCould you clarify?\n"
        "__Example Bank:__ Thank you."
    )
    passages = parse_earnings_call_passages(content)
    _assert_lossless(content, passages)
    assert [passage.role for passage in passages] == [
        "management",
        "unknown",
        "unknown",
        "unknown",
        "unknown",
        "management",
        "unknown",
    ]
    assert [passage.kind for passage in passages] == [
        "statement",
        "question",
        "statement",
        "question",
        "question",
        "question",
        "statement",
    ]
    assert all(
        passage.evidence is None and passage.speaker is None for passage in passages if passage.role == "unknown"
    )
    assert passages[5].evidence == "Jane Doe - CFO"


def test_immutable_deterministic_result() -> None:
    """解析结果是确定性的冻结数据值，不允许调用方修改身份。

    Args:
        无。
    Returns:
        无。
    Raises:
        AssertionError: 返回结构可变或同源输入结果不一致。
    """
    content = "Jane Doe - CEO\nRevenue grew."
    passages = parse_earnings_call_passages(content)
    assert passages == parse_earnings_call_passages(content)
    assert hash(passages) == hash(parse_earnings_call_passages(content))
    with pytest.raises(FrozenInstanceError):
        setattr(passages[0], "role", "analyst")
