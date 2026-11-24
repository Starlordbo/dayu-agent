"""上传材料轻量标题与开场提取的格式、失败回退及来源隔离测试。"""

from __future__ import annotations

import codecs
from io import BytesIO
from xml.sax.saxutils import escape
from zipfile import ZipFile

import pytest

from dayu.fins.material_metadata_extraction import extract_material_metadata
from dayu.fins.upload_recognition import infer_earnings_call_fiscal


_DOCX_DOCUMENT_PATH = "word/document.xml"
_WORDPROCESSING_NAMESPACE = (
    "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
)
_TITLE = "Example Q3 2024 Earnings Call"
_FIRST_OPENING = "Welcome to our Q2 2024 earnings call."
_SECOND_OPENING = "Management will discuss the reported results."
_LATER_PARAGRAPH = "The Q1 2023 figures appear later in the discussion."
_EXPECTED_OPENING = f"{_FIRST_OPENING}\n\n{_SECOND_OPENING}"
_OVERSIZED_XML_CHARACTERS = 4 * 1024 * 1024


def _package_docx_xml(
    document_xml: str,
    *,
    document_path: str = _DOCX_DOCUMENT_PATH,
) -> bytes:
    """打包仅包含所需 XML 部件的 DOCX 测试字节。

    Args:
        document_xml: 写入归档的文档 XML。
        document_path: XML 在归档中的路径，用于验证缺失文档部件。

    Returns:
        可用于轻量提取的 ZIP 字节。

    Raises:
        OSError: 内存归档写入失败时抛出。
    """
    stream = BytesIO()
    with ZipFile(stream, mode="w") as archive:
        archive.writestr(document_path, document_xml)
    return stream.getvalue()


def _build_docx(paragraphs: tuple[tuple[str, ...], ...]) -> bytes:
    """从段落内的多个文本 run 构建最小 DOCX。

    Args:
        paragraphs: 段落序列；每个段落内的字符串表示一个文本 run。

    Returns:
        含标准命名空间文档 XML 的 DOCX 字节。

    Raises:
        OSError: 内存归档写入失败时抛出。
    """
    paragraph_xml = "".join(
        "<w:p>"
        + "".join(f"<w:r><w:t>{escape(run)}</w:t></w:r>" for run in paragraph)
        + "</w:p>"
        for paragraph in paragraphs
    )
    return _package_docx_xml(
        f'<w:document xmlns:w="{_WORDPROCESSING_NAMESPACE}">'
        f"<w:body>{paragraph_xml}</w:body></w:document>"
    )


@pytest.mark.unit
@pytest.mark.parametrize("filename", ["call.md", "CALL.MD", "call.txt"])
@pytest.mark.parametrize(
    "paragraph_separator",
    ["\n\n", "\n \n \n \n"],
    ids=["blank-line", "multiple-whitespace-lines"],
)
def test_markdown_extracts_heading_and_only_first_two_body_paragraphs(
    filename: str,
    paragraph_separator: str,
) -> None:
    """Markdown 标题不进入开场，后续讨论也不影响预填。

    Args:
        filename: 含不同大小写扩展名的材料名。
        paragraph_separator: 正文两段之间的空行，可能包含多行空格。

    Returns:
        无。

    Raises:
        AssertionError: 标题或开场边界不符合约定时抛出。
    """
    data = (
        f"\n# {_TITLE}\n\n{_FIRST_OPENING}{paragraph_separator}"
        f"{_SECOND_OPENING}\n\n{_LATER_PARAGRAPH}"
    ).encode("utf-8-sig")

    title, opening = extract_material_metadata(filename=filename, data=data)

    assert title == _TITLE
    assert opening == _EXPECTED_OPENING


@pytest.mark.unit
@pytest.mark.parametrize(
    "data",
    [
        f"\n\n{_TITLE}\r\n\r\n{_FIRST_OPENING}".encode("utf-8"),
        f"\n\n{_TITLE}\r\n\r\n{_FIRST_OPENING}".encode("utf-8-sig"),
        codecs.BOM_UTF16_LE
        + f"\n\n{_TITLE}\r\n\r\n{_FIRST_OPENING}".encode("utf-16-le"),
        codecs.BOM_UTF16_BE
        + f"\n\n{_TITLE}\r\n\r\n{_FIRST_OPENING}".encode("utf-16-be"),
    ],
    ids=["utf8", "utf8-bom", "utf16-le-bom", "utf16-be-bom"],
)
def test_text_uses_first_nonempty_line_and_honors_bom(data: bytes) -> None:
    """TXT 解码尊重 BOM，并把首非空行和正文分开。

    Args:
        data: 使用支持编码和 CRLF 换行的文本字节。

    Returns:
        无。

    Raises:
        AssertionError: 编码、标题或开场提取不符合约定时抛出。
    """
    assert extract_material_metadata(filename="call.txt", data=data) == (
        _TITLE,
        _FIRST_OPENING,
    )


@pytest.mark.unit
def test_text_without_blank_line_after_title_keeps_opening() -> None:
    """标题与正文只隔一个换行时仍保留正文。

    Args:
        无。

    Returns:
        无。

    Raises:
        AssertionError: 首行标题吞掉紧接的正文时抛出。
    """
    data = f"{_TITLE}\n{_FIRST_OPENING}".encode("utf-8")

    assert extract_material_metadata(filename="call.txt", data=data) == (
        _TITLE,
        _FIRST_OPENING,
    )


@pytest.mark.unit
@pytest.mark.parametrize("filename", ["call.md", "call.txt"])
def test_later_section_heading_cannot_replace_initial_metadata(filename: str) -> None:
    """后文小节标题不能覆盖首行标题或开头两段声明。

    Args:
        filename: 使用 Markdown 或 TXT 扩展名的材料名。

    Returns:
        无。

    Raises:
        AssertionError: 正文后段被当作标题或开场时抛出。
    """
    data = (
        f"{_TITLE}\n\n{_FIRST_OPENING}\n\n{_SECOND_OPENING}\n\n"
        f"## Q1 2023 comparison\n\n{_LATER_PARAGRAPH}"
    ).encode("utf-8")

    assert extract_material_metadata(filename=filename, data=data) == (
        _TITLE,
        _EXPECTED_OPENING,
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("filename", "data"),
    [
        ("call.md", f"# {_TITLE}".encode("utf-8")),
        ("call.txt", _TITLE.encode("utf-8")),
    ],
)
def test_title_only_material_has_no_opening(filename: str, data: bytes) -> None:
    """只有标题的材料不能伪造开场声明来源。

    Args:
        filename: 材料名。
        data: 仅包含标题的文本字节。

    Returns:
        无。

    Raises:
        AssertionError: 标题被重复当作开场时抛出。
    """
    assert extract_material_metadata(filename=filename, data=data) == (_TITLE, None)


@pytest.mark.unit
def test_docx_joins_text_runs_and_reads_first_body_paragraphs() -> None:
    """DOCX 直接合并段落内文本 run 并跳过空段落。

    Args:
        无。

    Returns:
        无。

    Raises:
        AssertionError: DOCX 段落边界或文本合并不符合约定时抛出。
    """
    data = _build_docx(
        (
            (),
            ("Example ", "Q3 2024 Earnings Call"),
            (),
            (_FIRST_OPENING,),
            (_SECOND_OPENING,),
            (_LATER_PARAGRAPH,),
        )
    )

    assert extract_material_metadata(filename="call.docx", data=data) == (
        _TITLE,
        _EXPECTED_OPENING,
    )


@pytest.mark.unit
def test_docx_oversized_xml_falls_back_without_unbounded_extraction() -> None:
    """过大的 DOCX XML 不进入轻量提取路径。

    Args:
        无。

    Returns:
        无。

    Raises:
        AssertionError: 超出轻量读取范围的文档仍返回元数据时抛出。
    """
    data = _build_docx(((_TITLE,), ("a" * _OVERSIZED_XML_CHARACTERS,)))

    assert extract_material_metadata(filename="call.docx", data=data) == (None, None)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("filename", "data"),
    [
        ("call.txt", b""),
        ("call.md", b" \r\n\t\n "),
        ("call.txt", b"\xff\x81\xfe\x80"),
        ("call.txt", "Unreadable \ufffd document".encode("utf-8")),
        ("call.txt", b"\x00\x01\x02\x03"),
        ("call.docx", b"not a zip archive"),
        ("call.docx", _package_docx_xml("<broken")),
        (
            "call.docx",
            _package_docx_xml("<unused />", document_path="word/styles.xml"),
        ),
        ("call.docx", _build_docx(((), (" ",)))),
    ],
    ids=[
        "empty",
        "whitespace",
        "invalid-encoding",
        "replacement-character",
        "binary-controls",
        "broken-archive",
        "broken-xml",
        "missing-document",
        "empty-document",
    ],
)
def test_invalid_or_empty_material_returns_no_metadata(
    filename: str,
    data: bytes,
) -> None:
    """无可用正文时返回空元数据供上层退回文件名。

    Args:
        filename: 材料名。
        data: 空内容、乱码或损坏文档字节。

    Returns:
        无。

    Raises:
        AssertionError: 失败未回退为空元数据时抛出。
    """
    assert extract_material_metadata(filename=filename, data=data) == (None, None)


@pytest.mark.unit
@pytest.mark.parametrize("filename", ["call.pdf", "CALL.PDF", "call.csv"])
def test_unsupported_formats_do_not_claim_body_extraction(filename: str) -> None:
    """PDF 及未支持格式即便含可读文本也不提取正文。

    Args:
        filename: PDF 或其他不支持的材料名。

    Returns:
        无。

    Raises:
        AssertionError: 不支持的格式被当作文本解析时抛出。
    """
    data = f"# {_TITLE}\n\n{_FIRST_OPENING}".encode("utf-8")

    assert extract_material_metadata(filename=filename, data=data) == (None, None)


@pytest.mark.unit
def test_extracted_title_cannot_override_higher_priority_opening() -> None:
    """标题和开场来源隔离后沿用已有财季优先级及冲突语义。

    Args:
        无。

    Returns:
        无。

    Raises:
        AssertionError: 标题污染开场或冲突未被保留时抛出。
    """
    filename = "Example_Q4_2024.txt"
    title, opening = extract_material_metadata(
        filename=filename,
        data=f"{_TITLE}\n\n{_FIRST_OPENING}".encode("utf-8"),
    )

    guess = infer_earnings_call_fiscal(
        filename=filename,
        title=title,
        opening=opening,
    )

    assert guess.fiscal_year == 2024
    assert guess.fiscal_period == "Q2"
    assert guess.source == "opening"
    assert guess.conflict is True
    assert guess.needs_confirmation is True


@pytest.mark.unit
def test_title_only_declaration_preserves_title_source() -> None:
    """无财季声明的正文不得让标题被误报为开场识别。

    Args:
        无。

    Returns:
        无。

    Raises:
        AssertionError: 标题来源被错误升级为开场来源时抛出。
    """
    filename = "Example_Q4_2024.md"
    title, opening = extract_material_metadata(
        filename=filename,
        data=f"# {_TITLE}\n\nWelcome everyone.".encode("utf-8"),
    )

    guess = infer_earnings_call_fiscal(
        filename=filename,
        title=title,
        opening=opening,
    )

    assert guess.fiscal_period == "Q3"
    assert guess.source == "title"
    assert guess.conflict is True
    assert guess.needs_confirmation is True
