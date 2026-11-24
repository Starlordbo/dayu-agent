"""真实聊天 AppTest：历史、增量及终态引用接线；不替代真实浏览器验收。"""

from __future__ import annotations

import asyncio
import re
import threading
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from queue import Queue
from urllib.parse import parse_qs, urlsplit

import pytest
from streamlit.testing.v1 import AppTest
from streamlit.testing.v1.element_tree import Block

from dayu.contracts.events import AppEvent, AppEventType
from dayu.contracts.agent_types import AgentMessage, JsonValue
from dayu.contracts.protocols import ToolExecutor
from dayu.engine.async_agent import AgentRunningConfig, AsyncAgent
from dayu.engine.events import (
    EventType, StreamEvent, content_complete, content_delta, done_event,
    tool_call_dispatched, tool_call_result, tool_calls_batch_done,
)
from dayu.engine.processors.processor_registry import ProcessorRegistry
from dayu.engine.tool_registry import ToolRegistry
from dayu.fins.domain.document_models import CompanyMeta, SourceDocumentUpsertRequest
from dayu.fins.domain.enums import SourceKind
from dayu.fins.tools.fins_tools import register_fins_read_tools
from dayu.fins.tools.service import FinsToolService
from dayu.host.events import build_app_event_from_stream_event
from dayu.host.protocols import (
    ConversationClearPartiallyAppliedError,
    ConversationClearRejectedError,
    ConversationClearStaleError,
    ConversationSessionTurnExcerpt,
)
from dayu.services.contracts import ChatPendingTurnView, ChatResumeRequest, ChatTurnRequest, ChatTurnSubmission
from dayu.services.protocols import ChatServiceProtocol
from dayu.web.streamlit.pages.chat.stream_runtime import (
    CHAT_STREAM_RUNTIME_HANDLES,
    ChatStreamRuntimeHandle,
    StreamQueueItem,
    new_stream_frame_state,
)
from dayu.web.streamlit.pages.chat.utils import build_chat_session_id, fold_app_events_to_assistant_text
from dayu.web.streamlit.pages.chat_tab import ChatMessage
from dayu.prompting.scene_definition import load_scene_definition
from dayu.startup.config_file_resolver import ConfigFileResolver
from dayu.startup.prompt_assets import FilePromptAssetStore
from tests.fins.storage_testkit import build_fs_storage_test_context

_TICKER = "AAPL"
_SESSION = "session-keep"
_STATE = "chat_tab_AAPL_stream_state"
_MESSAGES = "chat_tab_AAPL_messages"
_INPUT = "chat_tab_AAPL_input_text"
_MARKER = "[[source:AAPL|mat_call|qa]]"
_USER = "这份材料说明什么？ " + _MARKER
_ANSWER = "管理层尚未提供预测。 " + _MARKER
_REASONING = "先核对原文身份，再区分问题与公司答复。"
_APP = '''
import streamlit as st
from dayu.web.streamlit.components.watchlist import WatchlistItem
from dayu.web.streamlit.pages.chat_tab import render_chat_tab
render_chat_tab(selected_stock=WatchlistItem("AAPL", "Apple", "", ""),
                chat_service=st.session_state["test_service"],
                model_names=st.session_state["test_models"])
'''


def _turn(user: str = _USER, answer: str = _ANSWER, reasoning: str = _REASONING) -> ConversationSessionTurnExcerpt:
    """构造原始历史真源摘录，不预先替换来源标记。

    参数:
        user: 用户原始问题。
        answer: 助手原始正文。
        reasoning: 助手思考内容。
    返回值:
        类型完整的历史摘录。
    异常:
        无。
    """
    return ConversationSessionTurnExcerpt(user_text=user, assistant_text=answer,
                                          reasoning_text=reasoning, created_at="2026-01-01T00:00:00Z")


async def _offline_events() -> AsyncIterator[AppEvent]:
    """离线产生实际 worker 能消费的事件，不调用网络或模型。

    参数:
        无。
    返回值:
        思考、正文和终态事件序列。
    异常:
        无。
    """
    yield AppEvent(AppEventType.REASONING_DELTA, _REASONING)
    yield AppEvent(AppEventType.CONTENT_DELTA, _ANSWER)
    yield AppEvent(AppEventType.FINAL_ANSWER, _ANSWER)


@dataclass
class _ChatService:
    """完整满足聊天协议的离线服务，保留原始历史并记录请求。"""

    histories: dict[str, list[ConversationSessionTurnExcerpt]] = field(default_factory=dict)
    reads: list[tuple[str, int]] = field(default_factory=list)
    submissions: list[ChatTurnRequest] = field(default_factory=list)
    clears: list[str] = field(default_factory=list)
    clear_errors: list[Exception] = field(default_factory=list)
    history_error: bool = False

    async def submit_turn(self, request: ChatTurnRequest) -> ChatTurnSubmission:
        """记录真实 worker 提交并返回离线事件流。

        参数:
            request: 页面传来的完整聊天请求。
        返回值:
            已持久化历史对应的离线提交句柄。
        异常:
            无。
        """
        self.submissions.append(request)
        session_id = request.session_id or _SESSION
        self.histories.setdefault(session_id, []).append(_turn(user=request.user_text))
        return ChatTurnSubmission(session_id=session_id, event_stream=_offline_events())

    async def resume_pending_turn(self, request: ChatResumeRequest) -> ChatTurnSubmission:
        """阻止聊天页意外调用恢复接口。

        参数:
            request: 恢复请求。
        返回值:
            不返回。
        异常:
            AssertionError: 本页面不应恢复任务。
        """
        raise AssertionError(f"不应恢复任务：{request}")

    def list_resumable_pending_turns(
        self, *, session_id: str | None = None, scene_name: str | None = None,
    ) -> list[ChatPendingTurnView]:
        """提供无待恢复任务的明确状态。

        参数:
            session_id: 可选会话过滤。
            scene_name: 可选场景过滤。
        返回值:
            空任务列表。
        异常:
            无。
        """
        return []

    def cleanup_stale_pending_turns(self, *, session_id: str | None = None) -> list[str]:
        """提供无过期待清理任务的明确状态。

        参数:
            session_id: 可选会话过滤。
        返回值:
            空任务标识列表。
        异常:
            无。
        """
        return []

    def list_conversation_session_turn_excerpts(
        self, session_id: str, *, limit: int,
    ) -> list[ConversationSessionTurnExcerpt]:
        """读取会话真源摘录，允许注入可恢复的读失败。

        参数:
            session_id: 当前会话身份。
            limit: 最近轮次数量上限。
        返回值:
            原始摘录列表副本。
        异常:
            OSError: 设置读失败时抛出。
        """
        self.reads.append((session_id, limit))
        if self.history_error:
            raise OSError("测试历史读取失败")
        return list(self.histories.get(session_id, [])[-limit:])

    def clear_session_history(self, session_id: str) -> None:
        """清空指定会话真源，或依次抛出预设清理错误。

        参数:
            session_id: 当前会话身份。
        返回值:
            无。
        异常:
            Exception: 预设的清理异常。
        """
        self.clears.append(session_id)
        if self.clear_errors:
            raise self.clear_errors.pop(0)
        self.histories[session_id] = []


def _app(service: _ChatService | None, models: tuple[str, ...] = ("offline-model",)) -> AppTest:
    """构造真实聊天页面并设置可保留的会话查询参数。

    参数:
        service: 完整类型的离线服务，或服务不可用。
        models: 页面可选择的模型名。
    返回值:
        尚未执行的 AppTest。
    异常:
        无。
    """
    app = AppTest.from_string(_APP)
    app.session_state["test_service"] = service
    app.session_state["test_models"] = models
    app.query_params["chat_tab_AAPL_session"] = _SESSION
    app.query_params["view"] = "analysis"
    return app


def test_return_session_survives_empty_model_catalog() -> None:
    """可选模型为空也必须沿用 URL 会话，不能静默回到公司默认历史。

    Args:
        无。
    Returns:
        无。
    Raises:
        AssertionError: 返回后读取了错误会话或启动了模型请求时抛出。
    """
    service = _ChatService(histories={_SESSION: [_turn()]})
    app = _app(service, models=()).run()
    assert not app.exception
    assert service.reads and all(session == _SESSION for session, _ in service.reads)
    _assert_assistant_citation(app)
    assert service.submissions == []


def _assert_assistant_citation(app: AppTest) -> None:
    """断言页面实际输出可解析的来源链接，且用户原文保持不变。

    参数:
        app: 已执行的真实页面。
    返回值:
        无。
    异常:
        AssertionError: 链接身份、会话参数或用户原文发生错误。
    """
    markdowns = [item.value for item in app.markdown]
    rendered = [text for text in markdowns if "管理层尚未提供预测。" in text]
    # fragment 触发整页 rerun 的同轮快照可能同时含终态帧和新历史；两者必须完全一致。
    assert rendered and len(set(rendered)) == 1 and all(_MARKER not in text for text in rendered)
    match = re.search(r"\[章节来源\]\(([^)]+)\)", rendered[0])
    assert match is not None
    assert parse_qs(urlsplit(match.group(1)).query) == {
        "chat_tab_AAPL_session": [_SESSION], "view": ["analysis"], "ticker": [_TICKER],
        "source_ticker": [_TICKER], "source_document": ["mat_call"], "source_ref": ["qa"],
    }
    assert _USER in markdowns


def _finished_worker() -> None:
    """建立已结束线程，使测试能逐批推送真实运行时队列而不后台等待。

    参数:
        无。
    返回值:
        无。
    异常:
        无。
    """


def _install_queue(app: AppTest, monkeypatch: pytest.MonkeyPatch) -> Queue[StreamQueueItem]:
    """注入生产队列及完整运行时句柄，不替代轮询、fragment、清理或渲染。

    参数:
        app: 尚未运行的页面。
        monkeypatch: 自动恢复全局运行时注册表。
    返回值:
        测试可逐批投递事件的真实线程队列。
    异常:
        AssertionError: 空线程未及时结束。
    """
    worker = threading.Thread(target=_finished_worker)
    worker.start()
    worker.join(timeout=5)
    assert not worker.is_alive()
    queue: Queue[StreamQueueItem] = Queue()
    now = time.perf_counter()
    monkeypatch.setitem(CHAT_STREAM_RUNTIME_HANDLES, _TICKER, ChatStreamRuntimeHandle(
        worker=worker, event_queue=queue, cancel_event=threading.Event(), started_at=now,
        last_chunk_at=now, has_received_chunk=False,
    ))
    app.session_state[_STATE] = new_stream_frame_state(trace_id="render-test", session_id=_SESSION)
    app.session_state[_MESSAGES] = [ChatMessage("user", _USER)]
    app.session_state[_INPUT] = "提交后应清空的问题"
    return queue


@pytest.mark.parametrize("reasoning", ["", _REASONING])
def test_history_renders_user_verbatim_and_assistant_citation(reasoning: str) -> None:
    """历史服务真源经真实 UI 渲染；只转换助手正文，不转换用户文本。

    参数:
        reasoning: 有无思考内容两个合法历史分支。
    返回值:
        无。
    异常:
        AssertionError: 历史身份、思考面板或引用链接错误。
    """
    service = _ChatService(histories={_SESSION: [_turn(reasoning=reasoning)]})
    assert isinstance(service, ChatServiceProtocol)
    app = _app(service).run()
    assert not app.exception and not app.error
    assert service.reads == [(_SESSION, 50)]
    assert len(app.chat_message) == 2
    assert len(app.expander) == int(bool(reasoning))
    _assert_assistant_citation(app)
    assert service.histories[_SESSION][0].assistant_text == _ANSWER
    assert service.submissions == []


@pytest.mark.parametrize("history_error", [False, True])
def test_empty_or_unreadable_history_shows_welcome_and_repairs_cache(history_error: bool) -> None:
    """空历史或读取失败保留正常页面，并修复不合法本地消息缓存。

    参数:
        history_error: 是否由 Service 抛出读错误。
    返回值:
        无。
    异常:
        AssertionError: 读失败使页面崩溃或损坏缓存未修复。
    """
    service = _ChatService(history_error=history_error)
    app = _app(service, models=())
    del app.query_params["chat_tab_AAPL_session"]
    app.session_state[_MESSAGES] = "损坏的旧缓存"
    app.run()
    assert not app.exception and not app.error
    assert not app.chat_message and app.session_state[_MESSAGES] == []
    assert any("基于当前股票" in item.value for item in app.markdown)
    assert service.reads == [(build_chat_session_id(_TICKER), 50)]


@pytest.mark.parametrize("text", ["   ", "分析这个问题"])
def test_submit_validation_and_missing_service_clear(text: str) -> None:
    """真实按钮验证空输入、缺少服务和缺少服务时清空三个安全分支。

    参数:
        text: 空白或有效问题。
    返回值:
        无。
    异常:
        AssertionError: 按钮未显示对应提示。
    """
    app = _app(None, models=()).run()
    app.text_area[0].set_value(text)
    app.button(key="chat_tab_AAPL_send_button").click().run()
    assert not app.exception
    expected = "请输入问题" if not text.strip() else "交互式分析服务未就绪"
    assert any(expected in item.value for item in app.warning)
    app.button(key="chat_tab_AAPL_clear_button").click().run()
    assert any("聊天服务未就绪" in item.value for item in app.warning)


def test_model_switch_requires_new_session_and_preserves_other_query() -> None:
    """真实模型选择与新建会话更新身份，不清空旧后端历史或丢失其他查询。

    参数:
        无。
    返回值:
        无。
    异常:
        AssertionError: 新会话未隔离或旧历史被误清空。
    """
    service = _ChatService(histories={_SESSION: [_turn()]})
    app = _app(service).run()
    app.selectbox[0].set_value("offline-model").run()
    app.button(key="chat_tab_AAPL_new_session").click().run()
    assert not app.exception and not app.error
    new_session = app.query_params["chat_tab_AAPL_session"][0]
    assert new_session != _SESSION and len(new_session) == 32
    assert service.reads[-1] == (new_session, 50)
    assert service.histories[_SESSION] == [_turn()]
    assert not app.chat_message and service.clears == []
    assert app.query_params["view"] == ["analysis"]


def test_live_partial_marker_then_completed_frame_and_final_history(monkeypatch: pytest.MonkeyPatch) -> None:
    """真实轮询 fragment 拼接分片引用，完成后落本地消息再由历史真源重绘。

    参数:
        monkeypatch: 隔离生产运行时注册表。
    返回值:
        无。
    异常:
        AssertionError: 增量、终态或历史任一引用接点失效。
    """
    service = _ChatService()
    app = _app(service)
    queue = _install_queue(app, monkeypatch)
    queue.put(StreamQueueItem(done=False, chunk="管理层尚未提供预测。 [[source:AAPL|mat_"))
    app.run()
    assert not app.exception and not app.error
    assert any("[[source:AAPL|mat_" in item.value for item in app.markdown)
    assert not any("[章节来源](" in item.value for item in app.markdown)
    assert any("正在思考" in item.value for item in app.markdown)
    assert app.button(key="chat_tab_AAPL_send_button").disabled
    assert app.button(key="chat_tab_AAPL_new_session").disabled
    app.button(key="chat_tab_AAPL_clear_button").click().run()
    assert any("仍在生成中" in item.value for item in app.warning)
    assert service.clears == []
    queue.put(StreamQueueItem(done=False, chunk="call|qa]]"))
    queue.put(StreamQueueItem(done=False, kind="reasoning", chunk=_REASONING))
    app.run()
    assert not app.exception and not app.error
    _assert_assistant_citation(app)
    assert app.expander[0].label == "思考内容"
    assert service.reads == []
    service.histories[_SESSION] = [_turn()]
    queue.put(StreamQueueItem(done=True, event_type="done"))
    app.run()
    assert not app.exception and not app.error
    _assert_assistant_citation(app)
    assert app.session_state[_MESSAGES] == [ChatMessage("user", _USER), ChatMessage("assistant", _ANSWER, _REASONING)]
    assert app.text_area[0].value == ""
    assert not app.button(key="chat_tab_AAPL_send_button").disabled
    assert _STATE not in app.session_state and _TICKER not in CHAT_STREAM_RUNTIME_HANDLES
    assert service.reads == [(_SESSION, 50)] and service.submissions == []
    # 独立整页重绘后只允许一轮历史，排除将终态重复追加到消息真源。
    app.run()
    assert not app.exception and len(app.chat_message) == 2
    _assert_assistant_citation(app)


@pytest.mark.parametrize("kind", ["error", "side-message", "empty", "escaped-empty", "401", "400", "partial-error"])
def test_stream_terminal_error_and_empty_answer_paths(monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    """失败或空回复完成后重绘提示、恢复按钮并保留输入供重试。

    参数:
        monkeypatch: 隔离生产运行时注册表。
        kind: 执行异常、副作用、空回复、不可重试 HTTP 错误或部分回答失败。
    返回值:
        无。
    异常:
        AssertionError: 错误提示丢失、终态未清理或错误追加回复。
    """
    service = _ChatService()
    app = _app(service)
    queue = _install_queue(app, monkeypatch)
    app.run()
    assert app.button(key="chat_tab_AAPL_send_button").disabled
    assert "正在分析中" in app.button(key="chat_tab_AAPL_send_button").label
    if kind in ("error", "partial-error"):
        if kind == "partial-error":
            queue.put(StreamQueueItem(done=False, chunk=_ANSWER))
        queue.put(StreamQueueItem(done=True, event_type="error", chunk="离线测试执行失败"))
    elif kind == "side-message":
        queue.put(StreamQueueItem(done=False, event_type="side_message", chunk="材料不足，请补充原文"))
        queue.put(StreamQueueItem(done=False, event_type="filtered", flag=True))
    elif kind in ("401", "400"):
        queue.put(StreamQueueItem(done=False, event_type="side_message", chunk=f"HTTP {kind}：模型请求失败"))
    elif kind == "escaped-empty":
        queue.put(StreamQueueItem(done=False, chunk=r"\n"))
        queue.put(StreamQueueItem(done=False, kind="reasoning", chunk=_REASONING))
    queue.put(StreamQueueItem(done=True, event_type="done"))
    app.run()
    assert not app.exception
    assert _STATE not in app.session_state and _TICKER not in CHAT_STREAM_RUNTIME_HANDLES
    assert service.reads == [(_SESSION, 50)]
    # 再次整页重绘仍必须保留提示，不能只依赖终态 fragment 遗留的画面。
    for _ in range(2):
        assert not app.button(key="chat_tab_AAPL_send_button").disabled
        assert "正在分析中" not in app.button(key="chat_tab_AAPL_send_button").label
        assert not app.button(key="chat_tab_AAPL_new_session").disabled
        assert app.text_area[0].value == "提交后应清空的问题"
        if kind in ("error", "partial-error"):
            assert len(app.error) == 1 and "离线测试执行失败" in app.error[0].value
            if kind == "partial-error":
                assert any("管理层尚未提供预测" in item.value for item in app.markdown)
        else:
            assert not app.error
            assert any("本轮未收到" in item.value for item in app.warning)
            if kind == "side-message":
                assert any("材料不足" in item.value for item in app.warning)
                assert any("内容过滤" in item.value for item in app.info)
            elif kind in ("401", "400"):
                assert any(f"HTTP {kind}" in item.value for item in app.warning)
            elif kind == "escaped-empty":
                assert any(_REASONING in item.value for item in app.markdown)
        app.run()


@pytest.mark.parametrize("action", ["retry", "clear", "new-session", "switch-session"])
def test_terminal_feedback_does_not_leak_into_next_turn_or_session(
    monkeypatch: pytest.MonkeyPatch, action: str,
) -> None:
    """真实按钮及会话切换不会将旧终态反馈带入后续轮次或其他会话。

    参数:
        monkeypatch: 隔离运行时注册表。
        action: 重试、清空、新建或 URL 恢复其他会话。
    返回值:
        无。
    异常:
        AssertionError: 提示未清理、重试输入丢失或反馈串到其他会话。
    """
    service = _ChatService()
    app = _app(service)
    queue = _install_queue(app, monkeypatch)
    queue.put(StreamQueueItem(done=True, event_type="error", chunk="需要修复配置"))
    app.run()
    app.run()
    assert any("需要修复配置" in item.value for item in app.error)
    if action == "retry":
        app.button(key="chat_tab_AAPL_send_button").click().run()
        runtime = CHAT_STREAM_RUNTIME_HANDLES.get(_TICKER)
        if runtime is not None:
            runtime.worker.join(timeout=5)
            assert not runtime.worker.is_alive()
            app.run()
        assert len(service.submissions) == 1
        assert service.submissions[0].user_text == "提交后应清空的问题"
    elif action == "clear":
        app.button(key="chat_tab_AAPL_clear_button").click().run()
        assert service.clears == [_SESSION]
    elif action == "new-session":
        app.button(key="chat_tab_AAPL_new_session").click().run()
    else:
        app.query_params["chat_tab_AAPL_session"] = "another-session"
        app.run()
    assert not app.exception and not app.error and not app.warning and not app.info


def test_successful_answer_preserves_side_messages_after_rerun(monkeypatch: pytest.MonkeyPatch) -> None:
    """有正文的成功回复也需在历史重绘后保留警告与过滤提示。

    参数:
        monkeypatch: 隔离运行时注册表。
    返回值:
        无。
    异常:
        AssertionError: 正文重复、提示丢失或成功后输入未清空。
    """
    service = _ChatService(histories={_SESSION: [_turn()]})
    app = _app(service)
    queue = _install_queue(app, monkeypatch)
    queue.put(StreamQueueItem(done=False, chunk=_ANSWER))
    queue.put(StreamQueueItem(done=False, event_type="side_message", chunk="部分材料不可用"))
    queue.put(StreamQueueItem(done=False, event_type="filtered", flag=True))
    queue.put(StreamQueueItem(done=True, event_type="done"))
    app.run()
    app.run()
    assert not app.exception and not app.error
    assert len(app.chat_message) == 2
    assert any("部分材料不可用" in item.value for item in app.warning)
    assert any("内容过滤" in item.value for item in app.info)
    assert app.text_area[0].value == ""


_COMPARISON_TICKERS = ("GOOGL", "AMZN")
_COMPARISON_DOCUMENT = "mat_google_cloud"
_COMPARISON_QUESTION = "比较 GOOGL 与 AMZN 的云业务"
_COMPARISON_ANSWER = "仅能据现有证据比较；AMZN 云业务数据尚未覆盖。"
_COMPARISON_BASE_YEAR = 2026
_COMPARISON_MAX_CALLS = 40


class _ComparisonBudgetRunner:
    """离线双公司调用脚本，执行真实工具并让真实 Agent 终止迭代。"""

    def __init__(self) -> None:
        """初始化调用记录。

        参数:
            无。
        返回值:
            无。
        异常:
            无。
        """
        self.executor: ToolExecutor | None = None
        self.model_calls = 0
        self.tool_results: list[StreamEvent] = []

    def set_tools(self, executor: ToolExecutor | None) -> None:
        """接收 Agent 管理的工具开关。

        参数:
            executor: 工具执行器，降级时为空。
        返回值:
            无。
        异常:
            无。
        """
        self.executor = executor

    def is_supports_tool_calling(self) -> bool:
        """声明脚本使用工具事件协议。

        参数:
            无。
        返回值:
            True。
        异常:
            无。
        """
        return True

    async def close(self) -> None:
        """释放无外部资源的离线 Runner。

        参数:
            无。
        返回值:
            无。
        异常:
            无。
        """

    async def call(
        self, messages: list[AgentMessage], *, stream: bool = True, **extra_payloads: JsonValue,
    ) -> AsyncIterator[StreamEvent]:
        """每轮调用两个工具，故意混用一次 ID，直到 Agent 关闭工具。

        参数:
            messages: Agent 上下文。
            stream: 流式输出标记。
            extra_payloads: Runner 协议附带的调用上下文。
        返回值:
            真实工具结果事件；降级后只返回答案。
        异常:
            AssertionError: Agent 未在测试预算内停止工具调用。
        """
        del messages, stream, extra_payloads
        self.model_calls += 1
        if self.executor is None:
            yield content_delta(_COMPARISON_ANSWER)
            yield done_event()
            return
        assert len(self.tool_results) < _COMPARISON_MAX_CALLS
        yield content_complete("")
        call_ids: list[str] = []
        errors = 0
        for index, ticker in enumerate(_COMPARISON_TICKERS):
            name = "list_documents"
            arguments: dict[str, JsonValue] = {"ticker": ticker}
            if self.model_calls > 1:
                # 按期变化的查询避免重复调用防线抢先触发，单独锁定轮次预算。
                arguments["fiscal_years"] = [_COMPARISON_BASE_YEAR - self.model_calls]
            if self.model_calls == 2 and ticker == _COMPARISON_TICKERS[1]:
                name = "search_document"
                arguments = {"ticker": ticker, "document_id": _COMPARISON_DOCUMENT, "query": "cloud"}
            call_id = f"comparison_{self.model_calls}_{index}"
            call_ids.append(call_id)
            yield tool_call_dispatched(call_id, name, arguments, index_in_iteration=index)
            result = self.executor.execute(name, arguments)
            errors += int(result["ok"] is False)
            event = tool_call_result(call_id, result, name=name, arguments=arguments, index_in_iteration=index)
            self.tool_results.append(event)
            yield event
        yield tool_calls_batch_done(call_ids, ok=len(call_ids) - errors, error=errors, timeout=0, cancelled=0)
        yield done_event()


async def _run_comparison_budget(registry: ToolRegistry, runner: _ComparisonBudgetRunner) -> list[StreamEvent]:
    """使用包内交互预算驱动生产 Agent，不调用在线模型。

    参数:
        registry: 真实财报工具注册表。
        runner: 离线比较脚本。
    返回值:
        完整 Agent 事件。
    异常:
        AssertionError: 包内交互预算未声明。
    """
    definition = load_scene_definition(FilePromptAssetStore(ConfigFileResolver()), "interactive")
    max_iterations = definition.runtime.agent.max_iterations
    assert max_iterations is not None
    agent = AsyncAgent(runner, tool_executor=registry, running_config=AgentRunningConfig(
        max_iterations=max_iterations, max_compactions=0, fallback_mode="force_answer",
    ))
    return [event async for event in agent.run(_COMPARISON_QUESTION)]


def test_cross_company_budget_warning_survives_terminal_redraw(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """混用 ID 必须显式报错，双调用批次须有界，实际降级告警须留在终态对话区。

    参数:
        tmp_path: 隔离财报仓储。
        monkeypatch: 隔离聊天运行时队列。
    返回值:
        无。
    异常:
        AssertionError: 归属错误静默、预算失守或终态重绘丢失告警。
    """
    storage = build_fs_storage_test_context(tmp_path)
    for ticker in _COMPARISON_TICKERS:
        storage.company_repository.upsert_company_meta(CompanyMeta(
            company_id=ticker, company_name=ticker, ticker=ticker, market="US",
            resolver_version="test", updated_at="2026-11-24T00:00:00Z",
        ))
    storage.source_repository.create_source_document(SourceDocumentUpsertRequest(
        ticker=_COMPARISON_TICKERS[0], document_id=_COMPARISON_DOCUMENT,
        internal_document_id=_COMPARISON_DOCUMENT,
    ), SourceKind.MATERIAL)
    registry = ToolRegistry()
    register_fins_read_tools(registry, service=FinsToolService(
        company_repository=storage.company_repository, source_repository=storage.source_repository,
        processed_repository=storage.processed_repository, processor_registry=ProcessorRegistry(),
    ))
    runner = _ComparisonBudgetRunner()
    events = asyncio.run(_run_comparison_budget(registry, runner))
    errors = [event.data["result"] for event in runner.tool_results if event.data["result"]["ok"] is False]
    assert len(errors) == 1
    assert errors[0]["error"] == "invalid_argument"
    assert "GOOGL" in errors[0]["message"] and "AMZN" in errors[0]["message"]
    assert "list_documents" in errors[0]["hint"]
    assert len(runner.tool_results) == _COMPARISON_MAX_CALLS
    assert runner.model_calls == _COMPARISON_MAX_CALLS // len(_COMPARISON_TICKERS) + 1
    assert events[-1].type == EventType.FINAL_ANSWER and events[-1].data["degraded"] is True
    app_events = [app_event for event in events
                  if (app_event := build_app_event_from_stream_event(event)) is not None]
    answer, side_messages, filtered = fold_app_events_to_assistant_text(app_events)
    budget_warnings = [message for message in side_messages if "最大工具调用" in message]
    assert len(budget_warnings) == 1 and "现有上下文" in budget_warnings[0]
    assert answer == _COMPARISON_ANSWER and filtered is False
    service = _ChatService(histories={_SESSION: [_turn(user=_COMPARISON_QUESTION, answer=answer)]})
    app = _app(service)
    queue = _install_queue(app, monkeypatch)
    queue.put(StreamQueueItem(done=False, chunk=answer))
    for message in side_messages:
        queue.put(StreamQueueItem(done=False, event_type="side_message", chunk=message))
    queue.put(StreamQueueItem(done=True, event_type="done"))
    app.run()
    app.run()
    assert not app.exception and not app.error
    assert len(app.chat_message) == 2
    # 生产布局把终态反馈放在历史对话容器内、消息气泡之后。
    history_blocks = [node for node in app.main.children.values()
                      if isinstance(node, Block) and len(node.chat_message) == 2]
    assert len(history_blocks) == 1
    # Streamlit 将前导警告 emoji 拆为 icon，重组后严格比较完整消息。
    assert [f"{item.proto.icon} {item.value}" for item in history_blocks[0].warning] == budget_warnings


def test_submit_uses_real_worker_and_service_events_without_model() -> None:
    """真实提交按钮驱动生产 worker 消费离线 Service 事件，终态历史仍可追溯。

    参数:
        无。
    返回值:
        无。
    异常:
        AssertionError: 请求参数、worker 完成或终态引用不符合预期。
    """
    service = _ChatService()
    app = _app(service).run()
    app.selectbox[0].set_value("offline-model").run()
    app.text_area[0].set_value("  " + _USER + "  ")
    app.button(key="chat_tab_AAPL_send_button").click().run()
    runtime = CHAT_STREAM_RUNTIME_HANDLES.get(_TICKER)
    if runtime is not None:
        runtime.worker.join(timeout=5)
        assert not runtime.worker.is_alive()
        app.run()
    assert not app.exception and not app.error
    assert len(service.submissions) == 1
    request = service.submissions[0]
    assert request.user_text == _USER and request.ticker == _TICKER and request.session_id == _SESSION
    assert request.execution_options is not None and request.execution_options.model_name == "offline-model"
    _assert_assistant_citation(app)
    assert app.text_area[0].value == "" and _TICKER not in CHAT_STREAM_RUNTIME_HANDLES
    app.run()
    assert not app.exception and len(app.chat_message) == 2 and len(service.submissions) == 1
    _assert_assistant_citation(app)


@pytest.mark.parametrize("errors, expected", [
    ([], ""),
    ([KeyError("missing")], "会话不存在"),
    ([ConversationClearRejectedError(_SESSION, reason="存在运行中任务")], "存在运行中任务"),
    ([ConversationClearPartiallyAppliedError(_SESSION, residual_sources=("pending_turn_store",))], "部分残留待修复"),
    ([ConversationClearStaleError(_SESSION, expected_revision="r1", actual_revision="r2")], ""),
    ([ConversationClearStaleError(_SESSION, expected_revision="r1", actual_revision="r2"), RuntimeError("retry failed")], "请稍后重试"),
])
def test_clear_button_calls_service_and_preserves_failure_visibility(errors: list[Exception], expected: str) -> None:
    """真实清空按钮验证成功重绘、拒绝、缺失、补偿失败及乐观锁重试的可见反馈。

    参数:
        errors: Service 顺序抛出的错误。
        expected: 失败反馈；空字符串表示应成功清空。
    返回值:
        无。
    异常:
        AssertionError: 清理次数、错误可见性或历史清理不符合预期。
    """
    service = _ChatService(histories={_SESSION: [_turn()]}, clear_errors=list(errors))
    app = _app(service).run()
    app.text_area[0].set_value("尚未提交的问题")
    app.button(key="chat_tab_AAPL_clear_button").click().run()
    assert not app.exception
    assert service.clears == [_SESSION] * (2 if errors and isinstance(errors[0], ConversationClearStaleError) else 1)
    if expected:
        notices = [item.value for item in app.warning] + [item.value for item in app.error]
        assert any(expected in text for text in notices)
        _assert_assistant_citation(app)
    else:
        assert not app.warning and not app.error and not app.chat_message
        assert service.histories[_SESSION] == [] and app.text_area[0].value == ""
