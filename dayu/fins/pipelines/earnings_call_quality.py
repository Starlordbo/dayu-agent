"""电话会议纪要转换后的可问答质量判定。

转换返回成功不等于可问答：正文为空、正文乱码、段落页序损坏都必须判死，
避免把坏材料登记成“已处理”。本模块只做纯数据判定，不读文件、不访问仓储。

对应验收项见 `docs/p2-verification.md` 的 S6、S7。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional

from dayu.fins.pipelines.json_types import JsonValue

# 判定结论：可问答 / 不可问答。
MATERIAL_READY: str = "ready"
MATERIAL_NOT_READY: str = "not_ready"

# 不可问答原因码。
REASON_EMPTY_TEXT: str = "empty_text"
REASON_GARBLED_TEXT: str = "garbled_text"
REASON_PAGE_ORDER_BROKEN: str = "page_order_broken"

# 乱码判定：替换字符占比超过该阈值即判为乱码。
_GARBLED_REPLACEMENT_CHAR: str = "\ufffd"
_GARBLED_RATIO_THRESHOLD: float = 0.05


@dataclass(frozen=True)
class MaterialQuality:
    """材料可问答判定结果。

    Attributes:
        ready: 是否可作为可问答材料。
        reason: 判定原因码；可问答时为 `ready`。
        text_chars: 有效正文字符数（去除首尾空白后）。
    """

    ready: bool
    reason: str
    text_chars: int


def _first_page_no(prov: JsonValue) -> Optional[int]:
    """从 `prov` 中取首个页号。

    Args:
        prov: Docling 的 provenance 字段，通常是字典列表。

    Returns:
        首个整数页号；缺失或非法时返回 `None`。

    Raises:
        无。
    """

    if not isinstance(prov, list):
        return None
    for entry in prov:
        if not isinstance(entry, dict):
            continue
        page_no = entry.get("page_no")
        if isinstance(page_no, int):
            return page_no
    return None


def _iter_text_items(payload: Mapping[str, JsonValue]) -> list[tuple[str, Optional[int]]]:
    """从 Docling 导出字典中取出正文条目与页号。

    Args:
        payload: Docling `export_to_dict()` 结果。

    Returns:
        `(正文, 页号)` 列表；页号缺失时为 `None`。

    Raises:
        无。
    """

    raw_texts = payload.get("texts")
    if not isinstance(raw_texts, list):
        return []
    items: list[tuple[str, Optional[int]]] = []
    for raw_item in raw_texts:
        if not isinstance(raw_item, dict):
            continue
        text = raw_item.get("text")
        if not isinstance(text, str):
            continue
        items.append((text, _first_page_no(raw_item.get("prov"))))
    return items


def assess_docling_payload(payload: Mapping[str, JsonValue]) -> MaterialQuality:
    """判定转换结果是否可作为可问答材料。

    判定顺序：正文为空 → 乱码 → 页序损坏；命中即判不可问答。

    Args:
        payload: Docling `export_to_dict()` 结果。

    Returns:
        判定结果；不可问答时 `reason` 为具体原因码。

    Raises:
        无。
    """

    items = _iter_text_items(payload)
    joined = "\n".join(text for text, _ in items).strip()
    text_chars = len(joined)
    if text_chars == 0:
        return MaterialQuality(ready=False, reason=REASON_EMPTY_TEXT, text_chars=0)

    replacement_count = joined.count(_GARBLED_REPLACEMENT_CHAR)
    if replacement_count / text_chars > _GARBLED_RATIO_THRESHOLD:
        return MaterialQuality(ready=False, reason=REASON_GARBLED_TEXT, text_chars=text_chars)

    max_page = 0
    for _, page_no in items:
        if page_no is None:
            continue
        if page_no < max_page:
            return MaterialQuality(ready=False, reason=REASON_PAGE_ORDER_BROKEN, text_chars=text_chars)
        max_page = page_no

    return MaterialQuality(ready=True, reason=MATERIAL_READY, text_chars=text_chars)


def assess_conversion_reason(payload: Mapping[str, JsonValue]) -> Optional[str]:
    """把质量判定压缩为“不可问答原因”。

    Args:
        payload: Docling `export_to_dict()` 结果。

    Returns:
        可问答时返回 `None`；不可问答时返回具体原因码。

    Raises:
        无。
    """

    quality = assess_docling_payload(payload)
    return None if quality.ready else quality.reason
