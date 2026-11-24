"""跨公司文档归属的服务与工具错误契约回归测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from dayu.engine.processors.processor_registry import ProcessorRegistry
from dayu.engine.tool_errors import ToolBusinessError
from dayu.engine.tool_registry import ToolRegistry
from dayu.engine.tools.error_contract import ErrorCode
from dayu.fins.domain.document_models import CompanyMeta, SourceDocumentUpsertRequest
from dayu.fins.domain.enums import SourceKind
from dayu.fins.tools.fins_tools import register_fins_read_tools
from dayu.fins.tools.service import FinsToolService
from tests.fins.storage_testkit import FsStorageTestContext, build_fs_storage_test_context

_OWNER = "GOOGL"
_TARGET = "AMZN"
_DOCUMENT = "mat_google_cloud"
_MISSING = "mat_missing"


@pytest.fixture
def storage(tmp_path: Path) -> FsStorageTestContext:
    """准备两个公司的真实窄仓储，避免测试桩忽略 ticker。

    参数:
        tmp_path: 隔离工作区。
    返回值:
        共享文件存储的仓储集合。
    异常:
        OSError: 测试仓储写入失败。
    """
    context = build_fs_storage_test_context(tmp_path)
    for ticker in (_OWNER, _TARGET):
        context.company_repository.upsert_company_meta(CompanyMeta(
            company_id=ticker, company_name=ticker, ticker=ticker, market="US",
            resolver_version="test", updated_at="2026-11-24T00:00:00Z",
        ))
    return context


def _create_document(storage: FsStorageTestContext, ticker: str, source_kind: SourceKind) -> None:
    """通过仓储写入归属证据，无需构建处理器。

    参数:
        storage: 测试仓储。
        ticker: 文档归属公司。
        source_kind: 源文档类别。
    返回值:
        无。
    异常:
        OSError: 文档元数据写入失败。
    """
    storage.source_repository.create_source_document(
        SourceDocumentUpsertRequest(ticker=ticker, document_id=_DOCUMENT, internal_document_id=_DOCUMENT),
        source_kind,
    )


def _service(storage: FsStorageTestContext) -> FinsToolService:
    """装配真实服务，空处理器注册表可检测错误路径的意外解析。

    参数:
        storage: 测试仓储。
    返回值:
        文档工具服务。
    异常:
        无。
    """
    return FinsToolService(
        company_repository=storage.company_repository,
        source_repository=storage.source_repository,
        processed_repository=storage.processed_repository,
        processor_registry=ProcessorRegistry(),
    )


@pytest.mark.parametrize("source_kind", [SourceKind.FILING, SourceKind.MATERIAL])
def test_service_rejects_cross_company_document(storage: FsStorageTestContext, source_kind: SourceKind) -> None:
    """未热身缓存时也能区分他公司文档和不存在的文档。

    参数:
        storage: 测试仓储。
        source_kind: 覆盖财报和补充材料。
    返回值:
        无。
    异常:
        AssertionError: 归属、错误分类或恢复步骤不正确。
    """
    _create_document(storage, _OWNER, source_kind)
    service = _service(storage)
    with pytest.raises(ToolBusinessError) as caught:
        service.get_document_sections(ticker=" amzn ", document_id=_DOCUMENT)
    assert caught.value.code == ErrorCode.INVALID_ARGUMENT
    assert f"document_id `{_DOCUMENT}`" in caught.value.message
    assert f"属于 {_OWNER}" in caught.value.message
    assert f"ticker={_TARGET}" in caught.value.message
    assert "list_documents" in caught.value.hint and _TARGET in caught.value.hint
    with pytest.raises(FileNotFoundError, match="Document not found"):
        service.get_document_sections(ticker=_TARGET, document_id=_MISSING)


def test_document_ownership_does_not_require_company_meta(storage: FsStorageTestContext) -> None:
    """源文档存在即可提供归属证据，公司元数据缺失不应掩盖它。

    参数:
        storage: 测试仓储。
    返回值:
        无。
    异常:
        AssertionError: 文档被误报为不存在。
    """
    _create_document(storage, "MSFT", SourceKind.MATERIAL)
    with pytest.raises(ToolBusinessError, match="属于 MSFT"):
        _service(storage).get_document_sections(ticker=_TARGET, document_id=_DOCUMENT)


def test_document_shared_by_multiple_companies_reports_all_owners(storage: FsStorageTestContext) -> None:
    """同名 ID 不是全局唯一键，诊断须列明所有确证归属。

    参数:
        storage: 测试仓储。
    返回值:
        无。
    异常:
        AssertionError: 多归属被武断收敛为一个公司。
    """
    for ticker in (_OWNER, "MSFT"):
        _create_document(storage, ticker, SourceKind.MATERIAL)
    with pytest.raises(ToolBusinessError) as caught:
        _service(storage).get_document_sections(ticker=_TARGET, document_id=_DOCUMENT)
    assert _OWNER in caught.value.message and "MSFT" in caught.value.message


@pytest.mark.parametrize("source_kind", [SourceKind.FILING, SourceKind.MATERIAL])
def test_current_company_document_wins_without_inventory_scan(
    storage: FsStorageTestContext, source_kind: SourceKind, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """正常读取保持当前公司优先，并避免全局扫描开销。

    参数:
        storage: 测试仓储。
        source_kind: 文档来源类别。
        monkeypatch: 替换不应被调用的盘点方法。
    返回值:
        无。
    异常:
        AssertionError: 正常读取扫描了其他公司或解析错误。
    """
    _create_document(storage, _OWNER, source_kind)
    _create_document(storage, _TARGET, source_kind)
    monkeypatch.setattr(storage.company_repository, "scan_company_meta_inventory", _unexpected_inventory)
    assert _service(storage)._resolve_source_kind(ticker=_TARGET, document_id=_DOCUMENT) == source_kind


def _unexpected_inventory() -> None:
    """阻止正常读取路径触发跨公司扫描。

    参数:
        无。
    返回值:
        不返回。
    异常:
        AssertionError: 意外执行全局扫描。
    """
    raise AssertionError("正常读取不应扫描其他公司")


@pytest.mark.parametrize(("tool_name", "extra"), [
    ("get_document_sections", {}),
    ("search_document", {"query": "cloud"}),
    ("read_section", {"ref": "s_0001"}),
    ("list_tables", {}),
    ("get_table", {"table_ref": "t_0001"}),
    ("get_page_content", {"page_no": 1}),
    ("get_financial_statement", {"statement_type": "income"}),
    ("query_xbrl_facts", {}),
])
def test_all_document_tools_return_actionable_ownership_error(
    storage: FsStorageTestContext, tool_name: str, extra: dict[str, str | int],
) -> None:
    """所有文档读取工具透出可操作的归属错误，并保留真正缺失的语义。

    参数:
        storage: 测试仓储。
        tool_name: 已注册的文档工具。
        extra: 工具所需的额外参数。
    返回值:
        无。
    异常:
        AssertionError: 错误被静默吞掉或变成模糊执行失败。
    """
    _create_document(storage, _OWNER, SourceKind.MATERIAL)
    registry = ToolRegistry()
    register_fins_read_tools(registry, service=_service(storage))
    result = registry.execute(tool_name, {"ticker": _TARGET, "document_id": _DOCUMENT, **extra})
    assert result["ok"] is False
    assert result["error"] == ErrorCode.INVALID_ARGUMENT
    assert _OWNER in result["message"] and _TARGET in result["message"]
    assert "list_documents" in result["hint"]
    missing = registry.execute(tool_name, {"ticker": _TARGET, "document_id": _MISSING, **extra})
    assert missing["ok"] is False
    assert missing["error"] == "file_not_found"
    assert "Document not found" in missing["hint"]
