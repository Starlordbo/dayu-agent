"""报告取消与线程归属回归，假写作服务调用真实生命周期观察者。"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from threading import Event
from unittest.mock import Mock, patch

import pytest

from dayu.services.protocols import HostAdminServiceProtocol
from dayu.services.write_service import WRITE_CANCELLED_EXIT_CODE
from dayu.services.contracts import WriteRequest
from dayu.web.streamlit.report_runs import ReportRun, ReportRunState, write_report_run
from dayu.web.streamlit.report_runtime import (
    ReportRunLifecycleObserver,
    cancel_report_run,
    load_report_run,
    start_report_run,
)
from tests.application.test_streamlit_report_runtime import (
    _WAIT_SECONDS, _payload, _request, _state_path, _wait_terminal, _write_payload,
)


@dataclass
class _ObservedWriteService:
    """用事件控制登记与退出，模拟 WriteService 的 observer 生命周期契约。"""

    observer: ReportRunLifecycleObserver
    allow_register: Event = field(default_factory=Event)
    entered: Event = field(default_factory=Event)
    registered: Event = field(default_factory=Event)
    release: Event = field(default_factory=Event)
    exit_code: int = WRITE_CANCELLED_EXIT_CODE

    def run(self, request: WriteRequest) -> int:
        """登记 ticker 对应的假 Host 身份并等待退出。

        参数:
            request: 写作请求，仅使用 ticker 生成测试身份。
        返回值:
            预设退出码。
        异常:
            AssertionError: 事件未及时释放。
        """
        self.entered.set()
        assert self.allow_register.wait(_WAIT_SECONDS)
        run_id = f"host-{request.write_config.ticker}"
        self.observer.register_active_run(run_id)
        self.registered.set()
        try:
            assert self.release.wait(_WAIT_SECONDS)
            return self.exit_code
        finally:
            self.observer.clear_active_run(run_id)

    @staticmethod
    def print_report(output_dir: str) -> int:
        """拒绝测试触发无关打印路径。

        参数:
            output_dir: 输出目录。
        返回值:
            不返回。
        异常:
            AssertionError: 调用即失败。
        """
        raise AssertionError(output_dir)


def _current(workspace: Path, ticker: str = "AAPL") -> ReportRun:
    """读取必须存在的磁盘快照。

    参数:
        workspace: 测试工作区。
        ticker: 股票代码。
    返回值:
        已存在的快照。
    异常:
        AssertionError: 状态未落盘。
    """
    run = load_report_run(workspace, ticker)
    assert run is not None
    return run


@pytest.mark.parametrize("early", [False, True])
def test_cancel_persists_identity_and_intent_until_worker_exits(tmp_path: Path, early: bool) -> None:
    """提前或登记后取消均只提交一次，退出前仍排他，退出后保留身份可重试。

    参数:
        tmp_path: 测试工作区。
        early: 是否在登记前提交取消。
    返回值:
        无。
    异常:
        AssertionError: 意图、身份、幂等或终态错误。
    """
    admin = Mock(spec=HostAdminServiceProtocol)
    service = _ObservedWriteService(ReportRunLifecycleObserver(admin))
    try:
        assert start_report_run(workspace_root=tmp_path, write_service=service, request=_request(tmp_path))
        assert service.entered.wait(_WAIT_SECONDS)
        if not early:
            service.allow_register.set()
            assert service.registered.wait(_WAIT_SECONDS)
        run = _current(tmp_path)
        assert run.run_id == (None if early else "host-AAPL")
        requested = cancel_report_run(workspace_root=tmp_path, ticker="AAPL", host_admin_service=admin, expected_run=run)
        assert requested is not None and requested.state is ReportRunState.CANCELLING
        cancel_report_run(workspace_root=tmp_path, ticker="AAPL", host_admin_service=admin, expected_run=run)
        if early:
            admin.cancel_run.assert_not_called()
            service.allow_register.set()
            assert service.registered.wait(_WAIT_SECONDS)
        admin.cancel_run.assert_called_once_with("host-AAPL")
        assert _current(tmp_path).run_id == "host-AAPL"
        assert not start_report_run(workspace_root=tmp_path, write_service=service, request=_request(tmp_path))
    finally:
        service.allow_register.set()
        service.release.set()
    terminal = _wait_terminal(tmp_path)
    assert terminal.state is ReportRunState.CANCELLED and terminal.error is None
    assert terminal.run_id == "host-AAPL"
    assert "cancelled" in Path(terminal.log_path).read_text(encoding="utf-8")
    assert cancel_report_run(workspace_root=tmp_path, ticker="AAPL", host_admin_service=admin, expected_run=run) == terminal
    admin.cancel_run.assert_called_once()
    assert start_report_run(workspace_root=tmp_path, write_service=service, request=_request(tmp_path))
    assert _wait_terminal(tmp_path).state is ReportRunState.CANCELLED


@pytest.mark.parametrize("early", [False, True])
def test_host_cancel_error_keeps_running_and_can_retry(tmp_path: Path, early: bool) -> None:
    """Host 拒绝取消时保留可见原因，并且不终止写作或丢失重试入口。

    参数:
        tmp_path: 测试工作区。
        early: 是否从 observer 自动补发取消。
    返回值:
        无。
    异常:
        AssertionError: Host 错误被隐藏或变成写作失败。
    """
    admin = Mock(spec=HostAdminServiceProtocol)
    admin.cancel_run.side_effect = RuntimeError("Host 暂不可用")
    service = _ObservedWriteService(ReportRunLifecycleObserver(admin))
    try:
        assert start_report_run(workspace_root=tmp_path, write_service=service, request=_request(tmp_path))
        assert service.entered.wait(_WAIT_SECONDS)
        if not early:
            service.allow_register.set()
            assert service.registered.wait(_WAIT_SECONDS)
        cancel_report_run(workspace_root=tmp_path, ticker="AAPL", host_admin_service=admin, expected_run=_current(tmp_path))
        service.allow_register.set()
        assert service.registered.wait(_WAIT_SECONDS)
        failed_request = _current(tmp_path)
        assert failed_request.state is ReportRunState.RUNNING
        assert failed_request.error is not None and "Host 暂不可用" in failed_request.error
        admin.cancel_run.side_effect = None
        retried = cancel_report_run(workspace_root=tmp_path, ticker="AAPL", host_admin_service=admin, expected_run=failed_request)
        assert retried is not None and retried.state is ReportRunState.CANCELLING and retried.error is None
        assert admin.cancel_run.call_count == 2
    finally:
        service.allow_register.set()
        service.release.set()
    assert _wait_terminal(tmp_path).state is ReportRunState.CANCELLED


def test_shared_observer_keeps_parallel_tickers_isolated(tmp_path: Path) -> None:
    """同一个 observer 与服务跨线程并行时，身份不能串到另一 ticker。

    参数:
        tmp_path: 测试工作区。
    返回值:
        无。
    异常:
        AssertionError: 线程上下文串写身份或取消到错误任务。
    """
    admin = Mock(spec=HostAdminServiceProtocol)
    service = _ObservedWriteService(ReportRunLifecycleObserver(admin))
    service.allow_register.set()
    try:
        for ticker in ("AAPL", "MSFT"):
            service.registered.clear()
            assert start_report_run(workspace_root=tmp_path, write_service=service, request=_request(tmp_path, ticker))
            assert service.registered.wait(_WAIT_SECONDS)
        for ticker in ("AAPL", "MSFT"):
            assert _current(tmp_path, ticker).run_id == f"host-{ticker}"
        cancel_report_run(workspace_root=tmp_path, ticker="MSFT", host_admin_service=admin, expected_run=_current(tmp_path, "MSFT"))
        admin.cancel_run.assert_called_once_with("host-MSFT")
        assert _current(tmp_path, "AAPL").state is ReportRunState.RUNNING
    finally:
        service.release.set()
    for ticker in ("AAPL", "MSFT"):
        assert _wait_terminal(tmp_path, ticker).run_id == f"host-{ticker}"
    with pytest.raises(RuntimeError, match="上下文"):
        service.observer.register_active_run("unbound")


@pytest.mark.parametrize("state", [ReportRunState.SUCCEEDED, ReportRunState.FAILED, ReportRunState.CANCELLED, ReportRunState.STALE])
def test_terminal_and_replaced_tasks_cannot_be_cancelled(tmp_path: Path, state: ReportRunState) -> None:
    """旧页面按钮不能取消终态记录或同 ticker 后续生成任务。

    参数:
        tmp_path: 测试工作区。
        state: 需要保护的终态。
    返回值:
        无。
    异常:
        AssertionError: 错误任务被提交取消。
    """
    admin = Mock(spec=HostAdminServiceProtocol)
    _write_payload(tmp_path, _payload(tmp_path))
    old = _current(tmp_path)
    terminal = replace(old, state=state, error="失败原因" if state is ReportRunState.FAILED else None)
    write_report_run(_state_path(tmp_path), terminal)
    assert cancel_report_run(workspace_root=tmp_path, ticker="AAPL", host_admin_service=admin, expected_run=old) == terminal
    new = replace(old, log_path=str(tmp_path / "new.log"), run_id="new-host-id")
    write_report_run(_state_path(tmp_path), new)
    assert cancel_report_run(workspace_root=tmp_path, ticker="AAPL", host_admin_service=admin, expected_run=old) == new
    admin.cancel_run.assert_not_called()


def test_cancelling_dead_process_is_stale_and_missing_cancel_is_absent(tmp_path: Path) -> None:
    """等待取消的死进程沿用中断恢复，缺失状态不能凭旧快照创建任务。

    参数:
        tmp_path: 测试工作区。
    返回值:
        无。
    异常:
        AssertionError: 死进程仍占用生成入口或取消凭空创建状态。
    """
    admin = Mock(spec=HostAdminServiceProtocol)
    payload = _payload(tmp_path)
    payload["state"] = "cancelling"
    _write_payload(tmp_path, payload)
    with patch("dayu.web.streamlit.report_runtime.is_pid_alive", return_value=False):
        stale = _current(tmp_path)
    assert stale.state is ReportRunState.STALE
    _state_path(tmp_path).unlink()
    assert cancel_report_run(workspace_root=tmp_path, ticker="AAPL", host_admin_service=admin, expected_run=stale) is None
    admin.cancel_run.assert_not_called()


def test_completion_wins_without_fabricating_cancelled(tmp_path: Path) -> None:
    """已提交意图仍由服务退出契约决定终态，不能凭按钮点击伪造已取消。

    参数:
        tmp_path: 测试工作区。
    返回值:
        无。
    异常:
        AssertionError: 成功退出被页面错误改写为取消。
    """
    admin = Mock(spec=HostAdminServiceProtocol)
    service = _ObservedWriteService(ReportRunLifecycleObserver(admin), exit_code=0)
    service.allow_register.set()
    try:
        assert start_report_run(workspace_root=tmp_path, write_service=service, request=_request(tmp_path))
        assert service.registered.wait(_WAIT_SECONDS)
        cancel_report_run(workspace_root=tmp_path, ticker="AAPL", host_admin_service=admin, expected_run=_current(tmp_path))
    finally:
        service.release.set()
    assert _wait_terminal(tmp_path).state is ReportRunState.SUCCEEDED
