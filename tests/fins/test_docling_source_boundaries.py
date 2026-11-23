"""真实 Docling 结构的原文边界与快照版本回归。"""

from pathlib import Path

from docling_core.types.doc.document import DoclingDocument
from docling_core.types.doc.labels import DocItemLabel
import pytest

from dayu.engine.processors.docling_processor import DoclingProcessor
from dayu.fins.processors.fins_docling_processor import FinsDoclingProcessor
from dayu.fins.storage.local_file_source import LocalFileSource


@pytest.mark.parametrize(
    ("processor_type", "expected_version"),
    [
        (DoclingProcessor, "docling_processor_v1.2.0"),
        (FinsDoclingProcessor, "fins_docling_processor_v1.1.0"),
    ],
)
def test_section_preserves_block_and_internal_line_boundaries(
    tmp_path: Path, processor_type: type[DoclingProcessor], expected_version: str
) -> None:
    """块间与块内换行均须保留，缓存重读一致且业务版本不能沿用旧快照。

    Args:
        tmp_path: pytest 临时目录。
        processor_type: 引擎或业务真实处理器类型。
        expected_version: 本次有损正文修复后的明确版本。
    Returns:
        无。
    Raises:
        AssertionError: 正文边界、缓存或版本契约不符。
    """
    document = DoclingDocument(name="Evidence")
    document.add_heading(text="Questions and Answers", level=1)
    document.add_text(label=DocItemLabel.TEXT, text="Alex Smith - Analyst\nCould revenue double?")
    document.add_text(label=DocItemLabel.TEXT, text="Jane Doe - Chief Executive Officer")
    document.add_text(label=DocItemLabel.TEXT, text="We have not provided that forecast.")
    path = tmp_path / "source_docling.json"
    document.save_as_json(path)
    processor = processor_type(LocalFileSource(path=path, uri=path.as_uri()))
    sections = processor.list_sections()
    assert len(sections) == 1
    ref = sections[0]["ref"]
    content = processor.read_section(ref)["content"]
    assert "Alex Smith - Analyst\nCould revenue double?" in content
    assert "Could revenue double?\n\nJane Doe - Chief Executive Officer\n\nWe have not" in content
    assert processor.read_section(ref)["content"] == content
    assert processor.get_parser_version() == expected_version
