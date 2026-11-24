"""用真实 fragment 请求及 AppTest 验证终态恢复片段外按钮并保留失败反馈。"""

from __future__ import annotations

import threading
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from queue import Queue
from typing import cast

import pytest
import streamlit.testing.v1.app_test as app_test_module
from streamlit.runtime.scriptrunner import ScriptRunnerEvent
from streamlit.runtime.scriptrunner_utils.script_requests import RerunData
from streamlit.runtime.scriptrunner_utils.script_run_context import ScriptRunContext
from streamlit.testing.v1 import AppTest
from streamlit.testing.v1.element_tree import parse_tree_from_messages
from streamlit.testing.v1.local_script_runner import LocalScriptRunner

from dayu.contracts.events import AppEvent, AppEventType
from dayu.services.contracts import ChatTurnRequest, ChatTurnSubmission
from dayu.web.streamlit.pages.chat.stream_runtime import (
    ChatStreamEvent,
    CHAT_STREAM_RUNTIME_HANDLES,
    StreamQueueItem,
    run_stream_worker,
)
from tests.application.test_streamlit_chat_render import (
    _INPUT,
    _SESSION,
    _STATE,
    _TICKER,
    _USER,
    _ChatService,
    _app,
    _install_queue,
)

_PLAN_KEY = "test_fragment_rerun_plan"
_SEND_BUTTON = "chat_tab_AAPL_send_button"
_NEW_SESSION_BUTTON = "chat_tab_AAPL_new_session"
_TRACE_ID = "real-fragment-terminal-test"
_WORKER_FAILURE = "离线 worker 执行失败"
_HTTP_401 = "HTTP 401：模型认证失败"
_HTTP_400 = "HTTP 400：模型参数不受支持"
_SIDE_MESSAGE = "材料不足，请补充原文"
_EMPTY_REPLY_NOTICE = "本轮未收到"
_FILTER_NOTICE = "内容过滤"
_LOADING_LABEL = "正在分析中"


async def _terminal_events(events: tuple[AppEvent, ...]) -> AsyncIterator[AppEvent]:
    """输出确定的终态事件，使生产 worker 负责事件折叠和队列转换。

    参数:
        events: 本轮离线事件序列。
    返回值:
        保持原顺序的异步事件迭代器。
    异常:
        无。
    """
    for event in events:
        yield event


@dataclass
class _TerminalChatService(_ChatService):
    """保留真实服务协议，提供提交异常或可控的 AppEvent 终态。"""

    events: tuple[AppEvent, ...] = ()
    failure_message: str = ""

    async def submit_turn(self, request: ChatTurnRequest) -> ChatTurnSubmission:
        """记录请求并返回离线事件，或模拟 worker 必须捕获的服务异常。

        参数:
            request: 生产 worker 提交的完整请求。
        返回值:
            绑定原会话的离线事件流。
        异常:
            RuntimeError: 设置了服务提交失败消息。
        """
        self.submissions.append(request)
        if self.failure_message:
            raise RuntimeError(self.failure_message)
        return ChatTurnSubmission(
            effective_model_name="test-model",
            first_chunk_timeout_seconds=90.0,
            session_id=request.session_id or _SESSION,
            event_stream=_terminal_events(self.events),
        )


@dataclass
class _FragmentRerunPlan:
    """保存一次自动 fragment 请求的输入与运行证据，不替代生产渲染逻辑。"""

    service: _TerminalChatService
    event_queue: Queue[ChatStreamEvent]
    runs: list[tuple[tuple[str, ...], ScriptRunnerEvent]] = field(default_factory=list)
    initial_buttons: tuple[bool, bool, str] | None = None
    scheduled: bool = False


class _FragmentScriptRunner(LocalScriptRunner):
    """补足 AppTest 未暴露的 fragment 请求入口，沿用真实 ScriptRunner 执行。

    AppTest 每次运行均创建新 runner，不能靠第二次 ``app.run()`` 测片段刷新。
    此测试适配器只在首次整页结束后投递一次自动 fragment 请求；后续整页刷新
    必须由生产片段自行请求。依赖 Streamlit testing 的内部扩展点。
    """

    def _on_script_finished(
        self,
        ctx: ScriptRunContext,
        event: ScriptRunnerEvent,
        premature_stop: bool,
    ) -> None:
        """记录真实执行范围，并在 loading 页面完成后投递终态及片段请求。

        参数:
            ctx: Streamlit 当前执行上下文。
            event: 整页或片段执行结束的真实事件。
            premature_stop: 是否因错误提前结束。
        返回值:
            无。
        异常:
            KeyError: 测试未安装运行计划或页面缺少约定按钮。
        """
        super()._on_script_finished(ctx, event, premature_stop)
        plan = cast(_FragmentRerunPlan, self.session_state[_PLAN_KEY])
        plan.runs.append((tuple(ctx.fragment_ids_this_run or ()), event))
        if plan.scheduled:
            return
        plan.scheduled = True
        tree = parse_tree_from_messages(self.forward_msgs())
        plan.initial_buttons = (
            tree.button(key=_SEND_BUTTON).disabled,
            tree.button(key=_NEW_SESSION_BUTTON).disabled,
            tree.button(key=_SEND_BUTTON).label,
        )
        # 缺少或多出片段时由测试断言报告，不在 runner 线程抛错导致等待超时。
        if len(ctx.new_fragment_ids) != 1:
            return
        run_stream_worker(
            chat_service=plan.service,
            user_text=_USER,
            session_id=_SESSION,
            ticker=_TICKER,
            event_queue=plan.event_queue,
            cancel_event=threading.Event(),
            trace_id=_TRACE_ID,
        )
        self.request_rerun(RerunData(
            query_string=ctx.query_string,
            page_script_hash=ctx.page_script_hash,
            fragment_id=next(iter(ctx.new_fragment_ids)),
            is_auto_rerun=True,
        ))


def _assert_terminal_page(
    app: AppTest,
    *,
    original_input: str,
    failure_message: str,
    expected_side_message: str,
    expected_filtered: bool,
) -> None:
    """从 AppTest 元素树检查外部控件恢复及反馈和重试输入保留。

    参数:
        app: 已完成终态整页重绘的 AppTest。
        original_input: 提交时保留的输入原文。
        failure_message: worker 异常对应的错误文本；空值表示正常事件流。
        expected_side_message: AppEvent 折叠后必须展示的警告文本。
        expected_filtered: 是否应展示内容过滤提醒。
    返回值:
        无。
    异常:
        AssertionError: 控件未恢复、输入丢失或终态反馈缺失。
    """
    assert not app.exception
    assert not app.button(key=_SEND_BUTTON).disabled
    assert _LOADING_LABEL not in app.button(key=_SEND_BUTTON).label
    assert not app.button(key=_NEW_SESSION_BUTTON).disabled
    assert app.text_area[0].value == original_input
    if failure_message:
        assert len(app.error) == 1 and failure_message in app.error[0].value
    else:
        assert not app.error
        assert any(_EMPTY_REPLY_NOTICE in notice.value for notice in app.warning)
    if expected_side_message:
        assert any(expected_side_message in notice.value for notice in app.warning)
    assert any(_FILTER_NOTICE in notice.value for notice in app.info) is expected_filtered


@pytest.mark.parametrize(
    "events,failure_message,expected_side_message,expected_filtered",
    [
        pytest.param((), _WORKER_FAILURE, "", False, id="worker-error"),
        pytest.param(
            (AppEvent(AppEventType.ERROR, {"error": _HTTP_401}),),
            "", _HTTP_401, False, id="app-event-401",
        ),
        pytest.param(
            (AppEvent(AppEventType.ERROR, {"error": _HTTP_400}),),
            "", _HTTP_400, False, id="app-event-400",
        ),
        pytest.param((), "", "", False, id="empty"),
        pytest.param(
            (
                AppEvent(AppEventType.WARNING, {"message": _SIDE_MESSAGE}),
                AppEvent(AppEventType.FINAL_ANSWER, {"filtered": True}),
            ),
            "", _SIDE_MESSAGE, True, id="empty-side-message-filtered",
        ),
    ],
)
def test_real_fragment_terminal_restores_external_controls(
    monkeypatch: pytest.MonkeyPatch,
    events: tuple[AppEvent, ...],
    failure_message: str,
    expected_side_message: str,
    expected_filtered: bool,
) -> None:
    """真实片段处理终态后必须请求整页刷新，恢复外部按钮并持久展示反馈。

    参数:
        monkeypatch: 隔离运行时注册表及 AppTest 专用 runner。
        events: 生产 worker 消费的离线 AppEvent 序列。
        failure_message: 服务提交时需要抛出的错误；空值表示正常提交。
        expected_side_message: 最终页面应保留的警告文本。
        expected_filtered: 最终页面是否应保留内容过滤提醒。
    返回值:
        无。
    异常:
        AssertionError: 未实际执行片段刷新、未恢复按钮或反馈与输入丢失。
    """
    service = _TerminalChatService(events=events, failure_message=failure_message)
    app = _app(service)
    plan = _FragmentRerunPlan(service=service, event_queue=_install_queue(app, monkeypatch))
    original_input = cast(str, app.session_state[_INPUT])
    app.session_state[_PLAN_KEY] = plan
    monkeypatch.setattr(app_test_module, "LocalScriptRunner", _FragmentScriptRunner)

    app.run()

    assert not app.exception
    assert plan.initial_buttons is not None
    send_disabled, new_session_disabled, send_label = plan.initial_buttons
    assert send_disabled and new_session_disabled and _LOADING_LABEL in send_label
    assert [event for _, event in plan.runs] == [
        ScriptRunnerEvent.SCRIPT_STOPPED_WITH_SUCCESS,
        ScriptRunnerEvent.SCRIPT_STOPPED_FOR_RERUN,
        ScriptRunnerEvent.SCRIPT_STOPPED_WITH_SUCCESS,
    ]
    initial_scope, fragment_scope, final_scope = (scope for scope, _ in plan.runs)
    assert initial_scope == final_scope == ()
    assert len(fragment_scope) == 1
    assert _STATE not in app.session_state and _TICKER not in CHAT_STREAM_RUNTIME_HANDLES
    assert len(service.submissions) == 1
    assert service.reads == [(_SESSION, 50)]
    _assert_terminal_page(
        app,
        original_input=original_input,
        failure_message=failure_message,
        expected_side_message=expected_side_message,
        expected_filtered=expected_filtered,
    )

    # 再次独立整页运行，排除仅靠旧片段元素残留造成的假阳性。
    app.run()
    _assert_terminal_page(
        app,
        original_input=original_input,
        failure_message=failure_message,
        expected_side_message=expected_side_message,
        expected_filtered=expected_filtered,
    )
