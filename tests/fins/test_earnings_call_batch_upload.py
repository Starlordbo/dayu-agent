"""电话会议纪要批量上传调度测试。

覆盖 `docs/p2-verification.md` 的 S1–S4 与 S8。
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import pytest

from dayu.fins.pipelines.earnings_call_batch import (
    DECISION_CONFLICT_NEW_MATERIAL,
    DECISION_CREATE,
    DECISION_RESUME_PROCESSING,
    DECISION_REUSE,
    ITEM_STATUS_FAILED,
    ITEM_STATUS_REUSED,
    ITEM_STATUS_UPLOADED,
    BatchFileRequest,
    BatchPlanItem,
    ExistingMaterial,
    build_batch_plan,
    execute_batch,
)


class _UploaderStub:
    """记录调用并按需抛错的上传器桩。"""

    def __init__(self, *, fail_on_document_id: Optional[str] = None) -> None:
        """初始化桩。

        Args:
            fail_on_document_id: 命中该 document_id 时抛出异常，模拟单项失败。

        Returns:
            无。

        Raises:
            无。
        """
        self.called: list[BatchPlanItem] = []
        self._fail_on = fail_on_document_id

    def upload_one(self, item: BatchPlanItem) -> None:
        """记录一次上传调用。

        Args:
            item: 决策结果。

        Returns:
            无。

        Raises:
            RuntimeError: 命中预设失败项时抛出。
        """
        self.called.append(item)
        if self._fail_on is not None and item.document_id == self._fail_on:
            raise RuntimeError("docling 转换失败")


def _request(name: str, fingerprint: str, *, year: int = 2023, period: str = "Q4") -> BatchFileRequest:
    """构造批量请求。

    Args:
        name: 材料名。
        fingerprint: 内容指纹。
        year: 财年。
        period: 财季。

    Returns:
        批量请求对象。

    Raises:
        无。
    """
    return BatchFileRequest(
        path=Path(f"/tmp/{name}.txt"),
        material_name=name,
        fiscal_year=year,
        fiscal_period=period,
        content_fingerprint=fingerprint,
    )


def test_s1_single_failure_does_not_break_batch() -> None:
    """S1：批次中间一项失败，其余项继续完成。"""
    requests = [_request("call-a", "fp-a"), _request("call-b", "fp-b"), _request("call-c", "fp-c")]
    plans = build_batch_plan(requests=requests, existing_by_id={})
    failed_id = plans[1].document_id
    uploader = _UploaderStub(fail_on_document_id=failed_id)

    result = execute_batch(plans=plans, uploader=uploader)

    assert [item.status for item in result.items] == [
        ITEM_STATUS_UPLOADED,
        ITEM_STATUS_FAILED,
        ITEM_STATUS_UPLOADED,
    ]
    assert result.items[1].error is not None and "RuntimeError" in result.items[1].error
    assert len(uploader.called) == 3


def test_s2_same_fingerprint_is_reused_without_upload() -> None:
    """S2：指纹一致且已入库时复用，不重复上传。"""
    request = _request("call-a", "fp-a")
    plans = build_batch_plan(requests=[request], existing_by_id={})
    existing_id = plans[0].document_id
    existing = {
        existing_id: ExistingMaterial(
            document_id=existing_id,
            source_fingerprint="fp-a",
            ingest_complete=True,
        )
    }

    replanned = build_batch_plan(requests=[request], existing_by_id=existing)
    uploader = _UploaderStub()
    result = execute_batch(plans=replanned, uploader=uploader)

    assert replanned[0].decision == DECISION_REUSE
    assert result.items[0].status == ITEM_STATUS_REUSED
    assert uploader.called == []


def test_s3_uploaded_but_unprocessed_resumes_processing() -> None:
    """S3：上传成功但处理失败时只恢复处理，不新建材料。"""
    request = _request("call-a", "fp-a")
    existing_id = build_batch_plan(requests=[request], existing_by_id={})[0].document_id
    existing = {
        existing_id: ExistingMaterial(
            document_id=existing_id,
            source_fingerprint="fp-a",
            ingest_complete=False,
        )
    }

    plans = build_batch_plan(requests=[request], existing_by_id=existing)
    uploader = _UploaderStub()
    result = execute_batch(plans=plans, uploader=uploader)

    assert plans[0].decision == DECISION_RESUME_PROCESSING
    assert plans[0].document_id == existing_id
    assert result.items[0].status == ITEM_STATUS_UPLOADED
    assert len(uploader.called) == 1


def test_s4_conflicting_content_keeps_old_material_and_creates_new_one() -> None:
    """S4：同名同季但内容不同时保留旧件，新件另存独立材料。"""
    request = _request("call-a", "fp-new")
    original_id = build_batch_plan(requests=[request], existing_by_id={})[0].document_id
    existing = {
        original_id: ExistingMaterial(
            document_id=original_id,
            source_fingerprint="fp-old",
            ingest_complete=True,
        )
    }

    plans = build_batch_plan(requests=[request], existing_by_id=existing)

    assert plans[0].decision == DECISION_CONFLICT_NEW_MATERIAL
    assert plans[0].document_id != original_id
    assert plans[0].material_name.endswith("(2)")
    assert existing[original_id].document_id == original_id


def test_s4_two_conflicts_get_distinct_ids() -> None:
    """S4 延伸：连续两份冲突内容各自得到独立 ID。"""
    request_first = _request("call-a", "fp-1")
    request_second = _request("call-a", "fp-2")
    original_id = build_batch_plan(requests=[request_first], existing_by_id={})[0].document_id
    existing = {
        original_id: ExistingMaterial(
            document_id=original_id,
            source_fingerprint="fp-old",
            ingest_complete=True,
        )
    }

    plans = build_batch_plan(requests=[request_first, request_second], existing_by_id=existing)

    assert plans[0].decision == DECISION_CONFLICT_NEW_MATERIAL
    assert plans[1].decision == DECISION_CONFLICT_NEW_MATERIAL
    assert plans[0].document_id != plans[1].document_id
    assert plans[0].material_name.endswith("(2)")
    assert plans[1].material_name.endswith("(3)")


def test_s8_destructive_overwrite_is_rejected() -> None:
    """S8：首版拒绝破坏性覆盖。"""
    with pytest.raises(ValueError, match="破坏性覆盖"):
        build_batch_plan(requests=[_request("call-a", "fp-a")], existing_by_id={}, overwrite=True)


def test_new_material_gets_create_decision_and_stable_id() -> None:
    """新文件走 create，且同一输入重复规划得到同一 ID。"""
    request = _request("call-a", "fp-a")

    first = build_batch_plan(requests=[request], existing_by_id={})
    second = build_batch_plan(requests=[request], existing_by_id={})

    assert first[0].decision == DECISION_CREATE
    assert first[0].document_id == second[0].document_id


def test_same_batch_duplicate_file_is_reused_after_first_upload() -> None:
    """同一批次内重复文件只上传一次，后续按复用处理。"""
    request = _request("call-a", "fp-a")
    plans = build_batch_plan(requests=[request, request], existing_by_id={})

    assert plans[0].decision == DECISION_CREATE
    assert plans[1].decision == DECISION_REUSE


def test_blank_material_name_is_rejected() -> None:
    """材料名为空白时拒绝规划。"""
    with pytest.raises(ValueError, match="材料名不能为空"):
        build_batch_plan(requests=[_request("   ", "fp-a")], existing_by_id={})
