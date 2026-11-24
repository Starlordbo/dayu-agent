"""真实 Agent 消费耗尽结果并保留终态 side message 的离线集成测试。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

from dayu.contracts.agent_types import AgentMessage, JsonValue
from dayu.contracts.protocols import ToolExecutionContext, ToolExecutor
from dayu.engine.async_agent import AgentRunningConfig, AsyncAgent
from dayu.engine.events import (
    EventType, StreamEvent, content_complete, content_delta, done_event,
    tool_call_dispatched, tool_call_result, tool_calls_batch_done,
)
from dayu.engine.tool_registry import ToolRegistry
from dayu.host.events import build_app_event_from_stream_event
from dayu.web.streamlit.pages.chat.utils import fold_app_events_to_assistant_text
from tests.fins.test_retrieval_budget import build_budget_registry

_ANSWER = "根据现有证据作答，尚未覆盖的财务数据需另行核实。"


class BudgetRunner:
    """先超预算、再检查上下文并作答的离线 Runner。"""

    def __init__(self) -> None:
        """初始化脚本状态。

        参数:
            无。
        返回值:
            无。
        异常:
            无。
        """
        self.executor: ToolExecutor | None = None
        self.calls = 0

    def set_tools(self, executor: ToolExecutor | None) -> None:
        """接收工具执行器。

        参数:
            executor: Agent 配置的工具入口。
        返回值:
            无。
        异常:
            无。
        """
        self.executor = executor

    def is_supports_tool_calling(self) -> bool:
        """声明支持工具。

        参数:
            无。
        返回值:
            True。
        异常:
            无。
        """
        return True

    async def close(self) -> None:
        """关闭无外部资源的脚本。

        参数:
            无。
        返回值:
            无。
        异常:
            无。
        """

    async def call(self, messages: list[AgentMessage], *, stream: bool = True,
                   **extra_payloads: JsonValue) -> AsyncIterator[StreamEvent]:
        """产生真实工具事件，下一轮验证耗尽结果并回答。

        参数:
            messages: 模型上下文。
            stream: 流式标记。
            extra_payloads: Runner 协议的执行上下文。
        返回值:
            工具及答案事件流。
        异常:
            AssertionError: Agent 未传递完整结果或提前降级。
        """
        del stream
        self.calls += 1
        assert self.executor is not None
        if self.calls == 2:
            assert "budget_exhausted" in str(messages) and "covered_scope" in str(messages)
            yield content_delta(_ANSWER)
            yield done_event()
            return
        assert self.calls == 1
        yield content_complete("")
        trace_context = extra_payloads["trace_context"]
        assert isinstance(trace_context, dict)
        call_ids: list[str] = []
        for index, ticker in enumerate(("AAPL", "AAPL", "AAPL", "MSFT")):
            call_id = f"budget_{index}"
            call_ids.append(call_id)
            arguments: dict[str, JsonValue] = {"ticker": ticker, "fiscal_years": [2026 - index]}
            yield tool_call_dispatched(call_id, "list_documents", arguments, index_in_iteration=index)
            result = self.executor.execute("list_documents", arguments, ToolExecutionContext(
                run_id=str(trace_context["run_id"]), tool_call_id=call_id,
            ))
            if index in (1, 2):
                assert result["value"]["status"] == "budget_exhausted"
            else:
                assert result["value"]["company"]["ticker"] == ticker
            yield tool_call_result(call_id, result, name="list_documents", arguments=arguments, index_in_iteration=index)
        yield tool_calls_batch_done(call_ids, ok=4, error=0, timeout=0, cancelled=0)
        yield done_event()


async def _run_agent(registry: ToolRegistry, runner: BudgetRunner) -> list[StreamEvent]:
    """收集真实 Agent 事件。

    参数:
        registry: 财报工具入口。
        runner: 离线模型脚本。
    返回值:
        完整事件序列。
    异常:
        AssertionError: 脚本协议检查失败。
    """
    agent = AsyncAgent(runner, tool_executor=registry, running_config=AgentRunningConfig(max_iterations=4))
    return [event async for event in agent.run("比较 AAPL 与 MSFT")]


def test_agent_answers_after_exhaustion_with_visible_notice(tmp_path: Path) -> None:
    """超预算后自然回答，告警经生产映射进入终态反馈且不重复。

    参数:
        tmp_path: 仓储目录。
    返回值:
        无。
    异常:
        AssertionError: 答案、告警或隔离不符合契约。
    """
    registry, _ = build_budget_registry(tmp_path, 1)
    runner = BudgetRunner()
    events = asyncio.run(_run_agent(registry, runner))
    assert runner.calls == 2
    assert events[-1].type == EventType.FINAL_ANSWER
    assert events[-1].data["degraded"] is False
    app_events = [mapped for event in events if (mapped := build_app_event_from_stream_event(event)) is not None]
    answer, side_messages, filtered = fold_app_events_to_assistant_text(app_events)
    assert answer == _ANSWER and filtered is False
    assert len(side_messages) == 1
    assert "AAPL" in side_messages[0] and "检索预算" in side_messages[0]
    assert "现有证据" in side_messages[0]
