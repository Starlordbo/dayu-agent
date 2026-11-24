"""报告取消跨真实 WriteService、SQLite Host 与 worker 状态文件的离线集成测试。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event, get_ident

import pytest

from dayu.contracts.cancellation import CancellationToken
from dayu.contracts.run import RunState
from dayu.host.executor import DefaultHostExecutor
from dayu.host.host import Host
from dayu.host.host_store import HostStore
from dayu.host.run_registry import SQLiteRunRegistry
from dayu.host.session_registry import SQLiteSessionRegistry
from dayu.services.contracts import RunAdminView, WriteRequest
from dayu.services.host_admin_service import HostAdminService
from dayu.services.write_service import WRITE_CANCELLED_EXIT_CODE, WriteService
from dayu.web.streamlit.report_runs import ReportRun, ReportRunState
from dayu.web.streamlit.report_runtime import (
    ReportRunLifecycleObserver,
    cancel_report_run,
    load_report_run,
    start_report_run,
)
from tests.application.test_streamlit_report_runtime import (
    _POLL_SECONDS,
    _WAIT_SECONDS,
    _request,
    _wait_terminal,
)
from tests.application.test_write_service import (
    _FakeSceneExecutionAcceptancePreparer,
    _build_workspace,
)


_FIRST_TICKER = "AAPL"
_SECOND_TICKER = "MSFT"
_SUCCESS_EXIT_CODE = 0


@dataclass
class _PipelineBoundary:
    """记录真实 Host 令牌并在可控协作边界退出，不运行写作或模型。"""

    entered: Event = field(default_factory=Event)
    cancellation_seen: Event = field(default_factory=Event)
    release: Event = field(default_factory=Event)
    host_session_id: str | None = None
    worker_ident: int | None = None
    token: CancellationToken | None = None

    def run(self, *, host_session_id: str, cancellation_token: CancellationToken | None) -> int:
        """等待测试释放，再通过真实 Host 令牌响应取消。

        参数:
            host_session_id: WriteService 创建的真实 Host 会话。
            cancellation_token: 同步 Host operation 提供的协作式取消令牌。
        返回值:
            未取消时返回成功退出码。
        异常:
            AssertionError: 缺少令牌或测试未及时释放流水线。
            CancelledError: Host 令牌在协作边界触发取消。
        """
        assert cancellation_token is not None
        self.host_session_id = host_session_id
        self.worker_ident = get_ident()
        self.token = cancellation_token
        unregister = cancellation_token.on_cancel(self.cancellation_seen.set)
        self.entered.set()
        try:
            assert self.release.wait(_WAIT_SECONDS)
            cancellation_token.raise_if_cancelled()
            return _SUCCESS_EXIT_CODE
        finally:
            unregister()


@dataclass
class _PipelineRouter:
    """仅替代流水线主体，按显式请求 ticker 路由到独立控制点。"""

    boundaries: dict[str, _PipelineBoundary]

    def run(
        self,
        request: WriteRequest,
        *,
        host_session_id: str,
        cancellation_token: CancellationToken | None = None,
    ) -> int:
        """转交请求对应的假流水线，不改变 WriteService 的生命周期行为。

        参数:
            request: 真实服务收到的写作请求。
            host_session_id: 真实 Host 会话身份。
            cancellation_token: 真实 Host 运行取消令牌。
        返回值:
            受控流水线退出码。
        异常:
            KeyError: 测试未声明该 ticker。
            AssertionError: 流水线控制事件不满足前提。
            CancelledError: Host 令牌触发取消。
        """
        return self.boundaries[request.write_config.ticker].run(
            host_session_id=host_session_id,
            cancellation_token=cancellation_token,
        )


@dataclass
class _ExitRecorder:
    """旁路记录真实 WriteService 返回值，保持 worker 的调用契约。"""

    execute: Callable[[WriteRequest], int]
    exit_codes: dict[str, int] = field(default_factory=dict)

    def run(self, request: WriteRequest) -> int:
        """调用原始服务并记录实际返回码。

        参数:
            request: worker 传入的完整写作请求。
        返回值:
            原始 WriteService 的返回码。
        异常:
            Exception: 原始服务异常原样传播。
        """
        result = self.execute(request)
        self.exit_codes[request.write_config.ticker] = result
        return result


@dataclass
class _RegistrationGate:
    """暂停真实 observer 登记，用确定事件制造 run_id 尚未落盘的窗口。"""

    register: Callable[[str], None]
    entered: Event = field(default_factory=Event)
    release: Event = field(default_factory=Event)

    def register_active_run(self, run_id: str) -> None:
        """在原 worker 上下文中等待后调用真实 observer。

        参数:
            run_id: 真实 Host 分配的运行身份。
        返回值:
            无。
        异常:
            AssertionError: 测试未及时释放登记事件。
            RuntimeError: 真实 observer 拒绝当前 worker 上下文。
            OSError: 真实 observer 无法持久化状态。
        """
        self.entered.set()
        assert self.release.wait(_WAIT_SECONDS)
        self.register(run_id)


@dataclass(frozen=True)
class _Harness:
    """共享真实服务与 observer，仅通过流水线控制点管理离线执行。"""

    service: WriteService
    admin: HostAdminService
    observer: ReportRunLifecycleObserver
    pipelines: dict[str, _PipelineBoundary]
    recorder: _ExitRecorder


def _build_harness(workspace: Path, monkeypatch: pytest.MonkeyPatch, tickers: tuple[str, ...]) -> _Harness:
    """装配真实 SQLite Host 与 WriteService，并替换唯一流水线执行边界。

    参数:
        workspace: 隔离工作区。
        monkeypatch: 测试结束时恢复替换的工具。
        tickers: 允许启动的报告股票代码。
    返回值:
        可提交、取消与检查返回码的测试环境。
    异常:
        OSError: 工作区文件无法创建。
        sqlite3.Error: Host 存储初始化失败。
    """
    store = HostStore(workspace / "host.db")
    store.initialize_schema()
    registry = SQLiteRunRegistry(store)
    executor = DefaultHostExecutor(
        run_registry=registry,
        cancellation_bridge_poll_interval_seconds=_POLL_SECONDS,
    )
    host = Host(executor=executor, session_registry=SQLiteSessionRegistry(store), run_registry=registry)
    admin = HostAdminService(host=host)
    observer = ReportRunLifecycleObserver(admin)
    service = WriteService(
        host=host,
        host_governance=host,
        workspace=_build_workspace(workspace),
        scene_execution_acceptance_preparer=_FakeSceneExecutionAcceptancePreparer(workspace_dir=workspace),
        run_lifecycle_observer=observer,
    )
    pipelines = {ticker: _PipelineBoundary() for ticker in tickers}
    monkeypatch.setattr(service, "_run_pipeline", _PipelineRouter(pipelines).run)
    recorder = _ExitRecorder(service.run)
    monkeypatch.setattr(service, "run", recorder.run)
    return _Harness(service, admin, observer, pipelines, recorder)


def _current_report(workspace: Path, ticker: str) -> ReportRun:
    """读取必须存在的报告状态，避免测试把缺失记录误判为终态。

    参数:
        workspace: 报告工作区。
        ticker: 目标股票代码。
    返回值:
        当前磁盘状态。
    异常:
        AssertionError: 报告记录不存在。
        OSError: 状态文件读取失败。
    """
    current = load_report_run(workspace, ticker)
    assert current is not None
    return current


def _host_view(admin: HostAdminService, run: ReportRun) -> RunAdminView:
    """通过管理 Service 查询报告对应的真实 Host 身份。

    参数:
        admin: 真实管理服务。
        run: 持久化报告状态。
    返回值:
        真实 Host 运行视图。
    异常:
        AssertionError: 报告未登记身份或 Host 运行不存在。
    """
    assert run.run_id is not None
    view = admin.get_run(run.run_id)
    assert view is not None
    return view


@pytest.mark.integration
@pytest.mark.parametrize("early", [False, True])
def test_real_host_cancel_reaches_worker_terminal_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, early: bool,
) -> None:
    """真实 Host 取消在登记前后均穿过同步 Service 返回契约并持久化 cancelled。

    参数:
        tmp_path: 隔离工作区。
        monkeypatch: 流水线与观察记录的替换工具。
        early: 是否在 run_id 登记前提交取消意图。
    返回值:
        无。
    异常:
        AssertionError: 上下文、Host 状态、返回码或磁盘终态不符合契约。
    """
    harness = _build_harness(tmp_path, monkeypatch, (_FIRST_TICKER,))
    pipeline = harness.pipelines[_FIRST_TICKER]
    gate = _RegistrationGate(harness.observer.register_active_run)
    if early:
        monkeypatch.setattr(harness.observer, "register_active_run", gate.register_active_run)
    assert start_report_run(
        workspace_root=tmp_path, write_service=harness.service, request=_request(tmp_path, _FIRST_TICKER),
    )
    try:
        assert (gate.entered if early else pipeline.entered).wait(_WAIT_SECONDS)
        initial = _current_report(tmp_path, _FIRST_TICKER)
        assert initial.state is ReportRunState.RUNNING
        if early:
            assert initial.run_id is None
            assert not pipeline.entered.is_set()
        else:
            assert _host_view(harness.admin, initial).state == RunState.RUNNING.value
        requested = cancel_report_run(
            workspace_root=tmp_path, ticker=_FIRST_TICKER,
            host_admin_service=harness.admin, expected_run=initial,
        )
        assert requested is not None and requested.state is ReportRunState.CANCELLING
        if early:
            host_runs = harness.admin.list_runs()
            assert len(host_runs) == 1 and host_runs[0].cancel_requested_at is None
            gate.release.set()
        assert pipeline.entered.wait(_WAIT_SECONDS)
        assert pipeline.worker_ident != get_ident()
        assert pipeline.cancellation_seen.wait(_WAIT_SECONDS)
        registered = _current_report(tmp_path, _FIRST_TICKER)
        assert registered.state is ReportRunState.CANCELLING
        view = _host_view(harness.admin, registered)
        assert view.session_id == pipeline.host_session_id
        assert view.state == RunState.RUNNING.value
        assert view.cancel_requested_at is not None
        # Host 取消已送达令牌，但协作边界尚未释放，页面必须继续占用任务。
        assert not start_report_run(
            workspace_root=tmp_path, write_service=harness.service, request=_request(tmp_path, _FIRST_TICKER),
        )
    finally:
        gate.release.set()
        pipeline.release.set()
        terminal = _wait_terminal(tmp_path, _FIRST_TICKER)
    assert terminal.state is ReportRunState.CANCELLED and terminal.error is None
    assert harness.recorder.exit_codes[_FIRST_TICKER] == WRITE_CANCELLED_EXIT_CODE
    settled = _host_view(harness.admin, terminal)
    assert settled.state == RunState.CANCELLED.value and settled.finished_at is not None
    assert load_report_run(tmp_path, _FIRST_TICKER) == terminal


@pytest.mark.integration
def test_real_host_shared_observer_keeps_parallel_report_cancellation_isolated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同一真实服务与 observer 并行处理两只股票时，取消只影响所选 Host run。

    参数:
        tmp_path: 隔离工作区。
        monkeypatch: 唯一假流水线边界与退出码记录的替换工具。
    返回值:
        无。
    异常:
        AssertionError: ticker 身份、令牌、Host 状态或报告终态发生串写。
    """
    tickers = (_FIRST_TICKER, _SECOND_TICKER)
    harness = _build_harness(tmp_path, monkeypatch, tickers)
    started: list[str] = []
    try:
        for ticker in tickers:
            assert start_report_run(
                workspace_root=tmp_path, write_service=harness.service, request=_request(tmp_path, ticker),
            )
            started.append(ticker)
        for pipeline in harness.pipelines.values():
            assert pipeline.entered.wait(_WAIT_SECONDS)
        snapshots = {ticker: _current_report(tmp_path, ticker) for ticker in tickers}
        assert snapshots[_FIRST_TICKER].run_id != snapshots[_SECOND_TICKER].run_id
        for ticker, report in snapshots.items():
            assert _host_view(harness.admin, report).session_id == harness.pipelines[ticker].host_session_id
        requested = cancel_report_run(
            workspace_root=tmp_path, ticker=_SECOND_TICKER,
            host_admin_service=harness.admin, expected_run=snapshots[_SECOND_TICKER],
        )
        assert requested is not None and requested.state is ReportRunState.CANCELLING
        assert harness.pipelines[_SECOND_TICKER].cancellation_seen.wait(_WAIT_SECONDS)
        unaffected = harness.pipelines[_FIRST_TICKER]
        assert unaffected.token is not None and not unaffected.token.is_cancelled()
        assert _current_report(tmp_path, _FIRST_TICKER).state is ReportRunState.RUNNING
        assert _host_view(harness.admin, snapshots[_FIRST_TICKER]).cancel_requested_at is None
    finally:
        for pipeline in harness.pipelines.values():
            pipeline.release.set()
        terminals = {ticker: _wait_terminal(tmp_path, ticker) for ticker in started}
    assert terminals[_FIRST_TICKER].state is ReportRunState.SUCCEEDED
    assert terminals[_SECOND_TICKER].state is ReportRunState.CANCELLED
    assert harness.recorder.exit_codes == {
        _FIRST_TICKER: _SUCCESS_EXIT_CODE,
        _SECOND_TICKER: WRITE_CANCELLED_EXIT_CODE,
    }
    for ticker, terminal in terminals.items():
        assert terminal.run_id == snapshots[ticker].run_id
        assert _host_view(harness.admin, terminal).state == terminal.state.value
        assert load_report_run(tmp_path, ticker) == terminal
