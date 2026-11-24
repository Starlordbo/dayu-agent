"""真实 Host 的启动身份、取消收尾与 UI 超时闭环回归。"""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import AsyncIterator, Callable

import pytest

from dayu.contracts.agent_execution import AgentCreateArgs, AgentInput, ExecutionContract
from dayu.contracts.agent_types import AgentMessage, JsonValue
from dayu.contracts.cancellation import CancellationToken
from dayu.contracts.events import AppEvent, AppEventType
from dayu.contracts.protocols import ToolExecutor
from dayu.contracts.run import RunRecord, RunState
from dayu.contracts.session import SessionSource
from dayu.engine.events import StreamEvent, content_complete
from dayu.host import agent_builder
from dayu.host.executor import DefaultHostExecutor
from dayu.host.concurrency import HOST_AGENT_LANE, SQLiteConcurrencyGovernor
from dayu.host.host import Host
from dayu.host.host_execution import HostedRunContext
from dayu.host.host_store import HostStore
from dayu.host.prepared_turn import PreparedAgentTurnSnapshot
from dayu.host.run_registry import SQLiteRunRegistry
from dayu.host.scene_preparer import PreparedAgentExecution
from dayu.host.session_registry import SQLiteSessionRegistry
from dayu.services.chat_service import ChatService
from dayu.services.chat_stream_config import ChatStreamConfig
from dayu.services.contracts import ChatTurnRequest
from dayu.web.streamlit.pages.chat import stream_runtime
from tests.application.test_chat_service import _build_service
from tests.application.test_host_executor import _build_prepared_execution
from dayu.engine.tool_registry import ToolRegistry

_WAIT_SECONDS = 5.0
_POLL_SECONDS = 0.01
_SESSION_ID = "cancel-contract-session"

@dataclass
class _Preparation:
    """可选择阻塞的准备器；事件用于直接验证取消前后边界。"""

    blocked: bool
    entered: threading.Event
    exited: threading.Event

    async def prepare(self, execution_contract: ExecutionContract, run_context: HostedRunContext) -> PreparedAgentExecution:
        """准备输入。

        参数: execution_contract 为执行契约，run_context 为 Host 运行上下文。
        返回值: 绑定 Host 令牌的输入。
        异常: asyncio.CancelledError 表示准备子任务被取消。
        """
        self.entered.set()
        try:
            if self.blocked:
                await asyncio.Future[None]()
            prepared = _build_prepared_execution(execution_contract=execution_contract)
            return replace(prepared, agent_input=replace(
                prepared.agent_input, cancellation_handle=run_context.cancellation_token,
            ))
        finally:
            self.exited.set()

    async def restore_prepared_execution(self, prepared_turn: PreparedAgentTurnSnapshot, run_context: HostedRunContext) -> AgentInput:
        """拒绝本测试不使用的恢复路径。

        参数: prepared_turn 为快照，run_context 为运行上下文。
        返回值: 无。
        异常: NotImplementedError 始终抛出。
        """
        raise NotImplementedError


@dataclass
class _Runner:
    """没有首输出但持续执行工具工作的假 runner。"""

    entered: threading.Event
    closed: threading.Event
    token: CancellationToken | None = None
    tool_calls: int = 0
    finish: bool = False
    tools: ToolRegistry = field(default_factory=ToolRegistry)

    def build(self, args: AgentCreateArgs, *, cancellation_token: CancellationToken | None = None) -> _Runner:
        """绑定 Host 令牌。

        参数: args 为构造参数，cancellation_token 为 Host 令牌。
        返回值: 当前 runner。
        异常: 无。
        """
        self.token = cancellation_token
        self.tools.register("count", self.count_tool, {
            "type": "function", "function": {"name": "count", "description": "计数", "parameters": {"type": "object", "properties": {}}},
        })
        return self

    async def call(self, messages: list[AgentMessage], *, stream: bool = True, **extra_payloads: JsonValue) -> AsyncIterator[StreamEvent]:
        """模拟无输出工具工作，直到取消或自然完成。

        参数: messages 为消息，stream 为流模式，extra_payloads 为调用选项。
        返回值: 自然完成时的一条完整内容事件。
        异常: CancelledError 由 Host 令牌触发。
        """
        self.entered.set()
        assert self.token is not None
        while not self.finish:
            self.token.raise_if_cancelled()
            assert self.tools.execute("count", {})["ok"]
            await asyncio.sleep(_POLL_SECONDS)
        yield content_complete("finished")

    def count_tool(self) -> int:
        """执行经 ToolRegistry 调用的计数工具。

        参数: 无。
        返回值: 当前调用次数。
        异常: 无。
        """
        self.tool_calls += 1
        return self.tool_calls

    def set_tools(self, executor: ToolExecutor | None) -> None:
        """接受工具装配。

        参数: executor 为工具执行器。
        返回值: 无。
        异常: 无。
        """

    def is_supports_tool_calling(self) -> bool:
        """返回工具支持状态。

        参数: 无。
        返回值: False，假 runner 内部经 ToolRegistry 调用计数工具。
        异常: 无。
        """
        return False

    async def close(self) -> None:
        """记录 runner 已释放。

        参数: 无。
        返回值: 无。
        异常: 无。
        """
        self.closed.set()


@dataclass
class _Harness:
    """真实 Host、注册表、Service 与可控准备器。"""

    host: Host
    registry: SQLiteRunRegistry
    executor: DefaultHostExecutor
    service: ChatService
    preparation: _Preparation


def _build_harness(tmp_path: Path, *, blocked: bool = False) -> _Harness:
    """创建真实 SQLite Host 与 Service。

    参数: tmp_path 为隔离目录，blocked 指定准备阶段是否阻塞。
    返回值: 测试装配结果。
    异常: SQLite 初始化异常透传。
    """
    store = HostStore(tmp_path / "host.db")
    store.initialize_schema()
    sessions = SQLiteSessionRegistry(store)
    sessions.create_session(source=SessionSource.API, session_id=_SESSION_ID)
    registry = SQLiteRunRegistry(store)
    preparation = _Preparation(blocked, threading.Event(), threading.Event())
    executor = DefaultHostExecutor(run_registry=registry, scene_preparation=preparation,
                                   cancellation_bridge_poll_interval_seconds=_POLL_SECONDS)
    host = Host(executor=executor, session_registry=sessions, run_registry=registry)
    service, _ = _build_service()
    service.host = host
    service.chat_stream_config = ChatStreamConfig(first_chunk_timeout_seconds=_POLL_SECONDS)
    return _Harness(host, registry, executor, service, preparation)


async def _collect(stream: AsyncIterator[AppEvent]) -> list[AppEvent]:
    """耗尽事件流以等待 Host finally。

    参数: stream 为事件流。
    返回值: 全部事件。
    异常: 流异常透传。
    """
    return [event async for event in stream]


@pytest.mark.asyncio
async def test_identity_precedes_blocking_preparation_and_cancel_settles(tmp_path: Path) -> None:
    """准备阻塞前可精确取消；终态与资源收尾均可确认。

    参数: tmp_path 为隔离目录。
    返回值: 无。
    异常: 断言失败。
    """
    harness = _build_harness(tmp_path, blocked=True)
    submission = await harness.service.submit_turn(ChatTurnRequest(user_text="test", session_id=_SESSION_ID))
    started = await asyncio.wait_for(anext(submission.event_stream), _WAIT_SECONDS)
    assert started.type.value == "run_started"
    assert not harness.preparation.entered.is_set()
    run_id = str(started.payload["run_id"])
    pending = asyncio.create_task(_collect(submission.event_stream))
    while not harness.preparation.entered.is_set():
        await asyncio.sleep(_POLL_SECONDS)
    harness.host.cancel_run(run_id)
    events = await asyncio.wait_for(pending, _WAIT_SECONDS)
    run = harness.host.get_run(run_id)
    assert run is not None and run.state == RunState.CANCELLED and run.completed_at is not None
    assert harness.preparation.exited.is_set()
    assert len([event for event in events if event.type == AppEventType.CANCELLED]) == 1
    assert run_id not in harness.executor._run_resources


@pytest.mark.parametrize("cancel_first", [True, False])
def test_cancel_and_complete_use_first_committed_decision(tmp_path: Path, cancel_first: bool) -> None:
    """取消意图与自然完成共用数据库事务裁决，终态时间不重写。

    参数: tmp_path 为隔离目录，cancel_first 指定先到者。
    返回值: 无。
    异常: 断言失败。
    """
    harness = _build_harness(tmp_path)
    run = harness.registry.register_run(session_id=_SESSION_ID, service_type="test")
    harness.registry.start_run(run.run_id)
    if cancel_first:
        assert harness.registry.request_cancel(run.run_id)
    settled = harness.registry.complete_run(run.run_id)
    if not cancel_first:
        assert not harness.registry.request_cancel(run.run_id)
    assert settled.state == (RunState.CANCELLED if cancel_first else RunState.SUCCEEDED)
    assert settled.completed_at is not None
    assert harness.executor._complete_run_preserving_terminal_state(run_id=run.run_id).completed_at == settled.completed_at


@pytest.mark.asyncio
async def test_cancel_waiting_session_lock_creates_no_run(tmp_path: Path) -> None:
    """锁等待期只取消本次提交，不创建 run、不释放他人锁。

    参数: tmp_path 为隔离目录。
    返回值: 无。
    异常: 断言失败。
    """
    harness = _build_harness(tmp_path)
    lock = harness.service._lock_for_session(_SESSION_ID)
    await lock.acquire()
    token = CancellationToken()
    submission = await harness.service.submit_turn(ChatTurnRequest(
        user_text="test", session_id=_SESSION_ID, cancellation_token=token,
    ))
    task = asyncio.create_task(_collect(submission.event_stream))
    await asyncio.sleep(_POLL_SECONDS)
    token.cancel()
    events = await asyncio.wait_for(task, _WAIT_SECONDS)
    assert [event.type for event in events] == [AppEventType.CANCELLED]
    assert harness.host.list_runs(session_id=_SESSION_ID) == []
    assert lock.locked()
    lock.release()


@pytest.mark.asyncio
async def test_cancel_on_start_notification_skips_preparation(tmp_path: Path) -> None:
    """启动通知处取消应收敛 CREATED run，且不进入准备。

    参数: tmp_path 为隔离目录。
    返回值: 无。
    异常: 断言失败。
    """
    harness = _build_harness(tmp_path, blocked=True)
    token = CancellationToken()
    submission = await harness.service.submit_turn(ChatTurnRequest(
        user_text="test", session_id=_SESSION_ID, cancellation_token=token,
    ))
    started = await anext(submission.event_stream)
    token.cancel()
    events = await _collect(submission.event_stream)
    run = harness.host.get_run(str(started.payload["run_id"]))
    assert run is not None and run.state == RunState.CANCELLED and run.completed_at is not None
    assert not harness.preparation.entered.is_set()
    assert [event.type for event in events] == [AppEventType.CANCELLED]
    assert not token._callbacks


@dataclass
class _CompletionRace:
    """在实际终态事务前后注入提交取消，检查流语义与重复写入。"""

    complete: Callable[[str], RunRecord]
    token: CancellationToken
    cancel_first: bool
    calls: int = 0

    def settle(self, run_id: str) -> RunRecord:
        """在确定的事务边界竞争取消。

        参数: run_id 为本次运行身份。
        返回值: 真正落库的终态。
        异常: 底层完成异常透传。
        """
        self.calls += 1
        if self.cancel_first:
            self.token.cancel()
        settled = self.complete(run_id)
        if not self.cancel_first:
            self.token.cancel()
        return settled


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_first", [True, False])
async def test_host_completion_race_has_one_terminal_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancel_first: bool) -> None:
    """真实 Host 的最后完成事务与取消竞争时只收敛一次。

    参数: tmp_path 为隔离目录，monkeypatch 为替换器，cancel_first 指定先到者。
    返回值: 无。
    异常: 断言失败。
    """
    harness = _build_harness(tmp_path)
    token = CancellationToken()
    runner = _Runner(threading.Event(), threading.Event(), finish=True)
    monkeypatch.setattr(agent_builder, "build_async_runner", runner.build)
    race = _CompletionRace(harness.registry.complete_run, token, cancel_first)
    monkeypatch.setattr(harness.registry, "complete_run", race.settle)
    submission = await harness.service.submit_turn(ChatTurnRequest(
        user_text="test", session_id=_SESSION_ID, cancellation_token=token,
    ))
    events = await _collect(submission.event_stream)
    run_id = str(events[0].payload["run_id"])
    run = harness.host.get_run(run_id)
    assert run is not None and run.completed_at is not None
    assert run.state == (RunState.CANCELLED if cancel_first else RunState.SUCCEEDED)
    assert len([event for event in events if event.type == AppEventType.CANCELLED]) == int(cancel_first)
    assert race.calls == 1
    assert runner.closed.is_set() and run_id not in harness.executor._run_resources
    assert not token._callbacks


@pytest.mark.asyncio
async def test_cancel_while_waiting_for_host_permit(tmp_path: Path) -> None:
    """同步 permit 等待仍能从另一个线程精确取消，且不进入准备。

    参数: tmp_path 为隔离目录。
    返回值: 无。
    异常: 断言失败。
    """
    harness = _build_harness(tmp_path, blocked=True)
    store = HostStore(tmp_path / "permits.db")
    store.initialize_schema()
    governor = SQLiteConcurrencyGovernor(store, {HOST_AGENT_LANE: 1})
    occupied = governor.acquire(HOST_AGENT_LANE)
    harness.executor.concurrency_governor = governor
    token = CancellationToken()
    submission = await harness.service.submit_turn(ChatTurnRequest(
        user_text="test", session_id=_SESSION_ID, cancellation_token=token,
    ))
    started = await anext(submission.event_stream)
    run_id = str(started.payload["run_id"])
    # 消费放在独立线程，测试线程模拟 UI 发取消，不依赖被 permit 阻塞的循环。
    worker = threading.Thread(target=_consume_in_thread, args=(submission.event_stream,), daemon=True)
    worker.start()
    try:
        deadline = time.monotonic() + _WAIT_SECONDS
        run = harness.host.get_run(run_id)
        while time.monotonic() < deadline:
            run = harness.host.get_run(run_id)
            if run is not None and run.state == RunState.RUNNING:
                break
            await asyncio.sleep(_POLL_SECONDS)
        assert run is not None and run.state == RunState.RUNNING
        assert not harness.preparation.entered.is_set()
        token.cancel()
        await asyncio.to_thread(worker.join, _WAIT_SECONDS)
        assert not worker.is_alive()
        run = harness.host.get_run(run_id)
        assert run is not None and run.state == RunState.CANCELLED and run.completed_at is not None
        assert not harness.preparation.entered.is_set()
        assert governor.get_lane_status(HOST_AGENT_LANE).active == 1
        assert run_id not in harness.executor._run_resources
    finally:
        token.cancel()
        governor.release(occupied)
        await asyncio.to_thread(worker.join, _WAIT_SECONDS)


def _consume_in_thread(stream: AsyncIterator[AppEvent]) -> None:
    """在线程的事件循环中耗尽已交接的流。

    参数: stream 为暂停在启动通知处的流。
    返回值: 无。
    异常: 流异常透传到测试线程告警。
    """
    asyncio.run(_collect(stream))


@dataclass
class _StreamlitState:
    """隔离 Streamlit UI 状态。"""

    session_state: dict[str, stream_runtime.ChatStreamFrameState]


@pytest.mark.parametrize("blocked", [False, True])
def test_ui_timeout_waits_for_host_and_worker_exit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, blocked: bool) -> None:
    """真实 UI worker 超时后确认收尾，且不会误伤同 session 的其他 run。

    参数: tmp_path 为隔离目录，monkeypatch 为替换器，blocked 指定准备阶段阻塞。
    返回值: 无。
    异常: 断言失败。
    """
    harness = _build_harness(tmp_path, blocked=blocked)
    runner = _Runner(threading.Event(), threading.Event())
    monkeypatch.setattr(agent_builder, "build_async_runner", runner.build)
    monkeypatch.setattr(stream_runtime, "st", _StreamlitState({}))
    sibling = harness.registry.register_run(session_id=_SESSION_ID, service_type="sibling")
    harness.registry.start_run(sibling.run_id)
    key = "cancel-contract-ui"
    stream_runtime.start_chat_stream_runtime(chat_service=harness.service, ticker=key, user_text="test",
                                             session_id=_SESSION_ID, trace_id=key, stream_state_key=key)
    handle = stream_runtime.CHAT_STREAM_RUNTIME_HANDLES[key]
    deadline = time.monotonic() + _WAIT_SECONDS
    try:
        entered = harness.preparation.entered if blocked else runner.entered
        assert entered.wait(timeout=_WAIT_SECONDS)
        while time.monotonic() < deadline:
            state = stream_runtime.poll_chat_stream_events(ticker=key, stream_state_key=key)
            assert state is not None
            if state.done:
                break
            time.sleep(_POLL_SECONDS)
        handle.worker.join(timeout=_WAIT_SECONDS)
        assert not handle.worker.is_alive()
        state = stream_runtime.poll_chat_stream_events(ticker=key, stream_state_key=key)
        assert state is not None and state.done and "本次运行已取消" in state.error_message
        runs = [run for run in harness.host.list_runs(session_id=_SESSION_ID) if run.run_id != sibling.run_id]
        assert len(runs) == 1
        assert runs[0].state == RunState.CANCELLED and runs[0].completed_at is not None
        assert runs[0].run_id not in harness.executor._run_resources
        untouched = harness.host.get_run(sibling.run_id)
        assert untouched is not None and untouched.state == RunState.RUNNING
        assert untouched.cancel_requested_at is None
        calls = runner.tool_calls
        time.sleep(_POLL_SECONDS * 2)
        assert runner.tool_calls == calls
        assert harness.preparation.exited.is_set()
        if not blocked:
            assert calls > 0 and runner.closed.is_set()
    finally:
        for run in harness.host.list_runs(session_id=_SESSION_ID):
            harness.host.cancel_run(run.run_id)
        handle.worker.join(timeout=_WAIT_SECONDS)
        stream_runtime.CHAT_STREAM_RUNTIME_HANDLES.pop(key, None)
