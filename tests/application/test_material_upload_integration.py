"""页面编排通过真实 Runtime、Docling、文件仓储及处理器的集成验证。

测试替身仅替代宿主会话外壳；转换、上传、快照及原生查询不使用假结果。
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urlsplit

import pytest

from dayu.contracts.fins import FinsCommandName, FinsResult, UploadMaterialCommandPayload, UploadMaterialResultData
from dayu.fins.domain.document_reference import DocumentReference
from dayu.fins.domain.enums import SourceKind
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
