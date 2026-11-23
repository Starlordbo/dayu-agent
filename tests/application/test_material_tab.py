"""电话会议纪要上传 Tab 纯函数测试。

覆盖 `docs/p2-verification.md` 的 W1、W2（结果映射）、W3、W4。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dayu.contracts.fins import (
    FinsCommandName,
    UploadMaterialCommandPayload,
    UploadMaterialResultData,
)
from dayu.web.streamlit.pages.material_tab import (
    STATUS_FAILED,
    STATUS_SUBMITTED,
    MaterialUploadEntry,
    apply_result,
    batch_key,
    build_material_entries,
    build_upload_request,
)


def _entry(
    *,
    file_name: str = "2023Q4_call.md",
    material_name: str = "2023Q4_call",
    fiscal_year: int | None = 2023,
    fiscal_period: str | None = "Q4",
    needs_confirmation: bool = False,
    reason: str = "declaration",
) -> MaterialUploadEntry:
    """构造条目，默认值可被覆盖。

    Args:
        file_name: 文件名。
        material_name: 材料名。
        fiscal_year: 财年。
        fiscal_period: 财季。
        needs_confirmation: 是否需要确认。
        reason: 原因码。

    Returns:
        条目对象。

    Raises:
        无。
    """

    return MaterialUploadEntry(
        file_name=file_name,
        material_name=material_name,
        fiscal_year=fiscal_year,
        fiscal_period=fiscal_period,
        needs_confirmation=needs_confirmation,
        reason=reason,
    )


def test_w1_each_file_gets_an_independent_entry() -> None:
    """W1：多文件逐份产出条目，互不影响。"""
    entries = build_material_entries(["2023Q4_call.md", "2024Q1_call.txt", "notes.md"])

    assert len(entries) == 3
    assert (entries[0].fiscal_year, entries[0].fiscal_period) == (2023, "Q4")
    assert (entries[1].fiscal_year, entries[1].fiscal_period) == (2024, "Q1")
    assert entries[2].needs_confirmation is True
    assert entries[2].fiscal_year is None


def test_w3_batch_key_is_stable_and_order_insensitive() -> None:
    """W3：批次指纹与顺序无关，内容变化会产生新指纹。"""
    first = batch_key(file_names=["a.md", "b.md"], sizes=[10, 20])
    reordered = batch_key(file_names=["b.md", "a.md"], sizes=[20, 10])
    changed = batch_key(file_names=["a.md", "b.md"], sizes=[10, 21])

    assert first == reordered
    assert first != changed


def test_w3_mismatched_lengths_are_rejected() -> None:
    """W3：文件名与大小数量不一致时拒绝计算。"""
    with pytest.raises(ValueError, match="数量不一致"):
        batch_key(file_names=["a.md"], sizes=[])


def test_w4_upload_request_builds_upload_material_command() -> None:
    """W4：请求使用 upload_material 命令且不带覆盖。"""
    entry = _entry()
    request = build_upload_request(
        ticker=" aapl ",
        entry=entry,
        file_path=Path("/tmp/2023Q4_call.md"),
        fiscal_year=2023,
        fiscal_period="q4",
    )

    command = request.command
    assert command.name == FinsCommandName.UPLOAD_MATERIAL
    assert command.stream is False
    payload = command.payload
    assert isinstance(payload, UploadMaterialCommandPayload)
    assert payload.ticker == "AAPL"
    assert payload.form_type == "EARNINGS_CALL"
    assert payload.material_name == "2023Q4_call"
    assert payload.fiscal_year == 2023
    assert payload.fiscal_period == "Q4"
    assert payload.overwrite is False
    assert payload.files == (Path("/tmp/2023Q4_call.md"),)


def test_w4_blank_ticker_is_rejected() -> None:
    """W4：空 ticker 拒绝构造请求。"""
    with pytest.raises(ValueError, match="ticker"):
        build_upload_request(
            ticker="  ",
            entry=_entry(),
            file_path=Path("/tmp/a.md"),
            fiscal_year=2023,
            fiscal_period="Q4",
        )


def _uploaded_data(status: str) -> UploadMaterialResultData:
    """构造上传结果数据。

    Args:
        status: 上传状态。

    Returns:
        结果数据。

    Raises:
        无。
    """

    return UploadMaterialResultData(
        pipeline="upload_material",
        status=status,
        ticker="AAPL",
        material_action="create",
    )


def test_w2_uploaded_result_maps_to_submitted() -> None:
    """W2：上传成功的文件映射为已提交。"""
    row = apply_result(entry=_entry(), data=_uploaded_data("uploaded"))

    assert row.status == STATUS_SUBMITTED
    assert row.file_name == "2023Q4_call.md"


def test_w2_failed_status_maps_to_failed() -> None:
    """W2：非 uploaded 状态映射为失败并带原因。"""
    row = apply_result(entry=_entry(), data=_uploaded_data("skipped"))

    assert row.status == STATUS_FAILED
    assert "skipped" in row.message
