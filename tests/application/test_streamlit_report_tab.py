"""报告页真实 AppTest，全部使用可控假写作服务，禁止调用模型。"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest
from streamlit.testing.v1 import AppTest

from dayu.process_lifecycle.coordinator import RunLifecycleObserver
from dayu.services.contracts import RunAdminView, WriteRequest
from dayu.services.protocols import HostAdminServiceProtocol
from dayu.services.write_service import WRITE_CANCELLED_EXIT_CODE
from dayu.web.streamlit.report_runs import ReportRunState, write_report_run
from dayu.web.streamlit.report_runtime import ReportRunLifecycleObserver, load_report_run

_TIMEOUT = 5.0
_POLL_INTERVAL = 0.01
_REPORT = "# 苹果分析报告\n\n这是离线假服务产出的报告正文。"
_APP = '''
from pathlib import Path
import streamlit as st
from dayu.web.streamlit.components.watchlist import WatchlistItem
from dayu.web.streamlit.pages.report_tab import render_report_tab
render_report_tab(
    selected_stock=WatchlistItem(st.session_state["ticker"], "Apple", "", ""),
    workspace_root=Path(st.session_state["workspace"]),
    write_service=st.session_state["service"],
    model_names=("offline-model", "second-model"),
    host_admin_service=st.session_state["host_admin_service"],
)
'''


@dataclass
class FakeWriteService:
    """可由事件释放的离线服务，记录请求并模拟成功、失败、取消和异常。"""

    exit_code: int = 0
    error: str = ""
    produce_report: bool = True
    entered: threading.Event = field(default_factory=threading.Event)
    release: threading.Event = field(default_factory=threading.Event)
    requests: list[WriteRequest] = field(default_factory=list)
    observer: RunLifecycleObserver | None = None
    register_after: threading.Event | None = None
    registered: threading.Event = field(default_factory=threading.Event)

    def run(self, request: WriteRequest) -> int:
        """等待测试释放后写出报告或返回预设失败。

        参数:
            request: 页面构造的写作请求。
        返回值:
            预设退出码。
        异常:
            RuntimeError: 预设服务错误。
            TimeoutError: 测试未及时释放 worker。
        """
        self.requests.append(request)
        self.entered.set()
        if self.register_after is not None and not self.register_after.wait(_TIMEOUT):
            raise TimeoutError("测试未释放 Host run 注册")
        run_id = f"report-{request.write_config.ticker}-{len(self.requests)}"
        if self.observer is not None:
            self.observer.register_active_run(run_id)
            self.registered.set()
        try:
            if not self.release.wait(_TIMEOUT):
                raise TimeoutError("测试未释放假写作服务")
            if self.error:
                raise RuntimeError(self.error)
            if self.produce_report and self.exit_code == 0:
                output = Path(request.write_config.output_dir)
                output.mkdir(parents=True, exist_ok=True)
                (output / f"{request.write_config.ticker}_qual_report.md").write_text(_REPORT, encoding="utf-8")
            return self.exit_code
        finally:
            if self.observer is not None:
                self.observer.clear_active_run(run_id)

    @staticmethod
    def print_report(output_dir: str) -> int:
        """禁止页面借打印接口触发额外业务操作。

        参数:
            output_dir: 输出目录。
        返回值:
            不返回。
        异常:
            AssertionError: 页面错误使用打印接口。
        """
        raise AssertionError(f"不应调用 print_report: {output_dir}")


@dataclass
class _CancelReport:
    """模拟 Host 接受取消，由测试决定何时让假 worker 完成收尾。"""

    service: FakeWriteService
    release_worker: bool = True

    def __call__(self, run_id: str) -> RunAdminView:
        """标记取消退出码并返回管理服务视图。

        参数:
            run_id: UI 经管理服务提交的 Host 身份。
        返回值:
            已请求取消的运行视图。
        异常:
            无。
        """
        self.service.exit_code = WRITE_CANCELLED_EXIT_CODE
        if self.release_worker:
            self.service.release.set()
        now = datetime.now(timezone.utc).isoformat()
        return RunAdminView(
            run_id=run_id, session_id=None, service_type="write", state="running",
            cancel_requested_at=now, cancel_requested_reason="测试取消", cancel_reason=None,
            scene_name="write", created_at=now, started_at=now, finished_at=None, error_summary=None,
        )


def _admin_service(service: FakeWriteService, *, release_worker: bool = True) -> Mock:
    """给离线写作服务装配真 observer 与受控 Host 管理边界。

    参数:
        service: 受控写作服务。
        release_worker: 取消受理后是否立即允许 worker 返回。
    返回值:
        只模拟管理服务公开协议的调用记录器。
    异常:
        无。
    """
    admin = Mock(spec=HostAdminServiceProtocol)
    admin.cancel_run.side_effect = _CancelReport(service, release_worker)
    service.observer = ReportRunLifecycleObserver(admin)
    return admin


def _app(
    workspace: Path, service: FakeWriteService | None, ticker: str = "AAPL",
    host_admin_service: HostAdminServiceProtocol | None = None,
) -> AppTest:
    """构建全新会话，只有输入依赖放在测试会话中。

    参数:
        workspace: 测试工作区。
        service: 假服务或缺失服务。
        ticker: 当前公司。
        host_admin_service: 管理服务协议实例；为空时不能发起取消。
    返回值:
        尚未执行的 AppTest。
    异常:
        无。
    """
    app = AppTest.from_string(_APP)
    app.session_state["workspace"] = str(workspace)
    app.session_state["service"] = service
    app.session_state["ticker"] = ticker
    app.session_state["host_admin_service"] = host_admin_service
    return app


def _wait_terminal(workspace: Path) -> None:
    """有限等待假 worker 的磁盘终态，不运行真实流水线。

    参数:
        workspace: 测试工作区。
    返回值:
        无。
    异常:
        AssertionError: worker 未及时落盘。
    """
    deadline = time.monotonic() + _TIMEOUT
    while time.monotonic() < deadline:
        run = load_report_run(workspace, "AAPL")
        if run is not None and not run.state.is_active:
            return
        time.sleep(_POLL_INTERVAL)
    raise AssertionError("假 worker 未及时落终态")


def _persist_run(workspace: Path, state: str, pid: int) -> Path:
    """写入无进程内注册的合法状态，模拟独立 worker 的持久记录。

    参数:
        workspace: 测试工作区。
        state: 状态名。
        pid: worker 所在进程编号。
    返回值:
        状态文件路径。
    异常:
        OSError: 文件写入失败。
    """
    path = workspace / ".dayu/streamlit/report_runs/AAPL.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).isoformat()
    path.write_text(json.dumps({
        "state": state, "pid": pid, "started_at": now, "updated_at": now,
        "output_dir": str(workspace / "draft/AAPL"), "error": None,
        "log_path": str(path.with_suffix(".log")), "run_id": None,
    }), encoding="utf-8")
    return path


def test_form_starts_background_and_new_session_recovers(tmp_path: Path) -> None:
    """启动不阻塞页面，全新 AppTest 从磁盘恢复并展示最终报告及下载。

    参数:
        tmp_path: 独立工作区。
    返回值:
        无。
    异常:
        AssertionError: 表单参数、非阻塞行为或刷新结果错误。
    """
    service = FakeWriteService()
    app = _app(tmp_path, service).run()
    assert not app.exception
    assert any("内置" in option for option in app.selectbox[0].options)
    assert not service.requests
    assert not app.get("text")
    app.selectbox[1].select("offline-model")
    app.checkbox[0].uncheck()
    try:
        app.button[0].click().run()
        assert not app.exception and service.entered.wait(_TIMEOUT)
        assert len(service.requests) == 1
        config = service.requests[0].write_config
        assert config.ticker == "AAPL" and config.company == "Apple"
        assert config.output_dir == str(tmp_path / "draft/AAPL")
        assert Path(config.template_path).is_file()
        assert config.write_model_override_name == "offline-model" and not config.resume
        assert app.button[0].disabled
        assert any("运行中" in element.value for element in app.info)
        assert any("开始时间" in element.value and "已运行" in element.value for element in app.caption)
        refreshed = _app(tmp_path, service).run()
        assert not refreshed.exception and refreshed.button[0].disabled
        assert len(service.requests) == 1
        other_company = _app(tmp_path, service, "MSFT").run()
        assert not other_company.exception and not other_company.button[0].disabled
    finally:
        service.release.set()
        _wait_terminal(tmp_path)
    refreshed.run()
    assert not refreshed.exception and not refreshed.button[0].disabled
    assert any(_REPORT == element.value for element in refreshed.markdown)
    assert len(refreshed.get("download_button")) == 1
    assert refreshed.get("download_button")[0].proto.label == "下载 Markdown"
    assert len(service.requests) == 1


@pytest.mark.parametrize("error,exit_code", [("假服务失败", 0), ("", 4)])
def test_failure_is_persistent_and_retry_is_allowed(tmp_path: Path, error: str, exit_code: int) -> None:
    """异常及非零退出码均持久显示，失败后可重新启动。

    参数:
        tmp_path: 独立工作区。
        error: 服务异常文本。
        exit_code: 服务返回码。
    返回值:
        无。
    异常:
        AssertionError: 失败原因或重试行为错误。
    """
    service = FakeWriteService(error=error, exit_code=exit_code)
    service.release.set()
    app = _app(tmp_path, service).run()
    app.button[0].click().run()
    _wait_terminal(tmp_path)
    app = _app(tmp_path, service).run()
    assert not app.exception and not app.button[0].disabled
    assert any((error or str(exit_code)) in element.value for element in app.error)
    service.error = ""
    service.exit_code = 0
    app.button[0].click().run()
    _wait_terminal(tmp_path)
    app.run()
    assert not app.exception and app.success
    assert len(service.requests) == 2


def test_disk_running_rejects_another_tab(tmp_path: Path) -> None:
    """另一个标签页写入 running 后，旧页面提交也不能启动第二次。

    参数:
        tmp_path: 独立工作区。
    返回值:
        无。
    异常:
        AssertionError: 重复启动或没有提示。
    """
    service = FakeWriteService()
    app = _app(tmp_path, service).run()
    _persist_run(tmp_path, "running", os.getpid())
    app.button[0].click().run()
    assert not app.exception and not service.requests
    assert any("运行" in element.value for element in app.info)
    assert app.button[0].disabled


def test_dead_pid_is_stale_and_retryable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """页面直接恢复死 PID 记录为 stale，并明确允许重试。

    参数:
        tmp_path: 独立工作区。
        monkeypatch: 仅替换操作系统进程探测。
    返回值:
        无。
    异常:
        AssertionError: 失联任务仍显示运行中。
    """
    _persist_run(tmp_path, "running", os.getpid())
    monkeypatch.setattr("dayu.web.streamlit.report_runtime.is_pid_alive", lambda pid: False)
    service = FakeWriteService()
    app = _app(tmp_path, service).run()
    assert not app.exception and not app.button[0].disabled
    assert any("上次运行已中断，可重试" in element.value for element in app.warning)
    state = load_report_run(tmp_path, "AAPL")
    assert state is not None and state.state == ReportRunState.STALE
    assert not service.requests


def test_missing_service_and_corrupt_state_are_visible(tmp_path: Path) -> None:
    """服务不可用及坏状态均明确提示，不能自动覆盖或启动。

    参数:
        tmp_path: 独立工作区。
    返回值:
        无。
    异常:
        AssertionError: 错误被隐藏或坏记录被覆盖。
    """
    unavailable = _app(tmp_path, None).run()
    assert not unavailable.exception and unavailable.warning
    state = _persist_run(tmp_path, "succeeded", os.getpid())
    state.write_text("{", encoding="utf-8")
    service = FakeWriteService()
    app = _app(tmp_path, service).run()
    assert not app.exception and app.error
    assert not service.requests and state.read_text(encoding="utf-8") == "{"


def test_workspace_template_preferred_and_missing_report_explained(tmp_path: Path) -> None:
    """工作区模板优先且成功记录缺少产物时不伪造报告。

    参数:
        tmp_path: 独立工作区。
    返回值:
        无。
    异常:
        AssertionError: 模板优先级或产物缺失提示错误。
    """
    template = tmp_path / "assets/定性分析模板.md"
    template.parent.mkdir()
    template.write_text("# 自定义模板", encoding="utf-8")
    _persist_run(tmp_path, "succeeded", os.getpid())
    app = _app(tmp_path, FakeWriteService()).run()
    assert not app.exception
    selected_template = app.selectbox[0].value
    assert isinstance(selected_template, str) and "工作区" in selected_template
    assert any("未找到" in element.value for element in app.warning)
    assert not app.get("download_button")


def test_cancel_registered_run_refreshes_and_allows_new_report(tmp_path: Path) -> None:
    """运行身份经真 observer 落盘，取消后新会话展示终态并允许再生成。

    参数:
        tmp_path: 独立工作区。
    返回值:
        无。
    异常:
        AssertionError: 取消未传给对应 Host run、刷新丢失终态或无法再次生成。
    """
    service = FakeWriteService()
    admin = _admin_service(service)
    app = _app(tmp_path, service, host_admin_service=admin).run()
    try:
        app.button[0].click().run()
        assert service.registered.wait(_TIMEOUT)
        run = load_report_run(tmp_path, "AAPL")
        assert run is not None and run.run_id == "report-AAPL-1"
        app.run()
        assert [button.label for button in app.button] == ["生成报告", "取消"]
        assert not app.button[1].disabled
        app.button[1].click().run()
        _wait_terminal(tmp_path)
        refreshed = _app(tmp_path, service, host_admin_service=admin).run()
        assert not refreshed.exception
        assert any("已取消" in notice.value for notice in refreshed.info)
        assert [button.label for button in refreshed.button] == ["生成报告"]
        assert not refreshed.button[0].disabled and not refreshed.get("download_button")
        admin.cancel_run.assert_called_once_with("report-AAPL-1")
        cancelled = load_report_run(tmp_path, "AAPL")
        assert cancelled is not None and cancelled.state is ReportRunState.CANCELLED
        assert cancelled.run_id == "report-AAPL-1"
        service.exit_code = 0
        refreshed.button[0].click().run()
        _wait_terminal(tmp_path)
        refreshed.run()
        assert not refreshed.exception and refreshed.success
        assert len(service.requests) == 2
    finally:
        service.release.set()
        _wait_terminal(tmp_path)


def test_cancel_before_registration_persists_intent_and_disables_duplicate(tmp_path: Path) -> None:
    """Host 尚未提供身份时也能提交取消，刷新保留意图并在注册后补发一次。

    参数:
        tmp_path: 独立工作区。
    返回值:
        无。
    异常:
        AssertionError: 提前取消丢失、重复发给 Host 或取消中允许重复生成。
    """
    register_after = threading.Event()
    service = FakeWriteService(register_after=register_after)
    admin = _admin_service(service, release_worker=False)
    app = _app(tmp_path, service, host_admin_service=admin).run()
    try:
        app.button[0].click().run()
        assert service.entered.wait(_TIMEOUT)
        app.button[1].click().run()
        assert not app.exception
        admin.cancel_run.assert_not_called()
        pending = load_report_run(tmp_path, "AAPL")
        assert pending is not None and pending.state is ReportRunState.CANCELLING
        assert pending.run_id is None
        refreshed = _app(tmp_path, service, host_admin_service=admin).run()
        assert any("取消请求已提交" in notice.value for notice in refreshed.info)
        assert all(button.disabled for button in refreshed.button)
        register_after.set()
        assert service.registered.wait(_TIMEOUT)
        admin.cancel_run.assert_called_once_with("report-AAPL-1")
        refreshed.run()
        assert refreshed.button[1].disabled
    finally:
        register_after.set()
        service.release.set()
        _wait_terminal(tmp_path)
    refreshed.run()
    assert any("已取消" in notice.value for notice in refreshed.info)
    assert [button.label for button in refreshed.button] == ["生成报告"]


def test_host_cancel_error_stays_running_and_can_retry(tmp_path: Path) -> None:
    """管理服务拒绝取消时展示原因且保留正在执行的任务和重试按钮。

    参数:
        tmp_path: 独立工作区。
    返回值:
        无。
    异常:
        AssertionError: Host 错误被隐藏、任务被误判失败或无法重试取消。
    """
    service = FakeWriteService()
    admin = _admin_service(service)
    admin.cancel_run.side_effect = RuntimeError("Host 暂时不可用")
    app = _app(tmp_path, service, host_admin_service=admin).run()
    try:
        app.button[0].click().run()
        assert service.registered.wait(_TIMEOUT)
        app.button[1].click().run()
        assert not app.exception
        assert any("取消请求失败" in item.value and "Host 暂时不可用" in item.value for item in app.error)
        assert app.button[0].disabled and not app.button[1].disabled
        run = load_report_run(tmp_path, "AAPL")
        assert run is not None and run.state is ReportRunState.RUNNING
        refreshed = _app(tmp_path, service, host_admin_service=admin).run()
        assert refreshed.error and not refreshed.button[1].disabled
        admin.cancel_run.side_effect = _CancelReport(service)
        refreshed.button[1].click().run()
        _wait_terminal(tmp_path)
        refreshed.run()
        assert not refreshed.exception
        assert any("已取消" in notice.value for notice in refreshed.info)
        assert admin.cancel_run.call_count == 2
    finally:
        service.release.set()
        _wait_terminal(tmp_path)


def test_missing_admin_service_disables_cancel_with_reason(tmp_path: Path) -> None:
    """恢复运行记录但管理服务不可用时显示禁用取消控件与原因。

    参数:
        tmp_path: 独立工作区。
    返回值:
        无。
    异常:
        AssertionError: 页面暗示可提交实际不可用的取消操作。
    """
    _persist_run(tmp_path, "running", os.getpid())
    app = _app(tmp_path, FakeWriteService()).run()
    assert not app.exception
    assert [button.label for button in app.button] == ["生成报告", "取消"]
    assert all(button.disabled for button in app.button)
    assert any("取消功能不可用" in notice.value for notice in app.warning)


@pytest.mark.parametrize("error", [OSError("状态文件只读"), ValueError("状态文件损坏"), RuntimeError("状态锁正忙")])
def test_cancel_state_failure_is_visible_without_losing_running_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: Exception,
) -> None:
    """状态读写或锁失败在页面明确显示，不误报已受理取消。

    参数:
        tmp_path: 独立工作区。
        monkeypatch: 替换取消适配器的公开调用边界。
        error: 适配器公开契约允许传播的错误。
    返回值:
        无。
    异常:
        AssertionError: 错误被隐藏或运行事实被页面更改。
    """
    _persist_run(tmp_path, "running", os.getpid())
    service = FakeWriteService()
    admin = _admin_service(service)
    cancel = Mock(side_effect=error)
    monkeypatch.setattr("dayu.web.streamlit.pages.report_tab.cancel_report_run", cancel)
    app = _app(tmp_path, service, host_admin_service=admin).run()
    app.button[1].click().run()
    assert not app.exception
    assert any(str(error) in notice.value for notice in app.error)
    assert not any("取消请求已提交" in notice.value for notice in app.info)
    assert app.button[0].disabled and not app.button[1].disabled
    cancel.assert_called_once()
    admin.cancel_run.assert_not_called()
    run = load_report_run(tmp_path, "AAPL")
    assert run is not None and run.state is ReportRunState.RUNNING


def test_old_cancel_button_cannot_cancel_replacement_run(tmp_path: Path) -> None:
    """旧页面的取消点击不能在重绘时错误匹配同公司新任务的按钮。

    参数:
        tmp_path: 独立工作区。
    返回值:
        无。
    异常:
        AssertionError: 旧任务控件事件错误取消替换后的新任务。
    """
    state_path = _persist_run(tmp_path, "running", os.getpid())
    service = FakeWriteService()
    admin = _admin_service(service)
    app = _app(tmp_path, service, host_admin_service=admin).run()
    original = load_report_run(tmp_path, "AAPL")
    assert original is not None
    replacement = replace(original, run_id="replacement-run", log_path=str(tmp_path / "replacement.log"))
    write_report_run(state_path, replacement)
    app.button[1].click().run()
    assert not app.exception
    admin.cancel_run.assert_not_called()
    assert load_report_run(tmp_path, "AAPL") == replacement
    assert app.button[0].disabled and not app.button[1].disabled
