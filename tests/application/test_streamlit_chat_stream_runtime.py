"""聊天流式运行时测试。

覆盖 ``chat/stream_runtime.py`` 的线程桥接、超时、取消与错误分支。
"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass
from queue import Empty, Queue
from typing import AsyncIterator, Iterator, cast

from dayu.contracts.cancellation import CancellationToken

import pytest

from dayu.contracts.events import AppEvent, AppEventType
from dayu.host.protocols import ConversationSessionTurnExcerpt
from dayu.services.contracts import ChatPendingTurnView, ChatResumeRequest, ChatTurnRequest, ChatTurnSubmission
from dayu.services.protocols import ChatServiceProtocol
from dayu.web.streamlit.pages.chat import stream_runtime
from dayu.web.streamlit.pages.chat.stream_runtime import (
    ChatStreamEvent,
    CHAT_STREAM_RUNTIME_HANDLES,
    consume_chat_event_stream,
    new_stream_frame_state,
    run_stream_worker,
    ChatStreamRuntimeHandle,
    StreamQueueItem,
    StreamSubmissionInfo,
    poll_chat_stream_events,
)


@dataclass
class _FakeStreamlitState:
    """用于测试的精简 Streamlit 状态容器。"""

    session_state: dict[str, stream_runtime.ChatStreamFrameState]


class _StubWorker:
    """用于测试轮询逻辑的 worker 存根。"""

    def __init__(self, *, alive: bool) -> None:
        """初始化 worker 存根。

        参数:
            alive: 是否报告为存活状态。

        返回值:
            无。

        异常:
            无。
        """

        self._alive = alive

    def is_alive(self) -> bool:
        """返回 worker 是否仍在运行。

        参数:
            无。

        返回值:
            当前存活状态。

        异常:
            无。
        """

        return self._alive


class _StubChatService(ChatServiceProtocol):
    """用于测试 `_consume_chat_event_stream` 的聊天服务存根。"""

    def __init__(self, submission: ChatTurnSubmission) -> None:
        """初始化服务存根。

        参数:
            submission: 预置的提交结果。

        返回值:
            无。

        异常:
            无。
        """

        self._submission = submission
        self.requests: list[ChatTurnRequest] = []

    async def submit_turn(self, request: ChatTurnRequest) -> ChatTurnSubmission:
        """返回预置提交结果。"""

        self.requests.append(request)
        return self._submission

    async def resume_pending_turn(self, request: ChatResumeRequest) -> ChatTurnSubmission:
        """测试桩不支持恢复 pending turn。"""

        _ = request
        raise NotImplementedError

    def list_resumable_pending_turns(
        self,
        *,
        session_id: str | None = None,
        scene_name: str | None = None,
    ) -> list[ChatPendingTurnView]:
        """返回空 pending turn 列表。"""

        _ = (session_id, scene_name)
        return []

    def cleanup_stale_pending_turns(self, *, session_id: str | None = None) -> list[str]:
        """返回空清理结果。"""

        _ = session_id
        return []

    def list_conversation_session_turn_excerpts(
        self,
        session_id: str,
        *,
        limit: int,
    ) -> list[ConversationSessionTurnExcerpt]:
        """返回空会话摘录。"""

        _ = (session_id, limit)
        return []

    def clear_session_history(self, session_id: str) -> None:
        """测试桩不支持清空历史。"""

        _ = session_id
        raise NotImplementedError


async def _cancelled_stream_with_cleanup_gate(entered: asyncio.Event, release: asyncio.Event) -> AsyncIterator[AppEvent]:
    """模拟已发取消事件但尚未完成 Host finally 的流。

    参数: entered 标记进入收尾，release 控制收尾结束。
    返回值: 一条取消事件。
    异常: 外部任务取消透传。
    """
    yield AppEvent(type=AppEventType.CANCELLED, payload={"run_id": "test-run"})
    entered.set()
    await release.wait()


@pytest.mark.asyncio
async def test_cancel_confirmation_waits_until_stream_cleanup() -> None:
    """仅收到 CANCELLED 不得确认，必须等到流收尾完成。

    参数: 无。
    返回值: 无。
    异常: 断言失败。
    """
    entered, release = asyncio.Event(), asyncio.Event()
    queue: Queue[ChatStreamEvent] = Queue()
    service = _StubChatService(ChatTurnSubmission(
        session_id="session", effective_model_name="model", first_chunk_timeout_seconds=90.0,
        event_stream=_cancelled_stream_with_cleanup_gate(entered, release),
    ))
    task = asyncio.create_task(consume_chat_event_stream(
        chat_service=service, user_text="test", session_id="session", ticker="test",
        event_queue=queue, cancellation_token=CancellationToken(), trace_id="cleanup",
    ))
    try:
        await asyncio.wait_for(entered.wait(), timeout=2.0)
        assert not task.done()
        assert isinstance(queue.get_nowait(), StreamSubmissionInfo)
        assert queue.empty()
    finally:
        release.set()
        await task
    confirmation = queue.get_nowait()
    assert isinstance(confirmation, StreamQueueItem)
    assert confirmation.event_type == "cancelled" and confirmation.chunk == "本次运行已取消"


@pytest.fixture(autouse=True)
def _reset_stream_runtime(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """为每个用例隔离 stream runtime 全局状态。"""

    CHAT_STREAM_RUNTIME_HANDLES.clear()
    monkeypatch.setattr(stream_runtime, "st", _FakeStreamlitState(session_state={}))
    yield
    CHAT_STREAM_RUNTIME_HANDLES.clear()


@pytest.mark.unit
class TestStreamQueueItem:
    """StreamQueueItem dataclass 测试。"""

    def test_default_values(self) -> None:
        item = StreamQueueItem(done=False)
        assert item.done is False
        assert item.kind == "content"
        assert item.chunk == ""
        assert item.event_type == "chunk"
        assert item.flag is False

    def test_reasoning_kind(self) -> None:
        item = StreamQueueItem(done=False, kind="reasoning", chunk="think")
        assert item.kind == "reasoning"
        assert item.chunk == "think"

    def test_done_item(self) -> None:
        item = StreamQueueItem(done=True, event_type="done")
        assert item.done is True
        assert item.event_type == "done"

    def test_frozen(self) -> None:
        item = StreamQueueItem(done=False)
        with pytest.raises(Exception):
            item.done = True  # type: ignore[misc]


@pytest.mark.unit
class TestChatStreamRuntimePolling:
    """stream_runtime 核心轮询路径测试。"""

    def test_first_chunk_timeout_sets_cancel_and_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stream_state_key = "stream-state"
        ticker = "AAPL"
        stream_runtime.st.session_state[stream_state_key] = new_stream_frame_state(trace_id="t1", session_id="s1")
        cancellation_token = CancellationToken()
        queue: Queue[ChatStreamEvent] = Queue()
        runtime = ChatStreamRuntimeHandle(
            worker=cast(threading.Thread, _StubWorker(alive=True)),
            event_queue=queue,
            cancellation_token=cancellation_token,
            started_at=0.0,
            last_chunk_at=0.0,
            has_received_chunk=False,
            submission_info=StreamSubmissionInfo(
                session_id="s1", effective_model_name="actual-model", first_chunk_timeout_seconds=1.0,
            ),
        )
        CHAT_STREAM_RUNTIME_HANDLES[ticker] = runtime
        monkeypatch.setattr(stream_runtime.time, "perf_counter", lambda: 100.0)

        state = poll_chat_stream_events(ticker=ticker, stream_state_key=stream_state_key)

        assert state is not None
        assert state.done is False
        assert state.error_message == "等待模型首个输出超过 1 秒（模型：actual-model）"
        assert cancellation_token.is_cancelled() is True

    def test_chunk_interval_timeout_sets_cancel_and_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stream_state_key = "stream-state"
        ticker = "AAPL"
        stream_runtime.st.session_state[stream_state_key] = new_stream_frame_state(trace_id="t2", session_id="s2")
        cancellation_token = CancellationToken()
        queue: Queue[ChatStreamEvent] = Queue()
        runtime = ChatStreamRuntimeHandle(
            worker=cast(threading.Thread, _StubWorker(alive=True)),
            event_queue=queue,
            cancellation_token=cancellation_token,
            started_at=10.0,
            last_chunk_at=0.0,
            has_received_chunk=True,
        )
        CHAT_STREAM_RUNTIME_HANDLES[ticker] = runtime
        monkeypatch.setattr(stream_runtime.time, "perf_counter", lambda: 100.0)
        monkeypatch.setattr(stream_runtime, "_STREAM_CHUNK_TIMEOUT_SECONDS", 1.0)

        state = poll_chat_stream_events(ticker=ticker, stream_state_key=stream_state_key)

        assert state is not None
        assert state.done is False
        assert "超时" in state.error_message
        assert cancellation_token.is_cancelled() is True

    def test_error_event_marks_state_done(self) -> None:
        stream_state_key = "stream-state"
        ticker = "AAPL"
        stream_runtime.st.session_state[stream_state_key] = new_stream_frame_state(trace_id="t3", session_id="s3")
        queue: Queue[ChatStreamEvent] = Queue()
        queue.put(StreamQueueItem(done=False, event_type="error", chunk="boom"))
        runtime = ChatStreamRuntimeHandle(
            worker=cast(threading.Thread, _StubWorker(alive=True)),
            event_queue=queue,
            cancellation_token=CancellationToken(),
            started_at=0.0,
            last_chunk_at=0.0,
            has_received_chunk=False,
        )
        CHAT_STREAM_RUNTIME_HANDLES[ticker] = runtime

        state = poll_chat_stream_events(ticker=ticker, stream_state_key=stream_state_key)

        assert state is not None
        assert state.done is True
        assert state.error_message == "boom"

    def test_trailing_events_drained_after_worker_exit(self) -> None:
        stream_state_key = "stream-state"
        ticker = "AAPL"
        state = new_stream_frame_state(trace_id="t4", session_id="s4")
        state.done = True
        stream_runtime.st.session_state[stream_state_key] = state
        queue: Queue[ChatStreamEvent] = Queue()
        queue.put(StreamQueueItem(done=False, event_type="side_message", chunk="warn"))
        queue.put(StreamQueueItem(done=False, event_type="filtered", flag=True))
        queue.put(StreamQueueItem(done=False, event_type="error", chunk="late error"))
        runtime = ChatStreamRuntimeHandle(
            worker=cast(threading.Thread, _StubWorker(alive=False)),
            event_queue=queue,
            cancellation_token=CancellationToken(),
            started_at=0.0,
            last_chunk_at=0.0,
            has_received_chunk=True,
        )
        CHAT_STREAM_RUNTIME_HANDLES[ticker] = runtime

        polled = poll_chat_stream_events(ticker=ticker, stream_state_key=stream_state_key)

        assert polled is not None
        assert polled.side_messages == ["warn"]
        assert polled.filtered_flags == [True]
        assert polled.error_message == "late error"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_consume_chat_event_stream_stops_when_cancelled() -> None:
    """取消信号置位后应停止继续投递后续 chunk。"""

    event_queue: Queue[ChatStreamEvent] = Queue()
    cancellation_token = CancellationToken()

    async def _event_stream() -> AsyncIterator[AppEvent]:
        yield AppEvent(type=AppEventType.CONTENT_DELTA, payload="first")
        cancellation_token.cancel()
        yield AppEvent(type=AppEventType.CONTENT_DELTA, payload="second")

    submission = ChatTurnSubmission(effective_model_name="test-model", first_chunk_timeout_seconds=90.0, session_id="session-new", event_stream=_event_stream())
    chat_service = _StubChatService(submission=submission)

    await consume_chat_event_stream(
        chat_service=chat_service,
        user_text="hello",
        session_id=None,
        ticker="AAPL",
        event_queue=event_queue,
        cancellation_token=cancellation_token,
        trace_id="trace-test",
    )

    events: list[ChatStreamEvent] = []
    while True:
        try:
            events.append(event_queue.get_nowait())
        except Empty:
            break

    chunk_values = [event.chunk for event in events if isinstance(event, StreamQueueItem) and event.event_type == "chunk"]
    assert "first" in chunk_values
    assert "second" not in chunk_values


@pytest.mark.unit
def test_run_stream_worker_emits_error_and_done_on_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    """worker 协程异常时应投递 error 与 done 事件。"""

    event_queue: Queue[ChatStreamEvent] = Queue()
    cancellation_token = CancellationToken()

    async def _raise_in_consumer(
        *,
        chat_service: ChatServiceProtocol,
        user_text: str,
        session_id: str | None,
        ticker: str,
        event_queue: Queue[ChatStreamEvent],
        cancellation_token: CancellationToken,
        trace_id: str,
        model_name: str | None = None,
    ) -> None:
        _ = (chat_service, user_text, session_id, ticker, event_queue, cancellation_token, trace_id)
        raise RuntimeError("worker failed")

    monkeypatch.setattr(stream_runtime, "consume_chat_event_stream", _raise_in_consumer)

    run_stream_worker(
        chat_service=_StubChatService(
            submission=ChatTurnSubmission(effective_model_name="test-model", first_chunk_timeout_seconds=90.0, session_id="s", event_stream=_empty_event_stream())
        ),
        user_text="u",
        session_id=None,
        ticker="AAPL",
        event_queue=event_queue,
        cancellation_token=cancellation_token,
        trace_id="trace-err",
    )

    first = event_queue.get_nowait()
    second = event_queue.get_nowait()
    assert isinstance(first, StreamQueueItem)
    assert isinstance(second, StreamQueueItem)
    assert first.event_type == "error"
    assert "worker failed" in first.chunk
    assert second.event_type == "done"


async def _empty_event_stream() -> AsyncIterator[AppEvent]:
    """返回一个空事件流。"""

    if False:
        yield AppEvent(type=AppEventType.DONE, payload="")


async def _empty_model_selection_stream() -> AsyncIterator[AppEvent]:
    """提供无 HTTP 的终态事件。

    Args:
        无。
    Returns:
        空答案事件流。
    Raises:
        无。
    """
    yield AppEvent(type=AppEventType.FINAL_ANSWER, payload={"content": "ok"})


@pytest.mark.asyncio
@pytest.mark.parametrize("model_name", [None, "openai-responses"])
async def test_selected_model_reaches_service(model_name: str | None) -> None:
    """网页选择传入 Service；默认选择不覆盖持久化会话模型。

    Args:
        model_name: 页面选择。
    Returns:
        无。
    Raises:
        AssertionError: 选项丢失或默认被覆盖。
    """
    service = _StubChatService(ChatTurnSubmission(effective_model_name="test-model", first_chunk_timeout_seconds=90.0, session_id="s1", event_stream=_empty_model_selection_stream()))
    event_queue: Queue[ChatStreamEvent] = Queue()
    await consume_chat_event_stream(chat_service=service, user_text="x", session_id="s1", ticker="AAA", event_queue=event_queue, cancellation_token=CancellationToken(), trace_id="model-test", model_name=model_name)
    # Service 返回值先于任何模型输出跨线程传递，不能从可空 UI 选择推断。
    assert event_queue.get_nowait() == StreamSubmissionInfo(
        session_id="s1", effective_model_name="test-model", first_chunk_timeout_seconds=90.0,
    )
    options = service.requests[0].execution_options
    if model_name is None:
        assert options is None
    else:
        assert options is not None and options.model_name == model_name


@pytest.mark.parametrize("timeout_seconds", [90.0, 300.0])
@pytest.mark.parametrize("received_kind", [None, "content", "reasoning"])
def test_first_output_timeout_boundary_and_chunk_transition(
    monkeypatch: pytest.MonkeyPatch,
    timeout_seconds: float,
    received_kind: str | None,
) -> None:
    """生效阈值之前不超时，正文与推理首增量都切换到既有间隔计时。

    Args:
        monkeypatch: 固定轮询时间。
        timeout_seconds: Service 生效阈值。
        received_kind: 阈值前是否收到正文或推理增量。
    Returns:
        无。
    Raises:
        AssertionError: 阈值提前生效或首增量后的正常路径变化。
    """
    ticker = "boundary"
    state_key = "boundary-state"
    stream_runtime.st.session_state[state_key] = new_stream_frame_state(trace_id=ticker, session_id="old")
    event_queue: Queue[ChatStreamEvent] = Queue()
    event_queue.put(StreamSubmissionInfo(
        session_id="actual", effective_model_name="actual-model", first_chunk_timeout_seconds=timeout_seconds,
    ))
    runtime = ChatStreamRuntimeHandle(
        worker=cast(threading.Thread, _StubWorker(alive=True)), event_queue=event_queue,
        cancellation_token=CancellationToken(), started_at=0.0, last_chunk_at=0.0, has_received_chunk=False,
    )
    CHAT_STREAM_RUNTIME_HANDLES[ticker] = runtime
    monkeypatch.setattr(stream_runtime.time, "perf_counter", lambda: timeout_seconds - 1.0)
    if received_kind is not None:
        kind = "reasoning" if received_kind == "reasoning" else "content"
        event_queue.put(StreamQueueItem(done=False, kind=kind, chunk="first"))
    state = poll_chat_stream_events(ticker=ticker, stream_state_key=state_key)
    assert state is not None and not state.done and state.session_id == "actual"
    monkeypatch.setattr(stream_runtime.time, "perf_counter", lambda: timeout_seconds)
    state = poll_chat_stream_events(ticker=ticker, stream_state_key=state_key)
    assert state is not None
    if received_kind is None:
        assert not state.done
        assert runtime.cancellation_token.is_cancelled()
        assert state.error_message == f"等待模型首个输出超过 {timeout_seconds:g} 秒（模型：actual-model）"
    else:
        assert not state.done and not state.error_message
        assert state.answer_text == ("first" if received_kind == "content" else "")
        assert state.reasoning_text == ("first" if received_kind == "reasoning" else "")
