"""以真实 Runtime、转换和文件仓储验证 W4–W7 的只读引用服务。"""

from __future__ import annotations

# pyright: strict

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from dayu.contracts.fins import (
    FinsCommand,
    FinsCommandName,
    FinsResult,
    ProcessMaterialCommandPayload,
    UploadMaterialResultData,
)
from dayu.engine.exceptions import ToolArgumentError
from dayu.fins.domain.document_models import SourceDocumentStateChangeRequest, SourceDocumentUpsertRequest
from dayu.fins.domain.document_reference import DocumentReference, DocumentReferenceView
from dayu.fins.domain.enums import SourceKind
from dayu.fins.service_runtime import DefaultFinsRuntime
from dayu.services.contracts import FinsSubmission, FinsSubmitRequest
from dayu.services.fins_service import FinsService
from dayu.web.streamlit.pages.material_tab import MaterialUploadEntry, build_upload_request
from tests.application.conftest import StubHostExecutor, StubSessionRegistry
from tests.application.test_material_upload_integration import RuntimeSubmissionService

pytestmark = pytest.mark.integration

_TITLE = "Q4 2023 Reference Company Earnings Call"
_COMPANY = "Reference Company"
_BODY = (
    "# Q4 2023 Reference Company Earnings Call\n\n"
    "## Management Remarks\n\nJane Doe - Chief Executive Officer\n\n"
    "Revenue reached 42 million dollars. Customer demand improved during the quarter.\n\n"
    "## Questions and Answers\n\nAlex Smith - Analyst\n\n"
    "Could revenue reach 100 million dollars next year? This is my question, not company guidance.\n\n"
    "Jane Doe - Chief Executive Officer\n\n"
    "We have not provided that revenue forecast. Our focus remains profitable customer growth.\n"
)


class _ReadOnlyHost(StubSessionRegistry, StubHostExecutor):
    """组合已有完整类型桩，满足宿主协议并记录所有执行入口。"""

    def __init__(self) -> None:
        """初始化会话与执行记录，不创建真实模型。

        Args:
            无。
        Returns:
            无。
        Raises:
            无。
        """
        StubSessionRegistry.__init__(self)
        StubHostExecutor.__init__(self)


@dataclass
class _ReadOnlyFinsService(FinsService):
    """继承真实读取路径，只将命令提交变为立即失败的哨兵。"""

    submit_calls: int = 0

    def submit(self, request: FinsSubmitRequest) -> FinsSubmission:
        """禁止只读视图走命令、上传或处理路径。

        Args:
            request: 意外收到的命令请求。
        Returns:
            不返回。
        Raises:
            AssertionError: 只读读取错误调用命令入口。
        """
        self.submit_calls += 1
        raise AssertionError(f"只读原文不应提交命令：{request.command.name}")


@dataclass(frozen=True)
class _StoredReference:
    """每个测试独立的真实仓储及只读服务装配。"""

    runtime: DefaultFinsRuntime
    service: _ReadOnlyFinsService
    host: _ReadOnlyHost
    document_id: str


def _upload_material(
    runtime: DefaultFinsRuntime,
    root: Path,
    *,
    ticker: str = "AAPL",
    company_name: str = _COMPANY,
    body: str = _BODY,
) -> str:
    """经真实上传与处理命令准备原文，不伪造解析快照。

    Args:
        runtime: 真实财报 Runtime。
        root: 隔离工作区。
        ticker: 原文所属公司。
        company_name: 入库公司名称。
        body: 待转换 Markdown 正文。
    Returns:
        实际创建的材料编号。
    Raises:
        AssertionError: 真实上传未返回材料编号或执行失败。
        OSError: 测试输入写入失败。
    """
    source = root / f"{ticker}_2023Q4_call.md"
    source.write_text(body, encoding="utf-8")
    upload_service = RuntimeSubmissionService(runtime)
    entry = MaterialUploadEntry(source.name, _TITLE, 2023, "Q4", False, "declaration")
    request = build_upload_request(
        ticker=ticker,
        company_name=company_name,
        entry=entry,
        file_path=source,
        fiscal_year=2023,
        fiscal_period="Q4",
    )
    execution = upload_service.submit(request).execution
    assert isinstance(execution, FinsResult)
    assert isinstance(execution.data, UploadMaterialResultData)
    assert execution.data.document_id is not None
    document_id = execution.data.document_id
    upload_service.submit(
        FinsSubmitRequest(
            command=FinsCommand(
                name=FinsCommandName.PROCESS_MATERIAL,
                payload=ProcessMaterialCommandPayload(ticker=ticker, document_id=document_id),
            )
        )
    )
    return document_id


def _reject_runtime_command(command: FinsCommand, *, cancel_checker: Callable[[], bool] | None = None) -> FinsResult:
    """禁止原文读取偷偷绕过 Service 调用运行时命令入口。

    Args:
        command: 意外调用的命令。
        cancel_checker: 命令取消检查器。
    Returns:
        不返回。
    Raises:
        AssertionError: 只读引用触发 Runtime 执行。
    """
    del cancel_checker
    raise AssertionError(f"查看原文不应执行 Runtime 命令：{command.name}")


@pytest.fixture
def stored_reference(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _StoredReference:
    """为每例创建真实材料，读取阶段不再提交命令。

    Args:
        tmp_path: pytest 提供的隔离目录。
        monkeypatch: 读取阶段禁止运行时命令的恢复型补丁。
    Returns:
        真实 Runtime、只读服务、宿主桩和材料编号。
    Raises:
        AssertionError: 上传或只读装配不符合契约。
    """
    runtime = DefaultFinsRuntime.create(workspace_root=tmp_path)
    document_id = _upload_material(runtime, tmp_path)
    monkeypatch.setattr(runtime, "execute", _reject_runtime_command)
    host = _ReadOnlyHost()
    return _StoredReference(runtime, _ReadOnlyFinsService(host, runtime), host, document_id)


def _assert_read_only(stored: _StoredReference) -> None:
    """断言查看原文未创建会话、提交命令或启动宿主执行。

    Args:
        stored: 当前测试的服务装配。
    Returns:
        无。
    Raises:
        AssertionError: 发生任何宿主执行或命令提交。
    """
    assert stored.service.submit_calls == 0
    assert stored.host.sync_call_count == 0
    assert stored.host.stream_call_count == 0
    assert stored.host.last_execution_contract is None
    assert stored.host.last_prepared_turn is None
    assert stored.host.list_sessions() == []


def _question_view(stored: _StoredReference, ticker: str = "AAPL") -> DocumentReferenceView:
    """通过真实目录寻找问答 ref，再以该 ref 读取章节。

    Args:
        stored: 真实材料装配。
        ticker: 原文公司。
    Returns:
        真实问答章节视图。
    Raises:
        AssertionError: 目录未提供问答章节。
    """
    document = stored.service.resolve_document_reference(DocumentReference(ticker, stored.document_id))
    sections = [section for section in document.sections if section.title == "Questions and Answers"]
    assert len(sections) == 1
    return stored.service.resolve_document_reference(DocumentReference(ticker, stored.document_id, sections[0].ref))


def test_w4_persisted_material_list_survives_new_runtime(
    stored_reference: _StoredReference, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """W4：重建 Runtime 仍从仓储读取真实公司、财季和材料名。

    Args:
        stored_reference: 已入库且处理完成的材料。
        tmp_path: 既有真实仓储目录。
        monkeypatch: 禁止重建后的 Runtime 执行命令。
    Returns:
        无。
    Raises:
        AssertionError: 列表依赖页面会话或元数据与仓储不一致。
    """
    fresh_runtime = DefaultFinsRuntime.create(workspace_root=tmp_path)
    monkeypatch.setattr(fresh_runtime, "execute", _reject_runtime_command)
    host = _ReadOnlyHost()
    fresh = _ReadOnlyFinsService(host, fresh_runtime)
    materials = fresh.list_materials("AAPL")
    assert len(materials) == 1
    assert materials[0].document_id == stored_reference.document_id
    assert materials[0].ticker == "AAPL"
    assert materials[0].company_name == _COMPANY
    assert materials[0].material_name == _TITLE
    assert (materials[0].fiscal_year, materials[0].fiscal_period) == (2023, "Q4")
    assert materials[0].form_type == "EARNINGS_CALL"
    _assert_read_only(_StoredReference(fresh_runtime, fresh, host, stored_reference.document_id))


def test_w5_w6_w7_real_directory_scope_and_mixed_evidence(stored_reference: _StoredReference) -> None:
    """W5–W7：真实 ref 保留引用范围、完整原文及分析师与管理层证据。

    Args:
        stored_reference: 已准备的真实材料。
    Returns:
        无。
    Raises:
        AssertionError: 文档级引用冒充章节或问句冒充管理层预测。
    """
    document = stored_reference.service.resolve_document_reference(
        DocumentReference("AAPL", stored_reference.document_id)
    )
    assert document.info.material_name == _TITLE
    assert document.info.company_name == _COMPANY
    assert (document.info.fiscal_year, document.info.fiscal_period) == (2023, "Q4")
    assert document.info.ticker == "AAPL"
    assert document.info.document_id == stored_reference.document_id
    assert document.selected_section is None
    assert len(document.sections) >= 2
    assert len({section.ref for section in document.sections}) == len(document.sections)
    view = _question_view(stored_reference)
    section = view.selected_section
    assert section is not None and not section.contains_full_text
    assert section.ref in {summary.ref for summary in document.sections}
    assert "Could revenue reach 100 million" in section.content
    assert "We have not provided that revenue forecast" in section.content
    assert "".join(passage.text for passage in section.passages) == section.content
    analysts = [passage for passage in section.passages if passage.role == "analyst"]
    management = [passage for passage in section.passages if passage.role == "management"]
    assert len(analysts) == len(management) == 1, section.content
    assert analysts[0].kind == "question"
    assert analysts[0].evidence == "Alex Smith - Analyst"
    assert management[0].evidence == "Jane Doe - Chief Executive Officer"
    assert management[0].kind == "statement"
    assert "100 million" not in management[0].text
    for passage in section.passages:
        assert passage.text == section.content[passage.start : passage.end]
    _assert_read_only(stored_reference)


def test_same_document_id_isolated_by_company(stored_reference: _StoredReference, tmp_path: Path) -> None:
    """相同材料编号在不同公司下分别解析，不受当前侧栏或缓存串扰。

    Args:
        stored_reference: AAPL 的真实材料。
        tmp_path: 隔离工作区。
    Returns:
        无。
    Raises:
        AssertionError: 相同编号跨公司读到错误正文。
    """
    other_id = _upload_material(
        DefaultFinsRuntime.create(workspace_root=tmp_path),
        tmp_path,
        ticker="MSFT",
        company_name="Other Company",
        body=_BODY.replace("100 million", "777 million"),
    )
    assert other_id == stored_reference.document_id
    first = _question_view(stored_reference)
    second = _question_view(stored_reference, "MSFT")
    assert first.selected_section is not None and second.selected_section is not None
    assert first.info.company_name == _COMPANY
    assert second.info.company_name == "Other Company"
    assert "100 million" in first.selected_section.content
    assert "777 million" in second.selected_section.content
    assert "100 million" not in second.selected_section.content
    assert "777 million" not in first.selected_section.content
    _assert_read_only(stored_reference)


def test_unheaded_source_remains_full_text(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """无章节标题原文显示全文范围，不因内部 ref 伪装成章节证据。

    Args:
        tmp_path: 隔离工作区。
        monkeypatch: 入库后阻断 Runtime 命令执行。
    Returns:
        无。
    Raises:
        AssertionError: 全文回退漏正文或被错误标成章节。
    """
    runtime = DefaultFinsRuntime.create(workspace_root=tmp_path)
    body = "Jane Doe - Chief Executive Officer\n\nRevenue reached 42 million dollars. Customer demand improved.\n"
    document_id = _upload_material(runtime, tmp_path, body=body)
    monkeypatch.setattr(runtime, "execute", _reject_runtime_command)
    host = _ReadOnlyHost()
    service = _ReadOnlyFinsService(host, runtime)
    document = service.resolve_document_reference(DocumentReference("AAPL", document_id))
    assert document.selected_section is None
    assert len(document.sections) == 1
    full = service.resolve_document_reference(DocumentReference("AAPL", document_id, document.sections[0].ref))
    assert full.selected_section is not None
    assert full.selected_section.contains_full_text
    assert "Revenue reached 42 million" in full.selected_section.content
    _assert_read_only(_StoredReference(runtime, service, host, document_id))


@pytest.mark.parametrize("state", ["deleted", "incomplete"])
def test_current_source_state_rejects_cached_reference(stored_reference: _StoredReference, state: str) -> None:
    """已读取后的材料删除或撤销完整入库状态，不能继续展示缓存原文。

    Args:
        stored_reference: 已准备的真实材料。
        state: 要模拟的真实仓储状态变更。
    Returns:
        无。
    Raises:
        AssertionError: 已删除或不完整材料仍可解析、列出。
    """
    _question_view(stored_reference)
    assert len(stored_reference.service.list_materials("AAPL")) == 1
    repository = stored_reference.runtime.source_repository
    if state == "deleted":
        repository.delete_source_document(
            SourceDocumentStateChangeRequest("AAPL", stored_reference.document_id, SourceKind.MATERIAL.value)
        )
        with pytest.raises(FileNotFoundError, match="删除|不存在|not found"):
            stored_reference.service.resolve_document_reference(DocumentReference("AAPL", stored_reference.document_id))
    else:
        repository.update_source_document(
            SourceDocumentUpsertRequest(
                ticker="AAPL",
                document_id=stored_reference.document_id,
                internal_document_id="reference-incomplete",
                meta={"ingest_complete": False},
            ),
            SourceKind.MATERIAL,
        )
        with pytest.raises(ValueError, match="完整入库"):
            stored_reference.service.resolve_document_reference(DocumentReference("AAPL", stored_reference.document_id))
    assert stored_reference.service.list_materials("AAPL") == []
    _assert_read_only(stored_reference)


@pytest.mark.parametrize(
    "reference",
    [
        DocumentReference("../AAPL", "mat_0001"),
        DocumentReference("AAPL", "../mat_0001"),
        DocumentReference("AAPL", "mat_0001", "../section"),
        DocumentReference("AAPL", "mat_0001", "/etc/passwd"),
        DocumentReference("AAPL", "mat_0001", "section%2F.."),
        DocumentReference("AAPL", "mat_0001", ""),
        DocumentReference("", "mat_0001"),
        DocumentReference("AAPL", ""),
        DocumentReference("AAPL", "mat_0001", "x" * 257),
    ],
)
def test_invalid_reference_rejected_without_submit(tmp_path: Path, reference: DocumentReference) -> None:
    """无效定位与路径穿越在原文读取前明确拒绝，不创建宿主会话。

    Args:
        tmp_path: 无材料的隔离工作区。
        reference: 非法原文定位请求。
    Returns:
        无。
    Raises:
        AssertionError: 参数被接受或触发命令提交。
    """
    runtime = DefaultFinsRuntime.create(workspace_root=tmp_path)
    host = _ReadOnlyHost()
    service = _ReadOnlyFinsService(host, runtime)
    with pytest.raises(ValueError, match="参数非法"):
        service.resolve_document_reference(reference)
    _assert_read_only(_StoredReference(runtime, service, host, "unused"))


def test_unknown_section_ref_fails_explicitly(stored_reference: _StoredReference) -> None:
    """格式合法但不存在的 ref 不能静默回退全文或第一章节。

    Args:
        stored_reference: 已准备的真实材料。
    Returns:
        无。
    Raises:
        AssertionError: 未知章节被静默替换。
    """
    with pytest.raises(ToolArgumentError, match="章节不存在"):
        stored_reference.service.resolve_document_reference(
            DocumentReference("AAPL", stored_reference.document_id, "nonexistent-section")
        )
    _assert_read_only(stored_reference)
