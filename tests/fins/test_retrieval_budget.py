"""检索预算在真实注册入口上的契约、隔离及并发测试。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from dayu.contracts.protocols import ToolExecutionContext
from dayu.contracts.tool_configs import FinsToolLimits
from dayu.engine.processors.processor_registry import ProcessorRegistry
from dayu.engine.tool_registry import ToolRegistry
from dayu.engine.tool_result import project_for_llm
from dayu.fins.domain.document_models import CompanyMeta
from dayu.fins.tools.fins_tools import register_fins_read_tools
from dayu.fins.tools.service import FinsToolService
from tests.fins.storage_testkit import build_fs_storage_test_context


def build_budget_registry(tmp_path: Path, limit: int = 2) -> tuple[ToolRegistry, FinsToolService]:
    """装配真实仓储及工具入口。

    参数:
        tmp_path: 隔离仓储目录。
        limit: 每公司预算。
    返回值:
        工具注册表与可复用服务。
    异常:
        OSError: 仓储初始化失败。
    """
    storage = build_fs_storage_test_context(tmp_path)
    for ticker in ("AAPL", "MSFT"):
        storage.company_repository.upsert_company_meta(CompanyMeta(
            company_id=ticker, company_name=ticker, ticker=ticker, market="US",
            ticker_aliases=["APPLE"] if ticker == "AAPL" else [],
            resolver_version="test", updated_at="2026-11-24T00:00:00Z",
        ))
    service = FinsToolService(
        company_repository=storage.company_repository,
        source_repository=storage.source_repository,
        processed_repository=storage.processed_repository,
        processor_registry=ProcessorRegistry(),
    )
    registry = ToolRegistry()
    register_fins_read_tools(registry, service=service, limits=FinsToolLimits(retrieval_budget_per_ticker=limit))
    return registry, service


def test_exhaustion_is_structured_and_companies_are_independent(tmp_path: Path) -> None:
    """已覆盖范围及建议进入模型投影，别名共用预算、另一公司独立。

    参数:
        tmp_path: 隔离目录。
    返回值:
        无。
    异常:
        AssertionError: 契约或隔离不成立。
    """
    registry, _ = build_budget_registry(tmp_path)
    assert registry.execute("list_documents", {"ticker": "aapl.us"})["value"]["company"]["ticker"] == "AAPL"
    assert registry.execute("list_documents", {"ticker": "APPLE", "fiscal_years": [2025]})["ok"] is True
    result = registry.execute("search_document", {"ticker": "AAPL", "document_id": "missing", "query": "revenue"})
    assert result["ok"] is True
    value = project_for_llm(result)
    assert value["status"] == "budget_exhausted"
    assert value["ticker"] == "AAPL"
    assert value["budget"] == {"limit": 2, "used": 2, "remaining": 0}
    assert len(value["covered_scope"]) == 2
    assert value["covered_scope"][1]["arguments"]["fiscal_years"] == [2025]
    assert all(item["outcome"] == "returned" for item in value["covered_scope"])
    assert "get_financial_statement" in value["hint"] and "query_xbrl_facts" in value["hint"]
    assert "现有证据" in value["hint"] and "AAPL" in value["user_notice"]
    assert "truncation" not in result
    assert registry.execute("list_documents", {"ticker": "MSFT"})["value"]["company"]["ticker"] == "MSFT"


@pytest.mark.parametrize(("name", "arguments"), [
    ("get_document_sections", {"document_id": "missing"}),
    ("read_section", {"document_id": "missing", "ref": "s1"}),
    ("search_document", {"document_id": "missing", "query": "revenue"}),
    ("list_tables", {"document_id": "missing"}),
    ("get_table", {"document_id": "missing", "table_ref": "t1"}),
    ("get_page_content", {"document_id": "missing", "page_no": 1}),
])
def test_all_unstructured_tools_share_budget(tmp_path: Path, name: str, arguments: dict[str, str | int]) -> None:
    """失败尝试也计数，耗尽后不再进入文档处理。

    参数:
        tmp_path: 隔离目录。
        name: 检索工具名。
        arguments: 合法但文档缺失的参数。
    返回值:
        无。
    异常:
        AssertionError: 工具漏计数或失败被误记为已取证。
    """
    registry, _ = build_budget_registry(tmp_path, 1)
    assert registry.execute(name, {"ticker": "AAPL", **arguments})["ok"] is False
    result = registry.execute(name, {"ticker": "AAPL", **arguments})["value"]
    assert result["status"] == "budget_exhausted"
    assert result["covered_scope"][0]["outcome"] == "failed"


def test_structured_tools_remain_available_and_do_not_consume_budget(tmp_path: Path) -> None:
    """两种结构化取数不共享检索预算，耗尽后仍执行原有路径。

    参数:
        tmp_path: 隔离目录。
    返回值:
        无。
    异常:
        AssertionError: 结构化调用被预算拦截。
    """
    registry, _ = build_budget_registry(tmp_path, 1)
    for _ in range(2):
        for name, arguments in (
            ("get_financial_statement", {"statement_type": "income"}),
            ("query_xbrl_facts", {}),
        ):
            result = registry.execute(name, {"ticker": "AAPL", "document_id": "missing", **arguments})
            assert result["ok"] is False and result["error"] != "budget_exhausted"
        result = registry.execute("list_documents", {"ticker": "AAPL"})
        if _ == 0:
            assert result["value"]["company"]["ticker"] == "AAPL"
        else:
            assert result["value"]["status"] == "budget_exhausted"


def test_parallel_calls_and_run_isolation(tmp_path: Path) -> None:
    """并发准入原子化，新的 run 和工具集不继承缓存服务的计数。

    参数:
        tmp_path: 隔离目录。
    返回值:
        无。
    异常:
        AssertionError: 并发超发或跨运行泄漏。
    """
    registry, service = build_budget_registry(tmp_path)
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(registry.execute, "list_documents", {"ticker": "AAPL"}, ToolExecutionContext(run_id="one"))
                   for _ in range(20)]
        values = [future.result()["value"] for future in futures]
    assert sum("company" in value for value in values) == 2
    assert sum(value.get("status") == "budget_exhausted" for value in values) == 18
    assert registry.execute("list_documents", {"ticker": "AAPL"}, ToolExecutionContext(run_id="two"))["value"]["company"]
    fresh = ToolRegistry()
    register_fins_read_tools(fresh, service=service, limits=FinsToolLimits(retrieval_budget_per_ticker=2))
    assert fresh.execute("list_documents", {"ticker": "AAPL"}, ToolExecutionContext(run_id="one"))["value"]["company"]


def test_exhaustion_bypasses_content_truncation(tmp_path: Path) -> None:
    """极小内容上限不得截断治理结果或生成续读游标。

    参数:
        tmp_path: 隔离目录。
    返回值:
        无。
    异常:
        AssertionError: 提示或覆盖范围被当作正文截断。
    """
    _, service = build_budget_registry(tmp_path)
    registry = ToolRegistry()
    register_fins_read_tools(registry, service=service, limits=FinsToolLimits(
        retrieval_budget_per_ticker=2, read_section_max_chars=1, search_document_max_items=1,
    ))
    for _ in range(2):
        registry.execute("list_documents", {"ticker": "AAPL"})
    for name, arguments in (("read_section", {"ref": "s1"}), ("search_document", {"query": "revenue"})):
        result = registry.execute(name, {"ticker": "AAPL", "document_id": "missing", **arguments})
        assert result["value"]["status"] == "budget_exhausted"
        assert len(result["value"]["covered_scope"]) == 2
        assert "现有证据" in result["value"]["hint"]
        assert "truncation" not in result
