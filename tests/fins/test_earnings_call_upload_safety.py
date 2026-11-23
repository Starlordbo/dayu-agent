"""会议纪要默认上传链路的覆盖拒绝、冲突保护与幂等性测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pytest_mock import MockerFixture

from dayu.fins.domain.document_models import SourceHandle
from dayu.fins.domain.enums import SourceKind
from dayu.fins.pipelines import docling_upload_service
from dayu.fins.pipelines.cn_pipeline import CnPipeline
from dayu.fins.pipelines.docling_upload_service import (
    DoclingUploadService,
    UploadOperationResult,
    build_material_ids,
)
from dayu.fins.pipelines.json_types import JsonValue
from dayu.fins.pipelines.sec_pipeline import SecPipeline
from dayu.fins.processors.registry import build_fins_processor_registry
from tests.fins.storage_testkit import FsStorageTestContext, build_fs_storage_test_context

TICKER = "AAPL"
FORM_TYPE = "EARNINGS_CALL"
MATERIAL_NAME = "2023Q4 Call"
FISCAL_YEAR = 2023
FISCAL_PERIOD = "Q4"
ORIGINAL_NAME = "call.md"
DOCLING_NAME = "call_docling.json"
DOCUMENT_ID, INTERNAL_DOCUMENT_ID = build_material_ids(
    form_type=FORM_TYPE,
    material_name=MATERIAL_NAME,
    fiscal_year=FISCAL_YEAR,
    fiscal_period=FISCAL_PERIOD,
)


def _convert_text(raw_data: bytes, stream_name: str) -> dict[str, JsonValue]:
    """把源正文转换为最小有效 Docling 正文，保留输入内容差异。

    Args:
        raw_data: UTF-8 源正文。
        stream_name: 原始文件名。

    Returns:
        带正文和文件名的转换结果；空文件产生空正文。

    Raises:
        UnicodeDecodeError: 输入不是 UTF-8 文本时抛出。
    """

    return {"name": stream_name, "texts": [{"text": raw_data.decode("utf-8")}]}


@pytest.fixture
def context(tmp_path: Path) -> FsStorageTestContext:
    """构建测试使用的真实文件系统仓储。

    Args:
        tmp_path: 临时工作区。

    Returns:
        共享同一存储根目录的仓储上下文。

    Raises:
        OSError: 仓储初始化失败时抛出。
    """

    return build_fs_storage_test_context(tmp_path)


@pytest.fixture
def service(context: FsStorageTestContext) -> DoclingUploadService:
    """创建保留默认质量策略、只替换转换器的上传服务。

    Args:
        context: 真实仓储上下文。

    Returns:
        默认上传服务。

    Raises:
        ValueError: 服务初始化参数非法时抛出。
    """

    return DoclingUploadService(
        source_repository=context.source_repository,
        blob_repository=context.blob_repository,
        convert_with_docling=_convert_text,
    )


def _upload(
    service: DoclingUploadService,
    path: Path,
    *,
    action: str = "create",
    overwrite: bool = False,
    form_type: str = FORM_TYPE,
) -> UploadOperationResult:
    """以同一身份提交上传请求。

    Args:
        service: 上传服务。
        path: 源文件。
        action: 上传动作。
        overwrite: 是否请求覆盖。
        form_type: 材料类型。

    Returns:
        上传结果。

    Raises:
        ValueError: 覆盖策略不允许时抛出。
        FileExistsError: 已有身份与新内容冲突时抛出。
        RuntimeError: 转换质量不合格时抛出。
        OSError: 源文件或仓储访问失败时抛出。
    """

    return service.execute_upload(
        ticker=TICKER,
        source_kind=SourceKind.MATERIAL,
        action=action,
        document_id=DOCUMENT_ID,
        internal_document_id=INTERNAL_DOCUMENT_ID,
        form_type=form_type,
        files=[path],
        overwrite=overwrite,
        meta={"material_name": MATERIAL_NAME, "ingest_method": "upload"},
    )


def _snapshot(context: FsStorageTestContext, ticker: str = TICKER) -> tuple[str, bytes, bytes]:
    """从仓储读取元数据、原文件与转换文件的完整快照。

    Args:
        context: 仓储上下文。
        ticker: 标的代码。

    Returns:
        排序后的元数据 JSON、原文件字节、转换文件字节。

    Raises:
        OSError: 材料不存在或读取失败时抛出。
    """

    handle = SourceHandle(ticker=ticker, document_id=DOCUMENT_ID, source_kind=SourceKind.MATERIAL.value)
    return (
        json.dumps(context.source_repository.get_source_meta(ticker, DOCUMENT_ID, SourceKind.MATERIAL), sort_keys=True),
        context.blob_repository.read_file_bytes(handle, ORIGINAL_NAME),
        context.blob_repository.read_file_bytes(handle, DOCLING_NAME),
    )


@pytest.mark.parametrize("action", ["create", "update", "delete"])
@pytest.mark.parametrize("existing", [False, True])
def test_overwrite_is_rejected_before_any_mutation(
    tmp_path: Path,
    context: FsStorageTestContext,
    service: DoclingUploadService,
    mocker: MockerFixture,
    action: str,
    existing: bool,
) -> None:
    """覆盖请求无论目标是否存在都在任何写入或删除前拒绝。

    Args:
        tmp_path: 临时目录。
        context: 仓储上下文。
        service: 默认上传服务。
        mocker: 仓储调用观察器。
        action: 请求动作。
        existing: 是否预先存入同身份材料。

    Returns:
        无。

    Raises:
        AssertionError: 覆盖未被拒绝或产生了存储副作用时抛出。
    """

    path = tmp_path / ORIGINAL_NAME
    path.write_text("原会议正文", encoding="utf-8")
    if existing:
        _upload(service, path)
    before = _snapshot(context) if existing else None
    store = mocker.spy(context.blob_repository, "store_file")
    create = mocker.spy(context.source_repository, "create_source_document")
    update = mocker.spy(context.source_repository, "update_source_document")
    delete = mocker.spy(context.source_repository, "delete_source_document")
    path.unlink()

    with pytest.raises(ValueError, match="EARNINGS_CALL.*overwrite=True.*material_name"):
        _upload(service, path, action=action, overwrite=True, form_type=" earnings_call ")

    store.assert_not_called()
    create.assert_not_called()
    update.assert_not_called()
    delete.assert_not_called()
    if existing:
        assert _snapshot(context) == before
    else:
        assert context.source_repository.list_source_document_ids(TICKER, SourceKind.MATERIAL) == []


@pytest.mark.parametrize("action", ["create", "update"])
@pytest.mark.parametrize("same_filename", [False, True])
def test_conflict_preserves_original_blobs_and_meta(
    tmp_path: Path,
    context: FsStorageTestContext,
    service: DoclingUploadService,
    mocker: MockerFixture,
    action: str,
    same_filename: bool,
) -> None:
    """同身份异内容在转换和写入前拒绝，既不覆盖也不自动生成新身份。

    Args:
        tmp_path: 临时目录。
        context: 仓储上下文。
        service: 默认上传服务。
        mocker: 副作用观察器。
        action: 显式上传动作。
        same_filename: 是否使用与原件相同的文件名。

    Returns:
        无。

    Raises:
        AssertionError: 原资产变化、产生新材料或错误信息不明确时抛出。
    """

    path = tmp_path / ORIGINAL_NAME
    path.write_text("原会议正文", encoding="utf-8")
    _upload(service, path)
    before = _snapshot(context)
    convert = mocker.spy(service, "_convert_with_docling")
    store = mocker.spy(context.blob_repository, "store_file")
    create = mocker.spy(context.source_repository, "create_source_document")
    update = mocker.spy(context.source_repository, "update_source_document")
    revised_path = path if same_filename else tmp_path / "revised.md"
    revised_path.write_text("更改后的会议正文", encoding="utf-8")

    with pytest.raises(FileExistsError, match="同身份.*内容不同.*material_name.*另存"):
        _upload(service, revised_path, action=action)

    convert.assert_not_called()
    store.assert_not_called()
    create.assert_not_called()
    update.assert_not_called()
    assert _snapshot(context) == before
    assert context.source_repository.list_source_document_ids(TICKER, SourceKind.MATERIAL) == [DOCUMENT_ID]


@pytest.mark.parametrize("action", ["create", "update"])
def test_same_fingerprint_skips_without_conversion_or_writes(
    tmp_path: Path,
    context: FsStorageTestContext,
    service: DoclingUploadService,
    mocker: MockerFixture,
    action: str,
) -> None:
    """完整入库的同指纹材料保持跳过语义与原资产不变。

    Args:
        tmp_path: 临时目录。
        context: 仓储上下文。
        service: 默认上传服务。
        mocker: 副作用观察器。
        action: 显式上传动作。

    Returns:
        无。

    Raises:
        AssertionError: 重传未跳过或产生写入时抛出。
    """

    path = tmp_path / ORIGINAL_NAME
    path.write_text("原会议正文", encoding="utf-8")
    _upload(service, path)
    before = _snapshot(context)
    convert = mocker.spy(service, "_convert_with_docling")
    store = mocker.spy(context.blob_repository, "store_file")
    create = mocker.spy(context.source_repository, "create_source_document")
    update = mocker.spy(context.source_repository, "update_source_document")

    result = _upload(service, path, action=action)

    assert result.status == "skipped"
    assert result.payload["skip_reason"] == "already_uploaded"
    assert [event.event_type for event in result.file_events] == ["file_skipped"]
    convert.assert_not_called()
    store.assert_not_called()
    create.assert_not_called()
    update.assert_not_called()
    assert _snapshot(context) == before


@pytest.mark.parametrize(("pipeline_type", "ticker"), [(SecPipeline, TICKER), (CnPipeline, "000001.SZ")])
def test_default_upload_material_enforces_quality_skip_and_conflict(
    tmp_path: Path,
    context: FsStorageTestContext,
    monkeypatch: pytest.MonkeyPatch,
    pipeline_type: type[SecPipeline] | type[CnPipeline],
    ticker: str,
) -> None:
    """SEC/CN 真实同步上传入口默认执行质量门、幂等跳过和冲突保护。

    Args:
        tmp_path: 临时目录。
        context: 真实仓储上下文。
        monkeypatch: 仅替换外部 Docling 转换函数的工具。
        pipeline_type: 真实 SEC 或 CN 管线类。
        ticker: 对应市场的标的代码。

    Returns:
        无。

    Raises:
        AssertionError: 默认入口缺少质量门、错误覆盖或未复用原材料时抛出。
    """

    monkeypatch.setattr(docling_upload_service, "_convert_bytes_with_docling", _convert_text)
    pipeline = pipeline_type(
        workspace_root=tmp_path,
        processor_registry=build_fins_processor_registry(),
        company_repository=context.company_repository,
        source_repository=context.source_repository,
        blob_repository=context.blob_repository,
    )
    path = tmp_path / ORIGINAL_NAME
    before: tuple[str, bytes, bytes] | None = None
    for body, expected_status in [("", "failed"), ("原会议正文", "ok"), ("原会议正文", "skipped"), ("修订正文", "failed")]:
        path.write_text(body, encoding="utf-8")
        result = pipeline.upload_material(
            ticker=ticker,
            action=None,
            form_type=FORM_TYPE,
            material_name=MATERIAL_NAME,
            files=[path],
            fiscal_year=FISCAL_YEAR,
            fiscal_period=FISCAL_PERIOD,
            company_name="测试公司",
            overwrite=False,
        )
        assert result["status"] == expected_status
        if not body:
            assert "empty_text" in result["message"]
            assert context.source_repository.list_source_document_ids(ticker, SourceKind.MATERIAL) == []
        elif expected_status == "failed":
            assert "material_name" in result["message"]
            assert _snapshot(context, ticker) == before
            assert context.source_repository.list_source_document_ids(ticker, SourceKind.MATERIAL) == [DOCUMENT_ID]
        elif expected_status == "ok":
            before = _snapshot(context, ticker)
