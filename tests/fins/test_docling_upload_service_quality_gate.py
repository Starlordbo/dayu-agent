"""DoclingUploadService 转换质量否决测试。

验证 S6：转换返回成功但正文为空、或多段页序损坏时，材料被拒绝入库，
不会留下 `ingest_complete=True` 的坏材料。
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

import pytest

from dayu.fins.domain.enums import SourceKind
from dayu.fins.pipelines.docling_upload_service import DoclingUploadService
from dayu.fins.pipelines.earnings_call_quality import assess_conversion_reason
from dayu.fins.pipelines.json_types import JsonValue
from tests.fins.storage_testkit import FsStorageTestContext, build_fs_storage_test_context

TICKER = "AAPL"
DOCUMENT_ID = "mat_quality_gate"
Converter = Callable[[bytes, str], dict[str, JsonValue]]


def _convert_empty(_: bytes, __: str) -> dict[str, JsonValue]:
    """返回空正文的转换结果。

    Args:
        _: 原始字节（占位）。
        __: 文件名（占位）。

    Returns:
        无正文的 Docling 导出字典。

    Raises:
        无。
    """

    return {"texts": []}


def _convert_ok(_: bytes, __: str) -> dict[str, JsonValue]:
    """返回正常正文的转换结果。

    Args:
        _: 原始字节（占位）。
        __: 文件名（占位）。

    Returns:
        带正文与页号的 Docling 导出字典。

    Raises:
        无。
    """

    return {"texts": [{"text": "管理层回答", "prov": [{"page_no": 1}]}]}


def _build_service(
    tmp_path: Path,
    *,
    convert: Converter,
) -> tuple[FsStorageTestContext, DoclingUploadService]:
    """构建带质量否决回调的上传服务。

    Args:
        tmp_path: 临时目录。
        convert: 转换桩函数。

    Returns:
        `(storage_context, service)` 二元组。

    Raises:
        OSError: 仓储初始化失败时抛出。
    """

    context = build_fs_storage_test_context(tmp_path)
    service = DoclingUploadService(
        source_repository=context.source_repository,
        blob_repository=context.blob_repository,
        convert_with_docling=convert,
        assess_docling=assess_conversion_reason,
    )
    return context, service


def _upload(service: DoclingUploadService, path: Path) -> None:
    """执行一次材料上传。

    Args:
        service: 上传服务。
        path: 上传文件。

    Returns:
        无。

    Raises:
        RuntimeError: 质量否决时由服务抛出。
    """

    service.execute_upload(
        ticker=TICKER,
        source_kind=SourceKind.MATERIAL,
        action="create",
        document_id=DOCUMENT_ID,
        internal_document_id=DOCUMENT_ID,
        form_type="EARNINGS_CALL",
        files=[path],
        overwrite=False,
        meta={"material_name": "2023Q4 Call", "ingest_method": "upload"},
    )


def test_empty_conversion_is_rejected_before_upsert(tmp_path: Path) -> None:
    """空正文转换结果被拒绝，且不留下任何材料记录。"""
    context, service = _build_service(tmp_path, convert=_convert_empty)
    file_path = tmp_path / "2023Q4_call.md"
    file_path.write_text("body", encoding="utf-8")

    with pytest.raises(RuntimeError, match="不可问答"):
        _upload(service, file_path)

    with pytest.raises(FileNotFoundError):
        context.source_repository.get_source_meta(TICKER, DOCUMENT_ID, SourceKind.MATERIAL)


def test_ready_conversion_is_stored(tmp_path: Path) -> None:
    """正常正文转换结果正常入库。"""
    context, service = _build_service(tmp_path, convert=_convert_ok)
    file_path = tmp_path / "2023Q4_call.md"
    file_path.write_text("body", encoding="utf-8")

    _upload(service, file_path)

    meta = context.source_repository.get_source_meta(TICKER, DOCUMENT_ID, SourceKind.MATERIAL)
    assert meta["ingest_complete"] is True
