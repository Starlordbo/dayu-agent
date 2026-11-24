"""纪要上传页面的状态、隔离、恢复与真实命令边界测试。"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest

from dayu.contracts.fins import (
    FinsCommandName, FinsResult, ProcessSingleResultData,
    UploadMaterialCommandPayload, UploadMaterialResultData,
)
from dayu.fins.domain.document_models import FilingSummary
from dayu.fins.domain.document_reference import DocumentReference, DocumentReferenceView, DocumentSourceInfo
from dayu.services.contracts import FinsSubmission, FinsSubmitRequest
from dayu.web.streamlit.pages.material_tab import (
    STATUS_FAILED, STATUS_PENDING_CONFIRMATION, STATUS_PROCESSING, STATUS_READY,
    MaterialUploadEntry, MaterialUploadFile, MaterialUploadInput, MaterialUploadRow,
    apply_result, build_material_entries, build_upload_request,
    execute_material_input, material_input_key,
)


@dataclass
class RecordingService:
    """记录真实命令类型并允许注入处理失败的最小服务替身。"""

    fail_processing: bool = False
    upload_status: str = "ok"
    process_status: str = "processed"
    process_reason: str | None = None
    calls: list[FinsCommandName] = field(default_factory=list)
    requests: list[FinsSubmitRequest] = field(default_factory=list)
    materials: list[DocumentSourceInfo] = field(default_factory=list)

    def submit(self, request: FinsSubmitRequest) -> FinsSubmission:
        """记录调用并返回契约结果。

        Args:
            request: 页面提交请求。
        Returns:
            同步命令结果。
        Raises:
            RuntimeError: 注入的处理错误。
        """
        self.calls.append(request.command.name)
        self.requests.append(request)
        if request.command.name == FinsCommandName.UPLOAD_MATERIAL:
            data = UploadMaterialResultData(pipeline="sec", status=self.upload_status,
                                           ticker="AAPL", material_action="create", document_id="mat_test")
        else:
            if self.fail_processing:
                raise RuntimeError("injected processing failure")
            data = ProcessSingleResultData(pipeline="sec", action="process_material",
                                           status=self.process_status, reason=self.process_reason,
                                           ticker="AAPL", document_id="mat_test")
        return FinsSubmission(session_id="test", execution=FinsResult(command=request.command.name, data=data))

    def list_filings(self, ticker: str) -> list[FilingSummary]:
        """返回空列表。

        Args:
            ticker: 公司代码。
        Returns:
            空财报列表。
        Raises:
            无。
        """
        return []


    def list_materials(self, ticker: str) -> list[DocumentSourceInfo]:
        """返回显式配置且属于当前公司的材料目录。

        参数:
            ticker: 待查询公司代码。
        返回值:
            当前公司的测试材料列表副本。
        异常:
            无。
        """
        return [material for material in self.materials if material.ticker == ticker]

    def resolve_document_reference(self, request: DocumentReference) -> DocumentReferenceView:
        """阻止上传页测试意外读取未提供的原文。

        参数:
            request: 意外请求的原文定位。
        返回值:
            不返回；上传编排只需材料目录。
        异常:
            AssertionError: 测试错误地请求原文聚合时抛出。
        """
        raise AssertionError(f"上传编排不应解析原文：{request.document_id}")


def make_input(data: bytes = b"Q4 2023 Earnings Call") -> MaterialUploadInput:
    """构造已确认上传项。

    Args:
        data: 文件内容。
    Returns:
        上传输入。
    Raises:
        无。
    """
    return MaterialUploadInput(MaterialUploadEntry("2023Q4_call.md", "Call", 2023, "Q4", False, "declaration"), data)


def test_w1_independent_entries() -> None:
    """参数：无。返回：无。异常：断言失败；逐文件推断不合并。"""
    entries = build_material_entries([
        MaterialUploadFile(name, b"") for name in ("2023Q4_call.md", "2024Q1_call.txt", "notes.md")
    ])
    assert len(entries) == 3
    assert (entries[0].fiscal_year, entries[0].fiscal_period) == (2023, "Q4")
    assert entries[2].needs_confirmation


@pytest.mark.parametrize("suffix", ["md", "txt"])
def test_body_title_prefills_before_conflicting_filename(suffix: str) -> None:
    """参数：文本格式后缀。返回：无。异常：断言失败；标题优先且冲突需确认。"""
    entry = build_material_entries([
        MaterialUploadFile(f"2023Q4_call.{suffix}", b"# Q3 2023 Earnings Call\n\nWelcome everyone.")
    ])[0]
    assert entry.material_name == "Q3 2023 Earnings Call"
    assert (entry.fiscal_year, entry.fiscal_period) == (2023, "Q3")
    assert entry.inference_source == "title"
    assert entry.reason == "source_conflict" and entry.needs_confirmation


def test_opening_prefills_before_conflicting_title_and_filename() -> None:
    """参数：无。返回：无。异常：断言失败；开场独立于标题并拥有最高优先级。"""
    entry = build_material_entries([
        MaterialUploadFile("2023Q4_call.md", b"# Q3 2023 Earnings Call\n\nWelcome to Q2 2023 results.")
    ])[0]
    assert entry.material_name == "Q3 2023 Earnings Call"
    assert (entry.fiscal_year, entry.fiscal_period) == (2023, "Q2")
    assert entry.inference_source == "opening"
    assert entry.reason == "source_conflict" and entry.needs_confirmation


@pytest.mark.parametrize("name,data", [
    ("2023Q4_call.md", b""),
    ("2023Q4_call.txt", b"\xff\xfe\xfa"),
    ("2023Q4_call.docx", b"broken zip"),
    ("2023Q4_call.pdf", b"%PDF-1.7\nQ3 2023 Earnings Call"),
])
def test_unreadable_or_pdf_metadata_uses_filename(name: str, data: bytes) -> None:
    """参数：文件名及不可提取正文。返回：无。异常：断言失败；保持文件名退回语义。"""
    entry = build_material_entries([MaterialUploadFile(name, data)])[0]
    assert entry.material_name == "2023Q4_call"
    assert (entry.fiscal_year, entry.fiscal_period) == (2023, "Q4")
    assert entry.inference_source == "filename"
    assert entry.reason == "declaration" and not entry.needs_confirmation


def test_body_without_declaration_stays_unknown() -> None:
    """参数：无。返回：无。异常：断言失败；可读正文不等于识别出财季。"""
    entry = build_material_entries([
        MaterialUploadFile("notes.md", b"# Business Update\n\nWelcome everyone.")
    ])[0]
    assert entry.material_name == "Business Update"
    assert entry.fiscal_year is None and entry.fiscal_period is None
    assert entry.inference_source is None
    assert entry.reason == "no_declaration" and entry.needs_confirmation


def test_prefill_identity_tracks_title_and_period() -> None:
    """参数：无。返回：无。异常：断言失败；预填改变身份，编辑后键按新身份生成。"""
    file = MaterialUploadFile("2023Q4_call.md", b"# Q3 2023 Earnings Call")
    entry = build_material_entries([file])[0]
    item = MaterialUploadInput(entry, file.data)
    filename_entry = replace(entry, material_name="2023Q4_call", fiscal_period="Q4")
    assert material_input_key(ticker="AAPL", item=item) != material_input_key(
        ticker="AAPL", item=MaterialUploadInput(filename_entry, file.data)
    )


def test_w3_identity_includes_content_company_and_user_edits() -> None:
    """参数：无。返回：无。异常：断言失败；相同字节数不等于相同内容。"""
    item = make_input(b"aaa")
    key = material_input_key(ticker="AAPL", item=item)
    assert key == material_input_key(ticker=" aapl ", item=item)
    assert key != material_input_key(ticker="AAPL", item=make_input(b"bbb"))
    assert key != material_input_key(ticker="MSFT", item=item)
    assert key != material_input_key(ticker="AAPL", item=replace(item, entry=replace(item.entry, fiscal_year=2024)))
    assert key != material_input_key(ticker="AAPL", item=replace(item, entry=replace(item.entry, material_name="Revision")))


def test_w4_upload_request_uses_existing_command() -> None:
    """参数：无。返回：无。异常：断言失败；命令不得覆盖。"""
    request = build_upload_request(company_name="Apple", ticker=" aapl ", entry=make_input().entry, file_path=Path("call.md"), fiscal_year=2023, fiscal_period="q4")
    assert request.command.name == FinsCommandName.UPLOAD_MATERIAL
    assert request.command.stream is False
    payload = request.command.payload
    assert isinstance(payload, UploadMaterialCommandPayload)
    assert payload.form_type == "EARNINGS_CALL"
    assert payload.ticker == "AAPL"
    assert not payload.overwrite


@pytest.mark.parametrize("ticker,period", [("", "Q4"), ("AAPL", "Q9")])
def test_invalid_upload_request(ticker: str, period: str) -> None:
    """参数：非法公司或财季。返回：无。异常：断言失败。"""
    with pytest.raises(ValueError):
        build_upload_request(company_name="Apple", ticker=ticker, entry=make_input().entry, file_path=Path("call.md"), fiscal_year=2023, fiscal_period=period)


@pytest.mark.parametrize("status,reason,expected", [
    ("ok", None, STATUS_PROCESSING),
    ("skipped", "already_uploaded", STATUS_PROCESSING),
    ("skipped", "unknown", STATUS_FAILED),
    ("failed", None, STATUS_FAILED),
])
def test_upload_success_is_not_ready(status: str, reason: str | None, expected: str) -> None:
    """参数：真实上传状态与原因。返回：无。异常：断言失败。"""
    data = UploadMaterialResultData(pipeline="sec", status=status, skip_reason=reason, ticker="AAPL", material_action="create", document_id="mat_test")
    assert apply_result(entry=make_input().entry, data=data).status == expected


def test_upload_without_document_id_is_failed() -> None:
    """参数：无。返回：无。异常：断言失败；不伪造处理身份。"""
    data = UploadMaterialResultData(pipeline="sec", status="ok", ticker="AAPL", material_action="create")
    assert apply_result(entry=make_input().entry, data=data).status == STATUS_FAILED


def test_s3_resume_only_processing_and_ready_rerun_is_noop(tmp_path: Path) -> None:
    """参数：临时目录。返回：无。异常：断言失败；恢复不得重传。"""
    service = RecordingService(fail_processing=True)
    progress: list[MaterialUploadRow] = []
    item = make_input()
    failed = execute_material_input(ticker="AAPL", company_name="Apple", item=item, workspace_root=tmp_path, fins_service=service, on_progress=progress.append)
    assert failed.status == STATUS_FAILED
    assert failed.document_id == "mat_test"
    assert progress[-1].status == STATUS_PROCESSING
    service.fail_processing = False
    ready = execute_material_input(ticker="AAPL", company_name="Apple", item=item, workspace_root=tmp_path, fins_service=service, previous=failed)
    assert ready.status == STATUS_READY
    assert service.calls == [FinsCommandName.UPLOAD_MATERIAL, FinsCommandName.PROCESS_MATERIAL, FinsCommandName.PROCESS_MATERIAL]
    assert execute_material_input(ticker="AAPL", company_name="Apple", item=item, workspace_root=tmp_path, fins_service=service, previous=ready) == ready
    assert len(service.calls) == 3


@pytest.mark.parametrize("status,reason,expected", [
    ("skipped", "version_matched", STATUS_READY),
    ("skipped", "ingest_incomplete", STATUS_FAILED),
    ("failed", None, STATUS_FAILED),
])
def test_only_valid_processed_snapshot_is_ready(tmp_path: Path, status: str, reason: str | None, expected: str) -> None:
    """参数：临时目录及处理结果。返回：无。异常：断言失败。"""
    service = RecordingService(process_status=status, process_reason=reason)
    result = execute_material_input(ticker="AAPL", company_name="Apple", item=make_input(), workspace_root=tmp_path, fins_service=service)
    assert result.status == expected


def test_unconfirmed_and_path_traversal_do_not_submit(tmp_path: Path) -> None:
    """参数：临时目录。返回：无。异常：断言失败；非法输入不得调用服务。"""
    service = RecordingService()
    item = make_input()
    unconfirmed = replace(item, entry=replace(item.entry, needs_confirmation=True))
    assert execute_material_input(ticker="AAPL", company_name="Apple", item=unconfirmed, workspace_root=tmp_path, fins_service=service).status == STATUS_PENDING_CONFIRMATION
    unsafe = replace(item, entry=replace(item.entry, file_name="../evil.md"))
    assert execute_material_input(ticker="AAPL", company_name="Apple", item=unsafe, workspace_root=tmp_path, fins_service=service).status == STATUS_FAILED
    assert not service.calls


def test_upload_failure_does_not_process_or_block_other_file(tmp_path: Path) -> None:
    """参数：临时目录。返回：无。异常：断言失败；逐文件隔离。"""
    service = RecordingService(upload_status="failed")
    failed = execute_material_input(ticker="AAPL", company_name="Apple", item=make_input(), workspace_root=tmp_path, fins_service=service)
    assert failed.status == STATUS_FAILED and failed.document_id is None
    assert service.calls == [FinsCommandName.UPLOAD_MATERIAL]
    service.upload_status = "ok"
    assert execute_material_input(ticker="AAPL", company_name="Apple", item=make_input(b"other"), workspace_root=tmp_path, fins_service=service).status == STATUS_READY
