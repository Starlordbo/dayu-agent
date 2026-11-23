"""电话会议纪要转换质量判定测试。

覆盖 `docs/p2-verification.md` 的 S6、S7。
"""

from __future__ import annotations

from dayu.fins.pipelines.earnings_call_quality import (
    MATERIAL_READY,
    REASON_EMPTY_TEXT,
    REASON_GARBLED_TEXT,
    REASON_PAGE_ORDER_BROKEN,
    assess_conversion_reason,
    assess_docling_payload,
)
from dayu.fins.pipelines.json_types import JsonValue


def _payload(texts: list[tuple[str, int | None]]) -> dict[str, JsonValue]:
    """构造最小 Docling 导出字典。

    Args:
        texts: `(正文, 页号)` 列表。

    Returns:
        Docling 风格导出字典。

    Raises:
        无。
    """

    items: list[JsonValue] = []
    for text, page_no in texts:
        item: dict[str, JsonValue] = {"text": text}
        if page_no is not None:
            item["prov"] = [{"page_no": page_no}]
        items.append(item)
    return {"texts": items}


def test_normal_payload_is_ready() -> None:
    """正常正文判为可问答。"""
    quality = assess_docling_payload(_payload([("管理层回答", 1), ("分析师提问", 2)]))
    assert quality.ready is True
    assert quality.reason == MATERIAL_READY
    assert quality.text_chars > 0


def test_missing_texts_is_empty() -> None:
    """没有 texts 字段判为空文本。"""
    quality = assess_docling_payload({})
    assert quality.ready is False
    assert quality.reason == REASON_EMPTY_TEXT
    assert quality.text_chars == 0


def test_whitespace_only_text_is_empty() -> None:
    """只有空白字符判为空文本。"""
    quality = assess_docling_payload(_payload([("   ", 1), ("\n\t", 2)]))
    assert quality.ready is False
    assert quality.reason == REASON_EMPTY_TEXT


def test_replacement_heavy_text_is_garbled() -> None:
    """替换字符占比过高判为乱码。"""
    quality = assess_docling_payload(_payload([("\ufffd" * 20 + "abc", 1)]))
    assert quality.ready is False
    assert quality.reason == REASON_GARBLED_TEXT


def test_descending_pages_are_broken() -> None:
    """页号回退判为段落顺序损坏。"""
    quality = assess_docling_payload(_payload([("第一页", 3), ("第二页", 1)]))
    assert quality.ready is False
    assert quality.reason == REASON_PAGE_ORDER_BROKEN


def test_same_page_repeated_is_allowed() -> None:
    """同一页多段不构成页序损坏。"""
    quality = assess_docling_payload(_payload([("段一", 2), ("段二", 2), ("段三", 3)]))
    assert quality.ready is True


def test_missing_page_numbers_are_tolerated() -> None:
    """缺少页号时不做页序判定。"""
    quality = assess_docling_payload(_payload([("无页号正文", None)]))
    assert quality.ready is True
