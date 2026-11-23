"""电话会议纪要批量上传适配器测试。

覆盖 `docs/p2-verification.md` 的 U8、S1–S2、S4。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from dayu.fins.domain.enums import SourceKind
from dayu.fins.pipelines.docling_upload_service import (
    DoclingUploadService,
    build_upload_source_fingerprint,
)
from dayu.fins.pipelines.earnings_call_batch import (
    DECISION_CONFLICT_NEW_MATERIAL,
    DECISION_REUSE,
    ITEM_STATUS_FAILED,
    ITEM_STATUS_REUSED,
    ITEM_STATUS_UPLOADED,
    BatchFileRequest,
    BatchPlanItem,
    build_batch_plan,
    execute_batch,
)
from dayu.fins.pipelines.earnings_call_upload_adapter import DoclingEarningsCallUploader
from tests.fins.storage_testkit import FsStorageTestContext, build_fs_storage_test_context

TICKER = "AAPL"


def _convert_stub(raw_data: bytes, stream_name: str) -> dict[str, Any]:
    """返回固定转换结果；文件名含 bad 时抛出异常。

    Args:
        raw_data: 原始字节（占位）。
        stream_name: 文件名。

    Returns:
        固定结构化结果。

    Raises:
        RuntimeError: 文件名含 `bad` 时抛出，用于模拟单项转换失败。
    """

    _ = raw_data
    if "bad" in stream_name:
        raise RuntimeError("convert failed")
    return {"name": stream_name, "source": "docling", "texts": [{"text": "Revenue grew during Q4."}]}


def _build_context(tmp_path: Path) -> tuple[FsStorageTestContext, DoclingUploadService, DoclingEarningsCallUploader]:
    """构建带真实文件系统仓储的上传上下文。

    Args:
        tmp_path: 临时目录。

    Returns:
        `(storage_context, service, uploader)` 三元组。

    Raises:
        OSError: 仓储初始化失败时抛出。
    """

    context = build_fs_storage_test_context(tmp_path)
    service = DoclingUploadService(
        source_repository=context.source_repository,
        blob_repository=context.blob_repository,
        convert_with_docling=_convert_stub,
    )
    uploader = DoclingEarningsCallUploader(
        service=service,
        repository=context.source_repository,
        ticker=TICKER,
    )
    return context, service, uploader


def _write(path: Path, text: str) -> Path:
    """写入测试文件。

    Args:
        path: 目标路径。
        text: 文件内容。

    Returns:
        写入后的路径。

    Raises:
        OSError: 写入失败时抛出。
    """

    path.write_text(text, encoding="utf-8")
    return path


def _request(path: Path, name: str, *, year: int = 2023, period: str = "Q4") -> BatchFileRequest:
    """构造批量请求。

    Args:
        path: 本地文件。
        name: 材料名。
        year: 财年。
        period: 财季。

    Returns:
        批量请求对象。

    Raises:
        无。
    """

    return BatchFileRequest(
        path=path,
        material_name=name,
        fiscal_year=year,
        fiscal_period=period,
        content_fingerprint=build_upload_source_fingerprint([path]),
    )


def test_u8_slides_and_transcript_are_registered_as_separate_materials(tmp_path: Path) -> None:
    """U8：同一电话会的 slides 与 transcript 分别登记为独立材料。"""
    context, _, uploader = _build_context(tmp_path)
    slides = _write(tmp_path / "2023Q4_slides.md", "slides body")
    transcript = _write(tmp_path / "2023Q4_transcript.md", "transcript body")
    requests = [_request(slides, "2023Q4 Call Slides"), _request(transcript, "2023Q4 Call Transcript")]

    plans = build_batch_plan(requests=requests, existing_by_id={})
    result = execute_batch(plans=plans, uploader=uploader)

    assert [item.status for item in result.items] == [ITEM_STATUS_UPLOADED, ITEM_STATUS_UPLOADED]
    assert plans[0].document_id != plans[1].document_id
    for plan in plans:
        meta = context.source_repository.get_source_meta(TICKER, plan.document_id, SourceKind.MATERIAL)
        assert meta["ingest_complete"] is True
        assert meta["source_fingerprint"] == plan.request.content_fingerprint


def test_s1_single_conversion_failure_is_isolated(tmp_path: Path) -> None:
    """S1：批次中间一项转换失败，其余项仍完成上传。"""
    context, _, uploader = _build_context(tmp_path)
    good_one = _write(tmp_path / "call-a.md", "a")
    bad = _write(tmp_path / "bad-call.md", "bad")
    good_two = _write(tmp_path / "call-c.md", "c")
    requests = [
        _request(good_one, "Call A"),
        _request(bad, "Call B"),
        _request(good_two, "Call C"),
    ]

    plans = build_batch_plan(requests=requests, existing_by_id={})
    result = execute_batch(plans=plans, uploader=uploader)

    assert [item.status for item in result.items] == [
        ITEM_STATUS_UPLOADED,
        ITEM_STATUS_FAILED,
        ITEM_STATUS_UPLOADED,
    ]
    assert result.items[1].error is not None and "convert failed" in result.items[1].error
    uploaded = context.source_repository.get_source_meta(TICKER, plans[0].document_id, SourceKind.MATERIAL)
    failed_meta = None
    try:
        failed_meta = context.source_repository.get_source_meta(TICKER, plans[1].document_id, SourceKind.MATERIAL)
    except FileNotFoundError:
        failed_meta = None
    assert uploaded["ingest_complete"] is True
    assert failed_meta is None


def test_s2_reupload_same_content_is_reused(tmp_path: Path) -> None:
    """S2：相同内容重传时判定为复用，不再落一份新材料。"""
    _, _, uploader = _build_context(tmp_path)
    path = _write(tmp_path / "call.md", "same body")
    request = _request(path, "2023Q4 Call")
    first_plans = build_batch_plan(requests=[request], existing_by_id={})
    execute_batch(plans=first_plans, uploader=uploader)

    existing = uploader.read_existing([first_plans[0].document_id])
    second_request = _request(path, "2023Q4 Call")
    second_plans = build_batch_plan(requests=[second_request], existing_by_id=existing)
    result = execute_batch(plans=second_plans, uploader=uploader)

    assert second_plans[0].decision == DECISION_REUSE
    assert result.items[0].status == ITEM_STATUS_REUSED


def test_s4_conflict_keeps_old_material_and_adds_new_one(tmp_path: Path) -> None:
    """S4：同名同季内容不同时保留旧件并另存独立材料。"""
    context, _, uploader = _build_context(tmp_path)
    original_path = _write(tmp_path / "call.md", "original body")
    original_request = _request(original_path, "2023Q4 Call")
    first_plans = build_batch_plan(requests=[original_request], existing_by_id={})
    execute_batch(plans=first_plans, uploader=uploader)
    original_id = first_plans[0].document_id
    original_meta = context.source_repository.get_source_meta(TICKER, original_id, SourceKind.MATERIAL)

    new_path = _write(tmp_path / "call-v2.md", "revised body")
    new_request = _request(new_path, "2023Q4 Call")
    existing = uploader.read_existing([original_id])
    plans = build_batch_plan(requests=[new_request], existing_by_id=existing)
    result = execute_batch(plans=plans, uploader=uploader)

    assert plans[0].decision == DECISION_CONFLICT_NEW_MATERIAL
    assert plans[0].document_id != original_id
    assert result.items[0].status == ITEM_STATUS_UPLOADED
    untouched = context.source_repository.get_source_meta(TICKER, original_id, SourceKind.MATERIAL)
    assert untouched["source_fingerprint"] == original_meta["source_fingerprint"]
    created = context.source_repository.get_source_meta(TICKER, plans[0].document_id, SourceKind.MATERIAL)
    assert created["source_fingerprint"] == new_request.content_fingerprint
    assert str(created["material_name"]).endswith("(2)")


def test_uploader_rejects_blank_ticker(tmp_path: Path) -> None:
    """空 ticker 在构造阶段被拒绝。"""
    context, service, _ = _build_context(tmp_path)
    try:
        DoclingEarningsCallUploader(service=service, repository=context.source_repository, ticker="  ")
    except ValueError as error:
        assert "ticker" in str(error)
    else:  # pragma: no cover - 防御性分支
        raise AssertionError("空 ticker 未被拒绝")


def test_s5_same_quarter_different_call_is_not_treated_as_revision(tmp_path: Path) -> None:
    """S5：同季不同电话会各自登记，不覆盖旧件。"""
    context, _, uploader = _build_context(tmp_path)
    first_path = _write(tmp_path / "2023Q4_main.md", "main call body")
    second_path = _write(tmp_path / "2023Q4_followup.md", "follow-up call body")
    first_request = _request(first_path, "2023Q4 Q&A Call")
    second_request = _request(second_path, "2023Q4 Follow-up Call")

    first_plans = build_batch_plan(requests=[first_request], existing_by_id={})
    execute_batch(plans=first_plans, uploader=uploader)
    first_meta = context.source_repository.get_source_meta(
        TICKER, first_plans[0].document_id, SourceKind.MATERIAL
    )

    existing = uploader.read_existing([first_plans[0].document_id])
    second_plans = build_batch_plan(requests=[second_request], existing_by_id=existing)
    execute_batch(plans=second_plans, uploader=uploader)

    assert second_plans[0].document_id != first_plans[0].document_id
    untouched = context.source_repository.get_source_meta(
        TICKER, first_plans[0].document_id, SourceKind.MATERIAL
    )
    assert untouched["source_fingerprint"] == first_meta["source_fingerprint"]
    created = context.source_repository.get_source_meta(
        TICKER, second_plans[0].document_id, SourceKind.MATERIAL
    )
    assert created["ingest_complete"] is True
