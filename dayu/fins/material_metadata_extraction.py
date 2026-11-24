"""从上传字节轻量提取材料标题与开场，不转换文档或推断财季。"""

from __future__ import annotations

import codecs
from io import BytesIO
from pathlib import PurePath
import re
import unicodedata
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile
from zlib import error as ZlibError


_TEXT_EXTENSIONS = frozenset({".md", ".txt"})
_DOCX_EXTENSION = ".docx"
_DOCX_DOCUMENT_PATH = "word/document.xml"
_DOCX_PARAGRAPH_TAG = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p"
_DOCX_TEXT_TAG = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t"
_MAX_DOCX_XML_BYTES = 2 * 1024 * 1024
_OPENING_PARAGRAPH_LIMIT = 2
_TITLE_PARAGRAPH_COUNT = 1
_PARAGRAPH_SEPARATOR = "\n\n"
_ALLOWED_CONTROL_CHARACTERS = frozenset({"\t", "\n", "\r"})
_CONTROL_CHARACTER_CATEGORY = "Cc"
_UNICODE_REPLACEMENT_CHARACTER = "\ufffd"
_MARKDOWN_HEADING_PATTERN = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+(.+?)[ \t]*$")
_MARKDOWN_CLOSING_HASH_PATTERN = re.compile(r"[ \t]+#+[ \t]*$")
_PARAGRAPH_BREAK_PATTERN = re.compile(r"\n[ \t]*\n+")


def _decode_text(data: bytes) -> str | None:
    """严格解码带 BOM 或 UTF-8 的文本，并拒绝二进制控制字符。

    Args:
        data: 待解码的原始材料字节。

    Returns:
        可读文本；无法严格解码或包含二进制控制字符时返回 `None`。

    Raises:
        无。
    """
    if data.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)):
        encoding = "utf-32"
    elif data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        encoding = "utf-16"
    else:
        encoding = "utf-8-sig"
    try:
        text = data.decode(encoding)
    except UnicodeError:
        return None
    if _UNICODE_REPLACEMENT_CHARACTER in text or any(
        unicodedata.category(character) == _CONTROL_CHARACTER_CATEGORY
        and character not in _ALLOWED_CONTROL_CHARACTERS
        for character in text
    ):
        return None
    return text


def _extract_text_metadata(data: bytes) -> tuple[str | None, str | None]:
    """读取文本标题，并从标题后取最多两段开场。

    Args:
        data: Markdown 或 TXT 材料字节。

    Returns:
        首非空行标题与后续开场；标题上的 Markdown 标记会被移除。

    Raises:
        无。
    """
    text = _decode_text(data)
    if text is None:
        return None, None
    lines = text.splitlines()
    title: str | None = None
    title_index: int | None = None
    for index, line in enumerate(lines):
        cleaned = line.strip()
        if not cleaned:
            continue
        title, title_index = cleaned, index
        heading = _MARKDOWN_HEADING_PATTERN.fullmatch(line)
        if heading is not None:
            title = _MARKDOWN_CLOSING_HASH_PATTERN.sub("", heading.group(1)).strip()
        # 只取开头的标题，正文后段的小节标题不能覆盖当期口径。
        break
    if title_index is None:
        return None, None
    # 标题本身不能混入 opening，否则会错误提升财季声明的来源优先级。
    body = "\n".join(lines[title_index + _TITLE_PARAGRAPH_COUNT :]).strip()
    if not body:
        return title, None
    paragraphs = [
        cleaned
        for paragraph in _PARAGRAPH_BREAK_PATTERN.split(body)
        if (cleaned := paragraph.strip())
    ]
    opening = _PARAGRAPH_SEPARATOR.join(paragraphs[:_OPENING_PARAGRAPH_LIMIT])
    return title, opening or None


def _extract_docx_metadata(data: bytes) -> tuple[str | None, str | None]:
    """在固定解压上限内读取 DOCX 文档的前三个非空段落。

    Args:
        data: DOCX 的 ZIP 容器字节。

    Returns:
        首段标题及后续最多两段开场；损坏、缺失或超限时返回空元数据。

    Raises:
        无。
    """
    try:
        with ZipFile(BytesIO(data)) as archive:
            info = archive.getinfo(_DOCX_DOCUMENT_PATH)
            if info.file_size > _MAX_DOCX_XML_BYTES:
                return None, None
            with archive.open(info) as document:
                xml_data = document.read(_MAX_DOCX_XML_BYTES + 1)
            if len(xml_data) > _MAX_DOCX_XML_BYTES:
                return None, None
        root = ElementTree.fromstring(xml_data)
    except (
        BadZipFile,
        ElementTree.ParseError,
        KeyError,
        OSError,
        RuntimeError,
        EOFError,
        ZlibError,
    ):
        return None, None
    paragraphs: list[str] = []
    for paragraph in root.iter(_DOCX_PARAGRAPH_TAG):
        text = "".join(node.text or "" for node in paragraph.iter(_DOCX_TEXT_TAG)).strip()
        if text:
            paragraphs.append(text)
        if len(paragraphs) >= _TITLE_PARAGRAPH_COUNT + _OPENING_PARAGRAPH_LIMIT:
            break
    if not paragraphs:
        return None, None
    opening = _PARAGRAPH_SEPARATOR.join(paragraphs[_TITLE_PARAGRAPH_COUNT:])
    return paragraphs[0], opening or None


def extract_material_metadata(
    *,
    filename: str,
    data: bytes,
) -> tuple[str | None, str | None]:
    """从支持格式的上传材料字节中提取标题与开场。

    Args:
        filename: 用于选择 MD、TXT 或 DOCX 解析方式的材料文件名。
        data: 浏览器已持有的材料字节，不通过文件路径读取正文。

    Returns:
        `(title, opening)`；PDF、未支持格式、空内容及提取失败均返回
        `(None, None)`，由调用方决定是否退回文件名识别。

    Raises:
        无。
    """
    extension = PurePath(filename).suffix.casefold()
    if extension in _TEXT_EXTENSIONS:
        return _extract_text_metadata(data)
    if extension == _DOCX_EXTENSION:
        return _extract_docx_metadata(data)
    return None, None
