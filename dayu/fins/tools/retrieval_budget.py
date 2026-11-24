"""财报工具调用边界的累计检索预算；与共享服务及输出截断互相独立。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from threading import Lock
from typing import Final, Literal, Protocol, TypeVar, TypedDict

from dayu.contracts.agent_types import JsonValue
from dayu.contracts.protocols import ToolExecutionContext
from dayu.contracts.tool_configs import validate_retrieval_budget
from dayu.engine.tool_contracts import TerminalToolResult

_Result = TypeVar("_Result")
_EXHAUSTED: Final = "budget_exhausted"
_HINT = (
    "该公司本次运行的检索预算已耗尽，请停止非结构化检索，基于现有证据直接作答并明确未覆盖项。"
    "如仍缺财务数值，可使用 get_financial_statement 或 query_xbrl_facts；不要反复尝试已耗尽工具。"
    "covered_scope 仅记录尝试范围，returned 表示调用已返回，不代表已完整覆盖；failed/pending 不可当作证据。"
)


class TickerResolver(Protocol):
    """由财报服务提供统一公司标识解析，预算不访问仓储。"""

    def __call__(self, *, ticker: str, tool_name: str) -> str:
        """解析仓储中的规范公司标识。

        参数:
            ticker: 外部公司标识或别名。
            tool_name: 调用工具名。
        返回值:
            规范 ticker。
        异常:
            ValueError: 标识非法。
        """
        ...


class CoveredScope(TypedDict):
    """单次已准入调用的请求范围与实际执行状态。"""

    tool_name: str
    arguments: dict[str, JsonValue]
    outcome: Literal["pending", "returned", "failed"]


class BudgetUsage(TypedDict):
    """计数快照。"""

    limit: int
    used: int
    remaining: int


class BudgetExhaustedResult(TypedDict):
    """预算正常耗尽结果，属于业务终态而非执行异常。"""

    status: Literal["budget_exhausted"]
    ticker: str
    budget: BudgetUsage
    covered_scope: list[CoveredScope]
    hint: str
    user_notice: str


@dataclass
class _CompanyBudget:
    """单次运行中一家公司的已准入调用记录。"""

    calls: list[CoveredScope] = field(default_factory=list)


class RetrievalBudget:
    """工具集私有、按 Host run_id 和规范 ticker 隔离的线程安全预算。"""

    def __init__(self, limit: int, resolve_ticker: TickerResolver) -> None:
        """建立独立预算账本。

        参数:
            limit: 每公司允许准入的调用数。
            resolve_ticker: 与财报服务同源的标识解析器。
        返回值:
            无。
        异常:
            ValueError: 预算不是正整数。
        """
        self._limit = validate_retrieval_budget(limit)
        self._resolve_ticker = resolve_ticker
        self._companies: dict[tuple[str | None, str], _CompanyBudget] = {}
        self._lock = Lock()

    def execute(
        self,
        *,
        ticker: str,
        tool_name: str,
        arguments: Mapping[str, JsonValue],
        operation: Callable[[], _Result],
        context: ToolExecutionContext | None,
    ) -> _Result | TerminalToolResult[BudgetExhaustedResult]:
        """原子预留额度，再执行读取；耗尽时不调用读取操作。

        参数:
            ticker: 原始公司标识。
            tool_name: 检索工具名。
            arguments: 仅用于覆盖范围回显的请求参数，不用于执行转发。
            operation: 已显式绑定参数的财报读取操作。
            context: Host 注入的上下文；直接调用无上下文时以工具集为作用域。
        返回值:
            原样读取结果，或带范围及建议的预算耗尽结果。
        异常:
            Exception: 原有解析或读取异常原样交由 ToolRegistry 处理；已准入失败也消耗额度。
        """
        canonical = self._resolve_ticker(ticker=ticker, tool_name=tool_name)
        key = (context.run_id if context is not None else None, canonical)
        with self._lock:
            company = self._companies.setdefault(key, _CompanyBudget())
            if len(company.calls) >= self._limit:
                exhausted = BudgetExhaustedResult(
                    status=_EXHAUSTED,
                    ticker=canonical,
                    budget=BudgetUsage(limit=self._limit, used=len(company.calls), remaining=0),
                    covered_scope=deepcopy(company.calls),
                    hint=_HINT,
                    user_notice=(
                        f"{canonical} 检索预算已耗尽（{self._limit}/{self._limit} 次），"
                        "后续非结构化检索已停止；将基于现有证据作答，未覆盖项需明确说明。"
                    ),
                )
                return TerminalToolResult(value=exhausted, user_notice=exhausted["user_notice"])
            # 先记 pending 再释放锁，防止同一批并发调用越过上限；读取期间不持锁。
            coverage = CoveredScope(tool_name=tool_name, arguments=deepcopy(dict(arguments)), outcome="pending")
            company.calls.append(coverage)
        try:
            result = operation()
        except BaseException:
            with self._lock:
                coverage["outcome"] = "failed"
            raise
        with self._lock:
            coverage["outcome"] = "returned"
        return result
