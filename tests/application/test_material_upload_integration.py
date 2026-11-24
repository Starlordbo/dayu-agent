"""页面编排通过真实 Runtime、Docling、文件仓储及处理器的集成验证。

测试替身仅替代宿主会话外壳；转换、上传、快照及原生查询不使用假结果。
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urlsplit

import pytest

from dayu.contracts.fins import (
    FinsCommand, FinsCommandName, FinsResult, ProcessMaterialCommandPayload,
    ProcessSingleResultData, UploadMaterialCommandPayload, UploadMaterialResultData,
)
from dayu.fins.domain.document_models import DocumentQuery
from dayu.fins.domain.document_reference import DocumentReference, DocumentReferenceView
from dayu.fins.domain.enums import SourceKind
from dayu.fins.pipelines.docling_upload_service import build_upload_source_fingerprint
from dayu.fins.pipelines.json_types import JsonValue
from dayu.fins.pipelines.sec_pipeline import SecPipeline
from dayu.fins.service_runtime import DefaultFinsRuntime
from dayu.fins.tools.result_types import EarningsCallPassageResult
from dayu.services.contracts import FinsSubmission, FinsSubmitRequest
from dayu.services.fins_service import FinsService
from dayu.web.streamlit.pages.chat.citations import parse_source_marker, render_citation_links
from tests.application.conftest import StubHostExecutor, StubSessionRegistry
from dayu.web.streamlit.pages.material_tab import (
    STATUS_FAILED, STATUS_READY, MaterialUploadEntry, MaterialUploadInput, execute_material_input, build_upload_request,
)

pytestmark = pytest.mark.integration

_FIXTURE_DIRECTORY = Path(__file__).parents[1] / "fixtures" / "earnings_call"
_FIXTURE_TITLE = "Q4 2023 Fixture Company Earnings Call"
_FIXTURE_COMPANY = "Fixture Company"
_OTHER_COMPANY = "Other Fixture Company"
_FIXTURE_YEAR = 2023
_FIXTURE_PERIOD = "Q4"
_ORIGINAL_REMARK = "Customer demand improved during the quarter."
_OTHER_REMARK = "Other Fixture Company demand remained stable during the quarter."


class _IntegrationHost(StubSessionRegistry, StubHostExecutor):
    """只替代宿主会话和执行外壳，实际财报执行及读取仍走真实服务。"""

    def __init__(self) -> None:
        """初始化既有会话与执行替身。

        参数:
            无。
        返回值:
            无。
        异常:
            无。
        """
        StubSessionRegistry.__init__(self)
        StubHostExecutor.__init__(self)


class RuntimeSubmissionService(FinsService):
    """使用真实 Service 与 Runtime，只增加命令提交记录。"""

    def __init__(self, runtime: DefaultFinsRuntime) -> None:
        """注入实际财报运行时与测试宿主，不复制原文聚合逻辑。

        参数:
            runtime: 使用隔离工作区的真实财报运行时。
        返回值:
            无。
        异常:
            无。
        """
        super().__init__(host=_IntegrationHost(), fins_runtime=runtime)
        self.calls: list[FinsCommandName] = []

    def submit(self, request: FinsSubmitRequest) -> FinsSubmission:
        """记录并通过真实 Service 提交命令。

        参数:
            request: 页面服务请求。
        返回值:
            真实财报服务生成的同步执行结果。
        异常:
            RuntimeError: 真实上传或处理失败。
            AssertionError: 意外返回流式结果。
        """
        self.calls.append(request.command.name)
        submission = super().submit(request)
        assert isinstance(submission.execution, FinsResult)
        return submission


def _fixture_input(suffix: str) -> MaterialUploadInput:
    """读取既有四格式样本，不重新生成或修改 fixture。

    Args:
        suffix: 既有样本的文件扩展名。
    Returns:
        保留原始字节、标题和财季的上传输入。
    Raises:
        OSError: 样本无法读取。
    """
    source = _FIXTURE_DIRECTORY / f"2023Q4_call.{suffix}"
    entry = MaterialUploadEntry(
        source.name, _FIXTURE_TITLE, _FIXTURE_YEAR, _FIXTURE_PERIOD, False, "confirmed",
    )
    return MaterialUploadInput(entry, source.read_bytes())


def _source_files(runtime: DefaultFinsRuntime, ticker: str, document_id: str) -> dict[str, bytes]:
    """通过公开仓储读取源材料全部文件，用于比较重建和重传前后的持久内容。

    Args:
        runtime: 当前真实运行时。
        ticker: 材料所属公司。
        document_id: 实际入库编号。
    Returns:
        文件名到原始字节的映射，包含原件与转换产物。
    Raises:
        OSError: 源材料或文件无法读取。
    """
    handle = runtime.source_repository.get_source_handle(ticker, document_id, SourceKind.MATERIAL)
    return {
        Path(file.uri).name: runtime.blob_repository.read_file_bytes(handle, Path(file.uri).name)
        for file in runtime.blob_repository.list_files(handle)
    }


def _reject_read_command(
    command: FinsCommand, *, cancel_checker: Callable[[], bool] | None = None,
) -> FinsResult:
    """在只读验证期间阻断命令执行，禁止读取偷偷上传或处理。

    Args:
        command: 不应发生的运行时命令。
        cancel_checker: 命令取消检查器。
    Returns:
        不返回。
    Raises:
        AssertionError: 只读读取触发运行时命令。
    """
    raise AssertionError(f"只读原文不应执行命令：{command.name}")


def _read_material_views(
    service: RuntimeSubmissionService, *, ticker: str, document_id: str, company_name: str,
) -> tuple[DocumentReferenceView, ...]:
    """通过真实目录和机器引用读取全文，并验证读取没有触发命令或 Agent。

    Args:
        service: 当前 Runtime 重新装配的类型化服务。
        ticker: 预期公司代码。
        document_id: 实际入库编号。
        company_name: 预期公司名称。
    Returns:
        文档级视图和按真实目录顺序读取的章节视图。
    Raises:
        AssertionError: 文档头、原文、引用范围或只读边界不符合契约。
        OSError: 持久材料无法读取。
    """
    host = service.host
    assert isinstance(host, _IntegrationHost)
    calls_before = service.calls.copy()
    sessions_before = host.list_sessions()
    execution_counts = (host.sync_call_count, host.stream_call_count)
    tools = service.fins_runtime.get_tool_service()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(service.fins_runtime, "execute", _reject_read_command)
        document = service.resolve_document_reference(DocumentReference(ticker, document_id))
        assert service.list_materials(ticker) == [document.info]
        assert (document.info.ticker, document.info.document_id) == (ticker, document_id)
        assert (document.info.company_name, document.info.material_name) == (company_name, _FIXTURE_TITLE)
        assert (document.info.fiscal_year, document.info.fiscal_period) == (_FIXTURE_YEAR, _FIXTURE_PERIOD)
        assert document.info.form_type == "EARNINGS_CALL"
        assert document.selected_section is None
        directory = tools.get_document_sections(ticker=ticker, document_id=document_id)
        document_marker = directory["citation"]["source_marker"]
        assert isinstance(document_marker, str)
        parsed_document = parse_source_marker(document_marker)
        assert parsed_document is not None
        assert (parsed_document.ticker, parsed_document.document_id, parsed_document.ref) == (ticker, document_id, None)
        assert directory["citation"]["scope"] == "document"
        assert [section.ref for section in document.sections] == [section["ref"] for section in directory["sections"]]
        views = [document]
        bodies: list[str] = []
        for section in document.sections:
            content = tools.read_section(ticker=ticker, document_id=document_id, ref=section.ref)
            marker = content["citation"]["source_marker"]
            assert isinstance(marker, str)
            parsed = parse_source_marker(marker)
            assert parsed is not None
            expected_ref = None if content["contains_full_text"] else section.ref
            assert (parsed.ticker, parsed.document_id, parsed.ref) == (ticker, document_id, expected_ref)
            marker_view = service.resolve_document_reference(DocumentReference(parsed.ticker, parsed.document_id, parsed.ref))
            view = service.resolve_document_reference(DocumentReference(ticker, document_id, section.ref))
            assert marker_view == (document if expected_ref is None else view)
            selected = view.selected_section
            assert selected is not None and selected.ref == section.ref
            assert view.info == document.info and view.sections == document.sections
            assert selected.content == content["content"]
            assert "".join(passage.text for passage in selected.passages) == selected.content
            bodies.append(selected.content)
            views.append(view)
        full_text = "\n".join(bodies)
        for evidence in (
            "42 million", "100 million", "Alex Smith - Analyst", "Jane Doe - Chief Executive Officer",
            "We have not provided that revenue forecast",
        ):
            assert evidence in full_text
    assert service.calls == calls_before
    assert host.list_sessions() == sessions_before
    assert (host.sync_call_count, host.stream_call_count) == execution_counts
    assert host.last_execution_contract is None and host.last_prepared_turn is None
    return tuple(views)


@pytest.mark.parametrize("suffix", ["md", "txt", "docx", "pdf"])
def test_four_formats_new_runtime_preserves_references_and_deduplicates(tmp_path: Path, suffix: str) -> None:
    """全新 Runtime/Service 仍能定位已处理材料，同指纹重传不产生第二份材料。

    Args:
        tmp_path: 供重建前后共用的隔离工作区。
        suffix: 既有真实转换样本格式。
    Returns:
        无。
    Raises:
        AssertionError: 入库被误当就绪、重建丢失引用或重传改变源材料。
        OSError: 样本或仓储读写失败。
    """
    item = _fixture_input(suffix)
    source = _FIXTURE_DIRECTORY / item.entry.file_name
    runtime = DefaultFinsRuntime.create(workspace_root=tmp_path)
    service = RuntimeSubmissionService(runtime)
    upload_request = build_upload_request(
        ticker="AAPL", company_name=_FIXTURE_COMPANY, entry=item.entry, file_path=source,
        fiscal_year=_FIXTURE_YEAR, fiscal_period=_FIXTURE_PERIOD,
    )
    uploaded = service.submit(upload_request).execution
    assert isinstance(uploaded, FinsResult) and isinstance(uploaded.data, UploadMaterialResultData)
    assert uploaded.data.status == "ok"
    document_id = uploaded.data.document_id
    assert document_id is not None
    meta_before = runtime.source_repository.get_source_meta("AAPL", document_id, SourceKind.MATERIAL)
    assert meta_before["ingest_complete"] is True
    assert meta_before["source_fingerprint"] == build_upload_source_fingerprint([source])
    files_before = _source_files(runtime, "AAPL", document_id)
    assert files_before[source.name] == item.data
    # 目录可见只说明入库；此时还没有处理快照，不能据此声称已经可问答。
    assert [info.document_id for info in service.list_materials("AAPL")] == [document_id]
    assert runtime.processed_repository.list_processed_documents("AAPL", DocumentQuery()) == []
    process_request = FinsSubmitRequest(command=FinsCommand(
        name=FinsCommandName.PROCESS_MATERIAL,
        payload=ProcessMaterialCommandPayload(ticker="AAPL", document_id=document_id),
    ))
    processed = service.submit(process_request).execution
    assert isinstance(processed, FinsResult) and isinstance(processed.data, ProcessSingleResultData)
    assert (processed.data.status, processed.data.document_id) == ("processed", document_id)
    before = _read_material_views(service, ticker="AAPL", document_id=document_id, company_name=_FIXTURE_COMPANY)

    fresh_runtime = DefaultFinsRuntime.create(workspace_root=tmp_path)
    fresh_service = RuntimeSubmissionService(fresh_runtime)
    assert fresh_runtime is not runtime and fresh_service is not service
    assert _read_material_views(
        fresh_service, ticker="AAPL", document_id=document_id, company_name=_FIXTURE_COMPANY,
    ) == before
    assert fresh_service.calls == []
    assert fresh_runtime.source_repository.get_source_meta("AAPL", document_id, SourceKind.MATERIAL) == meta_before
    assert _source_files(fresh_runtime, "AAPL", document_id) == files_before

    duplicate = fresh_service.submit(upload_request).execution
    assert isinstance(duplicate, FinsResult) and isinstance(duplicate.data, UploadMaterialResultData)
    assert (duplicate.data.status, duplicate.data.skip_reason, duplicate.data.document_id) == (
        "skipped", "already_uploaded", document_id,
    )
    reprocessed = fresh_service.submit(process_request).execution
    assert isinstance(reprocessed, FinsResult) and isinstance(reprocessed.data, ProcessSingleResultData)
    assert (reprocessed.data.status, reprocessed.data.reason, reprocessed.data.document_id) == (
        "skipped", "version_matched", document_id,
    )
    assert fresh_runtime.source_repository.list_source_document_ids("AAPL", SourceKind.MATERIAL) == [document_id]
    assert [document.document_id for document in fresh_runtime.processed_repository.list_processed_documents(
        "AAPL", DocumentQuery(),
    )] == [document_id]
    assert fresh_runtime.source_repository.get_source_meta("AAPL", document_id, SourceKind.MATERIAL) == meta_before
    assert _source_files(fresh_runtime, "AAPL", document_id) == files_before
    assert _read_material_views(
        fresh_service, ticker="AAPL", document_id=document_id, company_name=_FIXTURE_COMPANY,
    ) == before
    assert fresh_service.calls == [FinsCommandName.UPLOAD_MATERIAL, FinsCommandName.PROCESS_MATERIAL]


@pytest.mark.parametrize("suffix", ["md", "txt", "docx", "pdf"])
def test_four_formats_new_runtime_isolates_same_document_id_by_company(tmp_path: Path, suffix: str) -> None:
    """同名同财季材料跨公司得到相同 ID，重建后的引用仍严格按公司读取。

    Args:
        tmp_path: 两家公司共用的隔离工作区。
        suffix: 既有样本格式；仅文本格式派生不同正文，二进制保持原样。
    Returns:
        无。
    Raises:
        AssertionError: 同 ID 材料的原件、公司头、正文或来源 ticker 串扰。
        OSError: 样本或仓储读写失败。
    """
    item = _fixture_input(suffix)
    other_item = item
    if suffix in {"md", "txt"}:
        # 只改临时上传输入的一句正文；保持 42/100 million 对照及原始 fixture 不变。
        assert _ORIGINAL_REMARK.encode("utf-8") in item.data
        other_item = replace(item, data=item.data.replace(
            _ORIGINAL_REMARK.encode("utf-8"), _OTHER_REMARK.encode("utf-8"),
        ))
    runtime = DefaultFinsRuntime.create(workspace_root=tmp_path)
    service = RuntimeSubmissionService(runtime)
    first = execute_material_input(
        ticker="AAPL", company_name=_FIXTURE_COMPANY, item=item, workspace_root=tmp_path, fins_service=service,
    )
    assert first.status == STATUS_READY, first.message
    assert first.document_id is not None
    before = _read_material_views(service, ticker="AAPL", document_id=first.document_id, company_name=_FIXTURE_COMPANY)
    second = execute_material_input(
        ticker="MSFT", company_name=_OTHER_COMPANY, item=other_item, workspace_root=tmp_path, fins_service=service,
    )
    assert second.status == STATUS_READY, second.message
    assert second.document_id == first.document_id

    fresh_runtime = DefaultFinsRuntime.create(workspace_root=tmp_path)
    fresh_service = RuntimeSubmissionService(fresh_runtime)
    # 交替读取相同 ID，使用公开返回值证明隔离，不窥探或清空内部缓存。
    for ticker, company_name, expected_item in (
        ("AAPL", _FIXTURE_COMPANY, item), ("MSFT", _OTHER_COMPANY, other_item),
        ("AAPL", _FIXTURE_COMPANY, item), ("MSFT", _OTHER_COMPANY, other_item),
    ):
        views = _read_material_views(
            fresh_service, ticker=ticker, document_id=first.document_id, company_name=company_name,
        )
        assert fresh_runtime.source_repository.list_source_document_ids(ticker, SourceKind.MATERIAL) == [first.document_id]
        assert _source_files(fresh_runtime, ticker, first.document_id)[item.entry.file_name] == expected_item.data
        if ticker == "AAPL":
            assert views == before
        if suffix in {"md", "txt"}:
            full_text = "\n".join(view.selected_section.content for view in views if view.selected_section is not None)
            expected_remark, excluded_remark = (
                (_ORIGINAL_REMARK, _OTHER_REMARK) if ticker == "AAPL" else (_OTHER_REMARK, _ORIGINAL_REMARK)
            )
            assert expected_remark in full_text and excluded_remark not in full_text
    assert fresh_service.calls == []


def fail_snapshot_export(
    self: SecPipeline, *, ticker: str, document_id: str, source_kind: SourceKind,
    source_meta: dict[str, JsonValue], ci: bool, expected_parser_signature: str,
    cancel_checker: Callable[[], bool] | None = None,
) -> None:
    """在源材料入库后、快照导出时注入失败。

    Args:
        self: 真实管线。
        ticker: 公司代码。
        document_id: 文档编号。
        source_kind: 源文档类别。
        source_meta: 真实入库元数据。
        ci: 快照选项。
        expected_parser_signature: 解析器版本。
        cancel_checker: 取消检查器。
    Returns:
        无。
    Raises:
        RuntimeError: 可重复的处理失败。
    """
    raise RuntimeError("injected snapshot failure after upload")


def test_s3_real_uploaded_material_resumes_without_upload_or_second_material(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证真实部分状态恢复，而不是把 ingest_complete 当处理完成。

    Args:
        tmp_path: 隔离工作区。
        monkeypatch: 仅用于注入快照导出失败。
    Returns:
        无。
    Raises:
        AssertionError: 重传、重复入库或未完成真实处理。
    """
    runtime = DefaultFinsRuntime.create(workspace_root=tmp_path)
    service = RuntimeSubmissionService(runtime)
    item = MaterialUploadInput(
        MaterialUploadEntry("2023Q4_call.md", "Q4 2023 Earnings Call", 2023, "Q4", False, "declaration"),
        b"# Q4 2023 Earnings Call\n\n## Management\n\nRevenue increased to 42 million dollars.\n\n## Questions and Answers\n\nAnalyst: What drove growth?\n\nCEO: Customer demand improved.\n",
    )
    with monkeypatch.context() as patch:
        patch.setattr(SecPipeline, "_export_tool_snapshot_for_document", fail_snapshot_export)
        failed = execute_material_input(ticker="AAPL", company_name="Apple", item=item, workspace_root=tmp_path, fins_service=service)
    assert failed.status == STATUS_FAILED, failed.message
    assert "injected snapshot failure" in failed.message
    assert failed.document_id is not None
    meta_before = runtime.source_repository.get_source_meta("AAPL", failed.document_id, SourceKind.MATERIAL)
    assert meta_before["ingest_complete"] is True
    handle = runtime.source_repository.get_source_handle("AAPL", failed.document_id, SourceKind.MATERIAL)
    files_before = runtime.blob_repository.list_files(handle)
    ready = execute_material_input(ticker="AAPL", company_name="Apple", item=item, workspace_root=tmp_path, fins_service=service, previous=failed)
    assert ready.status == STATUS_READY, ready.message
    assert ready.document_id is not None
    assert ready.document_id == failed.document_id
    assert service.calls == [FinsCommandName.UPLOAD_MATERIAL, FinsCommandName.PROCESS_MATERIAL, FinsCommandName.PROCESS_MATERIAL]
    assert runtime.source_repository.get_source_meta("AAPL", ready.document_id, SourceKind.MATERIAL) == meta_before
    assert runtime.blob_repository.list_files(handle) == files_before
    # 全新页面检查点也可凭仓储指纹跳过转换，再复用现有处理快照。
    reloaded = execute_material_input(ticker="AAPL", company_name="Apple", item=item, workspace_root=tmp_path, fins_service=service)
    assert reloaded.status == STATUS_READY, reloaded.message
    assert reloaded.document_id == ready.document_id
    assert runtime.source_repository.get_source_meta("AAPL", ready.document_id, SourceKind.MATERIAL) == meta_before


@pytest.mark.parametrize("ticker", ["AAPL", "600519", "0700"])
def test_s8_pipeline_rejects_overwrite_before_reset(tmp_path: Path, ticker: str) -> None:
    """真实 US/CN/HK 管线必须先拒绝覆盖，不能先清空后报错。

    Args:
        tmp_path: 隔离工作区。
        ticker: 市场对应代码。
    Returns:
        无。
    Raises:
        AssertionError: 拒绝前发生任何材料元数据或原件变更。
    """
    runtime = DefaultFinsRuntime.create(workspace_root=tmp_path)
    service = RuntimeSubmissionService(runtime)
    item = MaterialUploadInput(MaterialUploadEntry("call.md", "Q4 Call", 2023, "Q4", False, "confirmed"),
                               b"# Q4 Earnings Call\n\nRevenue increased by 20 percent.\n")
    ready = execute_material_input(ticker=ticker, company_name="Fixture Company", item=item,
                                   workspace_root=tmp_path, fins_service=service)
    assert ready.status == STATUS_READY, ready.message
    assert ready.document_id is not None
    before = runtime.source_repository.get_source_meta(ticker, ready.document_id, SourceKind.MATERIAL)
    handle = runtime.source_repository.get_source_handle(ticker, ready.document_id, SourceKind.MATERIAL)
    originals = {Path(file.uri).name: runtime.blob_repository.read_file_bytes(handle, Path(file.uri).name)
                 for file in runtime.blob_repository.list_files(handle)}
    replacement = tmp_path / "replacement.md"
    replacement.write_text("# Replacement\n\nDifferent content.\n", encoding="utf-8")
    request = build_upload_request(ticker=ticker, company_name="Fixture Company", entry=item.entry,
                                   file_path=replacement, fiscal_year=2023, fiscal_period="Q4")
    assert isinstance(request.command.payload, UploadMaterialCommandPayload)
    overwrite_request = replace(request, command=replace(request.command, payload=replace(request.command.payload, overwrite=True)))
    with pytest.raises(ValueError, match="不支持 overwrite=True"):
        service.submit(overwrite_request)
    assert runtime.source_repository.get_source_meta(ticker, ready.document_id, SourceKind.MATERIAL) == before
    assert {Path(file.uri).name: runtime.blob_repository.read_file_bytes(handle, Path(file.uri).name)
            for file in runtime.blob_repository.list_files(handle)} == originals


@pytest.mark.parametrize("suffix", ["md", "txt", "docx", "pdf"])
def test_four_formats_real_upload_process_discover_search_read(tmp_path: Path, suffix: str) -> None:
    """四格式从真实转换一路验证到原生文档发现、搜索与章节读取。

    Args:
        tmp_path: 隔离工作区。
        suffix: 受支持格式。
    Returns:
        无。
    Raises:
        AssertionError: 材料、快照、原文内容或引用身份不完整。
    """
    source = Path(__file__).parents[1] / "fixtures" / "earnings_call" / f"2023Q4_call.{suffix}"
    runtime = DefaultFinsRuntime.create(workspace_root=tmp_path)
    service = RuntimeSubmissionService(runtime)
    title = "Q4 2023 Fixture Company Earnings Call"
    item = MaterialUploadInput(MaterialUploadEntry(source.name, title, 2023, "Q4", False, "confirmed"), source.read_bytes())
    row = execute_material_input(ticker="AAPL", company_name="Fixture Company", item=item,
                                 workspace_root=tmp_path, fins_service=service)
    assert row.status == STATUS_READY, row.message
    assert row.document_id is not None
    tool_service = runtime.get_tool_service()
    documents = tool_service.list_documents(ticker="AAPL", document_types=["earnings_call"],
                                            fiscal_years=[2023], fiscal_periods=["Q4"])
    assert documents["matched"] == 1
    assert documents["documents"][0]["document_id"] == row.document_id
    sections = tool_service.get_document_sections(ticker="AAPL", document_id=row.document_id)
    assert sections["sections"]
    document_marker = sections["citation"]["source_marker"]
    assert isinstance(document_marker, str)
    document_citation = parse_source_marker(document_marker)
    assert document_citation is not None and document_citation.ref is None
    assert document_citation.document_id == row.document_id
    assert sections["citation"]["scope"] == "document"
    assert "section_ref" not in sections["citation"]
    assert render_citation_links(document_marker, {}).startswith("[文档来源](?")
    document_view = service.resolve_document_reference(DocumentReference("AAPL", row.document_id))
    assert document_view.info.material_name == title
    assert document_view.info.company_name == "Fixture Company"
    assert document_view.selected_section is None
    assert {entry.ref for entry in document_view.sections} == {section["ref"] for section in sections["sections"]}
    assert service.list_materials("AAPL") == [document_view.info]
    found = tool_service.search_document(ticker="AAPL", document_id=row.document_id, query="revenue")
    assert found["total_matches"] > 0
    bodies: list[str] = []
    passages: list[EarningsCallPassageResult] = []
    for section in sections["sections"]:
        ref = section["ref"]
        assert isinstance(ref, str)
        content = tool_service.read_section(ticker="AAPL", document_id=row.document_id, ref=ref)
        assert content["document_id"] == row.document_id
        assert content["ref"] == ref
        citation = content["citation"]
        marker = citation["source_marker"]
        assert isinstance(marker, str)
        parsed = parse_source_marker(marker)
        assert parsed is not None
        assert (parsed.ticker, parsed.document_id) == ("AAPL", row.document_id)
        assert citation["material_name"] == title
        if content["contains_full_text"]:
            assert parsed.ref is None
            assert citation["scope"] == "document"
            assert "section_ref" not in citation
            assert "42 million" in content["content"] and "100 million" in content["content"]
        else:
            assert parsed.ref == ref
            assert citation["section_ref"] == ref
            assert citation["scope"] == "section"
        rendered = render_citation_links(marker, {"chat_session": "integration"})
        label = "文档来源" if parsed.ref is None else "章节来源"
        assert rendered.startswith(f"[{label}](?")
        query = parse_qs(urlsplit(rendered.removeprefix(f"[{label}](").removesuffix(")")).query)
        expected_query = {
            "chat_session": ["integration"], "source_ticker": ["AAPL"], "source_document": [row.document_id],
        }
        if parsed.ref is not None:
            expected_query["source_ref"] = [ref]
        assert query == expected_query
        source_view = service.resolve_document_reference(DocumentReference(parsed.ticker, parsed.document_id, parsed.ref))
        if parsed.ref is None:
            assert source_view.selected_section is None
        else:
            selected = source_view.selected_section
            assert selected is not None
            assert selected.ref == ref and selected.content == content["content"]
        assert content["passages"]
        assert "".join(passage["text"] for passage in content["passages"]) == content["content"]
        for passage in content["passages"]:
            assert 0 <= passage["start"] < passage["end"] <= len(content["content"])
            assert passage["text"] == content["content"][passage["start"]:passage["end"]]
            evidence = passage["evidence"]
            if evidence is not None:
                assert evidence in passage["text"].splitlines()
                assert evidence in content["content"].splitlines()
        passages.extend(content["passages"])
        bodies.append(content["content"])
    analyst_questions = [passage for passage in passages if "100 million" in passage["text"]]
    management_responses = [passage for passage in passages if "We have not provided" in passage["text"]]
    assert analyst_questions and management_responses
    for passage in analyst_questions:
        assert passage["role"] == "analyst" and passage["kind"] == "question"
        assert passage["speaker"] == "Alex Smith"
        assert passage["evidence"] is not None and "Alex Smith - Analyst" in passage["evidence"]
        assert "We have not provided" not in passage["text"]
    for passage in management_responses:
        assert passage["role"] == "management" and passage["kind"] == "statement"
        assert passage["speaker"] == "Jane Doe"
        assert passage["evidence"] is not None and "Jane Doe - Chief Executive Officer" in passage["evidence"]
        assert "100 million" not in passage["text"]
    assert service.calls == [FinsCommandName.UPLOAD_MATERIAL, FinsCommandName.PROCESS_MATERIAL]
    full_text = "\n".join(bodies)
    assert "42 million" in full_text
    assert "Alex Smith - Analyst" in full_text
    assert "Jane Doe - Chief Executive Officer" in full_text
    assert "100 million" in full_text
    assert "We have not provided that revenue forecast" in full_text
    assert runtime.source_repository.list_source_document_ids("AAPL", SourceKind.MATERIAL) == [row.document_id]


@pytest.mark.parametrize("suffix", ["md", "txt"])
def test_full_text_fallback_keeps_document_scope(tmp_path: Path, suffix: str) -> None:
    """无标题纪要的全文兜底不得伪装为章节，真实内容与发言仍可读取。

    参数:
        tmp_path: 隔离的真实管线工作区。
        suffix: 无标题文本材料格式。
    返回值:
        无。
    异常:
        AssertionError: 全文被误标章节、正文或身份丢失时抛出。
    """
    runtime = DefaultFinsRuntime.create(workspace_root=tmp_path)
    service = RuntimeSubmissionService(runtime)
    text = (
        "Alex Smith - Analyst\n\nCould revenue reach 100 million dollars next year?\n\n"
        "Jane Doe - Chief Executive Officer\n\nWe have not provided that revenue forecast.\n"
    )
    item = MaterialUploadInput(
        MaterialUploadEntry(f"2023Q4_call.{suffix}", "无章节电话会议原文", 2023, "Q4", False, "confirmed"),
        text.encode("utf-8"),
    )
    row = execute_material_input(
        ticker="AAPL", company_name="Fixture Company", item=item, workspace_root=tmp_path, fins_service=service,
    )
    assert row.status == STATUS_READY, row.message
    assert row.document_id is not None
    tools = runtime.get_tool_service()
    directory = tools.get_document_sections(ticker="AAPL", document_id=row.document_id)
    assert len(directory["sections"]) == 1
    ref = directory["sections"][0]["ref"]
    assert isinstance(ref, str)
    result = tools.read_section(ticker="AAPL", document_id=row.document_id, ref=ref)
    assert result["contains_full_text"] is True
    assert result["citation"]["scope"] == "document"
    assert "section_ref" not in result["citation"]
    marker = result["citation"]["source_marker"]
    assert isinstance(marker, str)
    parsed = parse_source_marker(marker)
    assert parsed is not None and parsed.ref is None
    assert (parsed.ticker, parsed.document_id) == ("AAPL", row.document_id)
    link = render_citation_links(marker, {"source_ref": "stale", "chat_session": "kept"})
    assert link.startswith("[文档来源](?") and "source_ref" not in link
    assert "chat_session=kept" in link
    assert "100 million" in result["content"] and "We have not provided" in result["content"]
    assert [passage["role"] for passage in result["passages"]] == ["analyst", "management"]
    assert "".join(passage["text"] for passage in result["passages"]) == result["content"]
    view = service.resolve_document_reference(DocumentReference("AAPL", row.document_id, ref))
    assert view.selected_section is not None and view.selected_section.contains_full_text
    assert view.selected_section.content == result["content"]
    assert service.calls == [FinsCommandName.UPLOAD_MATERIAL, FinsCommandName.PROCESS_MATERIAL]
