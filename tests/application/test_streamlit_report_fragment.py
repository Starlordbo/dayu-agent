"""用真实自动片段请求验证报告轮询，不以整页 AppTest 重跑代替 fragment。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

import pytest
import streamlit.testing.v1.app_test as app_test_module
from streamlit.runtime.scriptrunner import ScriptRunnerEvent
from streamlit.runtime.scriptrunner_utils.script_requests import RerunData
from streamlit.runtime.scriptrunner_utils.script_run_context import ScriptRunContext
from streamlit.testing.v1.element_tree import parse_tree_from_messages
from streamlit.testing.v1.local_script_runner import LocalScriptRunner

from tests.application.test_streamlit_report_tab import (
    FakeWriteService, _REPORT, _TIMEOUT, _admin_service, _app, _wait_terminal,
)


_PLAN_KEY = "test_report_fragment_rerun_plan"
_POLL_SECONDS = 2.0


@dataclass
class _ReportFragmentPlan:
    """保留真实片段请求及初始运行态的证据，仅用于测试 runner。"""

    workspace: Path
    service: FakeWriteService
    runs: list[tuple[tuple[str, ...], ScriptRunnerEvent]] = field(default_factory=list)
    auto_reruns: list[tuple[str, float]] = field(default_factory=list)
    initial_button_disabled: bool | None = None
    initial_cancel_disabled: bool | None = None
    scheduled: bool = False


class _ReportFragmentScriptRunner(LocalScriptRunner):
    """沿用真实 ScriptRunner，仅补足 AppTest 未公开的自动片段请求入口。"""

    def _on_script_finished(
        self,
        ctx: ScriptRunContext,
        event: ScriptRunnerEvent,
        premature_stop: bool,
    ) -> None:
        """记录执行范围，在初始运行页结束后释放 worker 并请求一次片段刷新。

        参数:
            ctx: 当前 Streamlit 脚本上下文。
            event: 整页或片段完成事件。
            premature_stop: 脚本是否提前终止。
        返回值:
            无。
        异常:
            KeyError: 测试未安装片段运行计划。
            AssertionError: 假 worker 未及时写入终态。
        """
        super()._on_script_finished(ctx, event, premature_stop)
        plan = cast(_ReportFragmentPlan, self.session_state[_PLAN_KEY])
        plan.runs.append((tuple(ctx.fragment_ids_this_run or ()), event))
        if plan.scheduled:
            return
        plan.scheduled = True
        messages = self.forward_msgs()
        plan.auto_reruns = [
            (message.auto_rerun.fragment_id, message.auto_rerun.interval)
            for message in messages if message.HasField("auto_rerun")
        ]
        tree = parse_tree_from_messages(messages)
        plan.initial_button_disabled = tree.button[0].disabled
        plan.initial_cancel_disabled = tree.button[1].disabled
        # 缺失或多出片段时让主测试给出明确断言，避免后台线程异常遮蔽根因。
        if len(ctx.new_fragment_ids) != 1:
            return
        plan.service.release.set()
        _wait_terminal(plan.workspace)
        self.request_rerun(RerunData(
            query_string=ctx.query_string,
            page_script_hash=ctx.page_script_hash,
            fragment_id=next(iter(ctx.new_fragment_ids)),
            is_auto_rerun=True,
        ))


@pytest.mark.parametrize("cancelled", [False, True])
def test_report_terminal_fragment_updates_result_and_controls_without_full_rerun(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cancelled: bool,
) -> None:
    """实际两秒轮询消息对应的片段应读取磁盘终态并独立恢复按钮与报告。

    参数:
        tmp_path: 独立工作区。
        monkeypatch: 仅替换 AppTest 创建 runner 的测试扩展点。
        cancelled: 是否在自动片段刷新前先提交取消。
    返回值:
        无。
    异常:
        AssertionError: 自动轮询消息、片段范围、按钮或报告状态不符合契约。
    """
    service = FakeWriteService()
    admin = _admin_service(service, release_worker=False)
    app = _app(tmp_path, service, host_admin_service=admin).run()
    try:
        app.button[0].click().run()
        assert not app.exception and app.button[0].disabled
        assert service.registered.wait(_TIMEOUT)
        assert len(service.requests) == 1
        if cancelled:
            app.button[1].click().run()
            assert app.button[1].disabled
        plan = _ReportFragmentPlan(workspace=tmp_path, service=service)
        app.session_state[_PLAN_KEY] = plan
        monkeypatch.setattr(app_test_module, "LocalScriptRunner", _ReportFragmentScriptRunner)

        app.run()

        assert not app.exception
        assert plan.initial_button_disabled is True
        assert plan.initial_cancel_disabled is cancelled
        assert [event for _, event in plan.runs] == [
            ScriptRunnerEvent.SCRIPT_STOPPED_WITH_SUCCESS,
            ScriptRunnerEvent.FRAGMENT_STOPPED_WITH_SUCCESS,
        ]
        initial_scope, final_scope = (scope for scope, _ in plan.runs)
        assert initial_scope == () and len(final_scope) == 1
        assert plan.auto_reruns == [(final_scope[0], _POLL_SECONDS)]
        assert not app.button[0].disabled
        assert [button.label for button in app.button] == ["生成报告"]
        if cancelled:
            assert any("已取消" in notice.value for notice in app.info)
            assert not app.get("download_button")
        else:
            assert any("报告生成完成" in notice.value for notice in app.success)
            assert any(_REPORT == element.value for element in app.markdown)
            assert len(app.get("download_button")) == 1
        assert len(service.requests) == 1
    finally:
        service.release.set()
        _wait_terminal(tmp_path)
