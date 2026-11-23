"""DoclingUploadService 转换质量否决测试。

验证 S6：转换返回成功但正文为空、或多段页序损坏时，材料被拒绝入库，
不会留下 `ingest_complete=True` 的坏材料。
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

import pytest
from pytest_mock import MockerFixture

from dayu.fins.domain.document_models import SourceHandle
from dayu.fins.domain.enums import SourceKind
from dayu.fins.pipelines.docling_upload_service import ConversionAssessor, DoclingUploadService
from dayu.fins.pipelines.earnings_call_quality import assess_conversion_reason
from dayu.fins.pipelines.json_types import JsonValue
from tests.fins.storage_testkit import FsStorageTestContext, build_fs_storage_test_context

TICKER = "AAPL"
DOCUMENT_ID = "mat_quality_gate"
EARNINGS_CALL = "EARNINGS_CALL"
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


def _convert_garbled(_: bytes, __: str) -> dict[str, JsonValue]:
    """返回正文乱码的转换结果。

    Args:
        _: 原始字节占位。
        __: 文件名占位。

    Returns:
        包含替换字符的 Docling 正文。

    Raises:
        无。
    """

    return {"texts": [{"text": "\ufffd\ufffd\ufffd管理层"}]}


def _convert_reversed_pages(_: bytes, __: str) -> dict[str, JsonValue]:
    """返回页序回退的转换结果。

    Args:
        _: 原始字节占位。
        __: 文件名占位。

    Returns:
        段落页码从第二页退回第一页的 Docling 正文。

    Raises:
        无。
    """

    return {"texts": [
        {"text": "管理层回答", "prov": [{"page_no": 2}]},
        {"text": "分析师提问", "prov": [{"page_no": 1}]},
    ]}


def _build_service(
    tmp_path: Path,
    *,
    convert: Converter,
    assess: ConversionAssessor | None = None,
) -> tuple[FsStorageTestContext, DoclingUploadService]:
    """构建默认或显式指定质量否决回调的上传服务。

    Args:
        tmp_path: 临时目录。
        convert: 转换桩函数。
        assess: 显式质量回调；None 使用服务默认策略。

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
        assess_docling=assess,
    )
    return context, service


def _upload(service: DoclingUploadService, path: Path, form_type: str = EARNINGS_CALL) -> None:
    """执行一次材料上传。

    Args:
        service: 上传服务。
        path: 上传文件。
        form_type: 材料类型。

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
        form_type=form_type,
        files=[path],
        overwrite=False,
        meta={"material_name": "2023Q4 Call", "ingest_method": "upload"},
    )


@pytest.mark.parametrize("assess", [None, assess_conversion_reason], ids=["default", "explicit"])
@pytest.mark.parametrize(
    ("convert", "reason"),
    [(_convert_empty, "empty_text"), (_convert_garbled, "garbled_text"),
     (_convert_reversed_pages, "page_order_broken")],
)
def test_bad_conversion_is_rejected_before_blob_and_upsert(
    tmp_path: Path,
    mocker: MockerFixture,
    convert: Converter,
    reason: str,
    assess: ConversionAssessor | None,
) -> None:
    """默认与显式质量门都在任何写入前拒绝三类坏正文。

    Args:
        tmp_path: 临时目录。
        mocker: 仓储调用观察器。
        convert: 坏正文转换函数。
        reason: 预期拒绝原因。
        assess: 可选显式质量回调。

    Returns:
        无。

    Raises:
        AssertionError: 坏材料被写入或未正确拒绝时抛出。
    """

    context, service = _build_service(tmp_path, convert=convert, assess=assess)
    store = mocker.spy(context.blob_repository, "store_file")
    create = mocker.spy(context.source_repository, "create_source_document")
    update = mocker.spy(context.source_repository, "update_source_document")
    file_path = tmp_path / "2023Q4_call.md"
    file_path.write_text("body", encoding="utf-8")

    with pytest.raises(RuntimeError, match=f"不可问答.*{reason}"):
        _upload(service, file_path)

    store.assert_not_called()
    create.assert_not_called()
    update.assert_not_called()
    handle = SourceHandle(ticker=TICKER, document_id=DOCUMENT_ID, source_kind=SourceKind.MATERIAL.value)
    assert context.blob_repository.list_entries(handle) == []
    with pytest.raises(FileNotFoundError):
        context.source_repository.get_source_meta(TICKER, DOCUMENT_ID, SourceKind.MATERIAL)


def test_ready_conversion_is_stored(tmp_path: Path) -> None:
    """正常正文通过默认质量门并完成入库。

    Args:
        tmp_path: 临时目录。

    Returns:
        无。

    Raises:
        AssertionError: 正常材料未完整入库时抛出。
    """
    context, service = _build_service(tmp_path, convert=_convert_ok)
    file_path = tmp_path / "2023Q4_call.md"
    file_path.write_text("body", encoding="utf-8")

    _upload(service, file_path)

    meta = context.source_repository.get_source_meta(TICKER, DOCUMENT_ID, SourceKind.MATERIAL)
    assert meta["ingest_complete"] is True


@pytest.mark.parametrize("form_type", ["MATERIAL_OTHER", "10-K", "CN_ANNUAL_REPORT"])
def test_other_form_types_keep_default_quality_behavior(tmp_path: Path, form_type: str) -> None:
    """其他类型不因会议纪要默认质量门而改变既有上传行为。

    Args:
        tmp_path: 临时目录。
        form_type: 非会议纪要类型。

    Returns:
        无。

    Raises:
        AssertionError: 其他类型的默认入库行为改变时抛出。
    """

    context, service = _build_service(tmp_path, convert=_convert_empty)
    file_path = tmp_path / "material.md"
    file_path.write_text("body", encoding="utf-8")

    _upload(service, file_path, form_type)

    meta = context.source_repository.get_source_meta(TICKER, DOCUMENT_ID, SourceKind.MATERIAL)
    assert meta["ingest_complete"] is True


def test_other_form_type_keeps_explicit_assessor(tmp_path: Path) -> None:
    """其他类型仍执行调用方显式注入的质量门。

    Args:
        tmp_path: 临时目录。

    Returns:
        无。

    Raises:
        AssertionError: 显式质量门失效时抛出。
    """

    _, service = _build_service(tmp_path, convert=_convert_empty, assess=assess_conversion_reason)
    file_path = tmp_path / "material.md"
    file_path.write_text("body", encoding="utf-8")

    with pytest.raises(RuntimeError, match="empty_text"):
        _upload(service, file_path, "MATERIAL_OTHER")
