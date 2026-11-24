"""报告后台线程和磁盘状态测试，全部使用受事件控制的假写作服务。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import datetime
import errno
import json
import os
from pathlib import Path
import sys
from threading import Barrier, Event, Thread, get_ident
from time import monotonic
from unittest.mock import patch

import pytest

from dayu.file_lock import acquire_text_file_lock
from dayu.services.contracts import WriteRequest, WriteRunConfig
from dayu.web.streamlit import report_runtime
from dayu.web.streamlit.report_runs import ReportRun, ReportRunState
from dayu.web.streamlit.report_runtime import load_report_run, start_report_run


_WAIT_SECONDS = 5.0
_POLL_SECONDS = 0.01
_TICKER = "AAPL"
_STARTED_AT = "2026-11-24T12:00:00+00:00"


@dataclass
class _ControlledWriteService:
    """阻塞到测试释放后返回退出码或抛异常的假写作服务。"""

    exit_code: int = 0
    failure: BaseException | None = None
    entered: Event = field(default_factory=Event)
    release: Event = field(default_factory=Event)
    requests: list[WriteRequest] = field(default_factory=list)
    worker_ident: int | None = None

    def run(self, request: WriteRequest) -> int:
        """执行受控假任务。

        Args:
            request: 需要记录的写作请求。
        Returns:
            预设退出码。
        Raises:
            AssertionError: 测试未及时释放任务。
            BaseException: 预设失败。
        """
        self.requests.append(request)
        self.worker_ident = get_ident()
        self.entered.set()
        assert self.release.wait(_WAIT_SECONDS)
        if self.failure is not None:
            raise self.failure
        return self.exit_code

    @staticmethod
    def print_report(output_dir: str) -> int:
        """阻止测试误走报告打印路径。

        Args:
            output_dir: 未使用的报告目录。
        Returns:
            不返回。
        Raises:
            AssertionError: 调用即失败。
        """
        raise AssertionError(f"不应打印报告：{output_dir}")


def _request(workspace: Path, ticker: str = _TICKER) -> WriteRequest:
    """构造独立工作区的写作请求。

    Args:
        workspace: 临时工作区。
        ticker: 测试股票代码。
    Returns:
        不会调用真实流水线的请求。
    Raises:
        无。
    """
    return WriteRequest(WriteRunConfig(
        ticker=ticker, company="测试公司", template_path=str(workspace / "template.md"),
        output_dir=str(workspace / "draft" / ticker.strip().upper()),
        write_max_retries=1, web_provider="default", resume=True,
    ))


def _state_path(workspace: Path, ticker: str = _TICKER) -> Path:
    """定位任务要求的状态文件。

    Args:
        workspace: 临时工作区。
        ticker: 股票代码。
    Returns:
        状态文件路径。
    Raises:
        无。
    """
    return workspace / ".dayu" / "streamlit" / "report_runs" / f"{ticker}.json"


def _payload(workspace: Path, *, pid: int | None = None) -> dict[str, str | int | None]:
    """生成可直接写盘的合法运行态。

    Args:
        workspace: 临时工作区。
        pid: 运行进程；缺省使用当前进程。
    Returns:
        完整状态载荷。
    Raises:
        无。
    """
    return {
        "state": "running", "pid": os.getpid() if pid is None else pid,
        "started_at": _STARTED_AT, "updated_at": _STARTED_AT,
        "output_dir": str(workspace / "draft" / _TICKER), "error": None,
        "log_path": str(workspace / "report.log"),
        "run_id": None,
    }


def _write_payload(workspace: Path, payload: dict[str, str | int | None]) -> None:
    """写入测试状态文件。

    Args:
        workspace: 临时工作区。
        payload: 状态载荷。
    Returns:
        无。
    Raises:
        OSError: 文件系统写入失败。
    """
    path = _state_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _wait_terminal(workspace: Path, ticker: str = _TICKER) -> ReportRun:
    """在有限时间内等待后台线程落终态。

    Args:
        workspace: 临时工作区。
        ticker: 股票代码。
    Returns:
        已持久化的终态。
    Raises:
        AssertionError: 后台任务未及时结束。
    """
    deadline = monotonic() + _WAIT_SECONDS
    while monotonic() < deadline:
        run = load_report_run(workspace, ticker)
        if run is not None and not run.state.is_active:
            return run
        Event().wait(_POLL_SECONDS)
    raise AssertionError("后台任务未落终态")


def test_missing_state_is_absent_without_creating_files(tmp_path: Path) -> None:
    """验证首次读取没有副作用。

    Args:
        tmp_path: 临时工作区。
    Returns:
        无。
    Raises:
        AssertionError: 结果不符合预期。
    """
    assert load_report_run(tmp_path, _TICKER) is None
    assert not _state_path(tmp_path).parent.exists()


def test_worker_runs_off_main_thread_and_persists_success(tmp_path: Path) -> None:
    """验证后台执行、恢复、归一化、防重复与成功日志。

    Args:
        tmp_path: 临时工作区。
    Returns:
        无。
    Raises:
        AssertionError: 生命周期或持久化结果错误。
    """
    service = _ControlledWriteService()
    request = _request(tmp_path, " aapl ")
    stdout, stderr = sys.stdout, sys.stderr
    try:
        with patch("dayu.web.streamlit.report_runs.os.replace", wraps=os.replace) as atomic_replace:
            assert start_report_run(workspace_root=tmp_path, write_service=service, request=request)
            assert service.entered.wait(_WAIT_SECONDS)
            assert atomic_replace.called
        run = load_report_run(tmp_path, "aapl")
        assert run is not None and run.state is ReportRunState.RUNNING
        assert run.pid == os.getpid()
        assert datetime.fromisoformat(run.started_at).utcoffset() is not None
        assert run.output_dir == request.write_config.output_dir
        assert service.worker_ident != get_ident()
        assert service.requests[0].write_config.ticker == _TICKER
        assert sys.stdout is stdout and sys.stderr is stderr
        assert not start_report_run(workspace_root=tmp_path, write_service=service, request=request)
        assert len(service.requests) == 1
        assert set(json.loads(_state_path(tmp_path).read_text(encoding="utf-8"))) == {
            "state", "pid", "started_at", "updated_at", "output_dir", "error", "log_path", "run_id",
        }
    finally:
        service.release.set()
    completed = _wait_terminal(tmp_path)
    assert completed.state is ReportRunState.SUCCEEDED
    assert completed.error is None
    assert "succeeded" in Path(completed.log_path).read_text(encoding="utf-8")
    assert not list(_state_path(tmp_path).parent.glob("*.tmp"))


@pytest.mark.parametrize("exit_code,failure,expected", [
    (7, None, "7"), (0, RuntimeError("模拟服务失败"), "模拟服务失败"),
    (0, SystemExit("线程退出"), "线程退出"),
])
def test_failure_is_persisted_with_error_and_log(
    tmp_path: Path, exit_code: int, failure: BaseException | None, expected: str,
) -> None:
    """验证非零退出码和异常都变成可重试失败。

    Args:
        tmp_path: 临时工作区。
        exit_code: 假服务退出码。
        failure: 假服务异常。
        expected: 错误与日志应包含的文本。
    Returns:
        无。
    Raises:
        AssertionError: 失败状态不完整。
    """
    service = _ControlledWriteService(exit_code=exit_code, failure=failure)
    service.release.set()
    assert start_report_run(workspace_root=tmp_path, write_service=service, request=_request(tmp_path))
    run = _wait_terminal(tmp_path)
    assert run.state is ReportRunState.FAILED
    assert run.error is not None and expected in run.error
    assert expected in Path(run.log_path).read_text(encoding="utf-8")
    retry = _ControlledWriteService()
    retry.release.set()
    assert start_report_run(workspace_root=tmp_path, write_service=retry, request=_request(tmp_path))
    assert _wait_terminal(tmp_path).state is ReportRunState.SUCCEEDED


@dataclass
class _Contender:
    """让多个调用者同时抢占同一个 ticker。"""

    workspace: Path
    service: _ControlledWriteService
    barrier: Barrier

    def __call__(self, index: int) -> bool:
        """同步后启动任务。

        Args:
            index: 并发调用序号。
        Returns:
            是否赢得任务启动权。
        Raises:
            BrokenBarrierError: 调用者未及时到齐。
        """
        self.barrier.wait(_WAIT_SECONDS)
        return start_report_run(workspace_root=self.workspace, write_service=self.service, request=_request(self.workspace))


def test_concurrent_start_only_runs_once_and_other_ticker_is_independent(tmp_path: Path) -> None:
    """验证跨调用互斥与 ticker 隔离。

    Args:
        tmp_path: 临时工作区。
    Returns:
        无。
    Raises:
        AssertionError: 并发任务重复启动或错误互斥。
    """
    contenders = 8
    service = _ControlledWriteService()
    other = _ControlledWriteService()
    try:
        with ThreadPoolExecutor(max_workers=contenders) as executor:
            results = list(executor.map(_Contender(tmp_path, service, Barrier(contenders)), range(contenders)))
        assert results.count(True) == 1
        assert service.entered.wait(_WAIT_SECONDS)
        assert len(service.requests) == 1
        assert start_report_run(workspace_root=tmp_path, write_service=other, request=_request(tmp_path, "MSFT"))
        assert other.entered.wait(_WAIT_SECONDS)
    finally:
        service.release.set()
        other.release.set()
    assert _wait_terminal(tmp_path).state is ReportRunState.SUCCEEDED
    assert _wait_terminal(tmp_path, "MSFT").state is ReportRunState.SUCCEEDED


def test_dead_pid_becomes_persisted_stale_and_can_retry(tmp_path: Path) -> None:
    """验证进程消失后恢复为可重试中断状态。

    Args:
        tmp_path: 临时工作区。
    Returns:
        无。
    Raises:
        AssertionError: 中断未被持久化或重试失败。
    """
    _write_payload(tmp_path, _payload(tmp_path))
    with patch("dayu.process_liveness.os.kill", side_effect=ProcessLookupError) as kill:
        run = load_report_run(tmp_path, _TICKER)
        kill.assert_called_with(os.getpid(), 0)
    assert run is not None and run.state is ReportRunState.STALE
    assert run.error is not None and "中断" in run.error
    assert load_report_run(tmp_path, _TICKER) == run
    service = _ControlledWriteService()
    service.release.set()
    assert start_report_run(workspace_root=tmp_path, write_service=service, request=_request(tmp_path))
    assert _wait_terminal(tmp_path).state is ReportRunState.SUCCEEDED


@dataclass
class _ReplacingLivenessProbe:
    """在首次判活中模拟另一个调用者已恢复并启动新任务。"""

    workspace: Path
    calls: int = 0

    def __call__(self, pid: int) -> bool:
        """首次替换任务，后续认定新进程存活。

        Args:
            pid: 被查询的进程 ID。
        Returns:
            首次为假，其后为真。
        Raises:
            OSError: 无法写入模拟任务。
        """
        self.calls += 1
        if self.calls == 1:
            replacement = _payload(self.workspace)
            replacement["started_at"] = "2026-11-24T13:00:00+00:00"
            _write_payload(self.workspace, replacement)
            return False
        return True


def test_stale_recovery_rereads_under_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """验证过期恢复快照不会覆盖已启动的新任务。

    Args:
        tmp_path: 临时工作区。
        monkeypatch: 判活依赖替换工具。
    Returns:
        无。
    Raises:
        AssertionError: 新任务被旧快照覆盖。
    """
    _write_payload(tmp_path, _payload(tmp_path))
    probe = _ReplacingLivenessProbe(tmp_path)
    monkeypatch.setattr(report_runtime, "is_pid_alive", probe)
    run = load_report_run(tmp_path, _TICKER)
    assert run is not None and run.state is ReportRunState.RUNNING
    assert run.started_at == "2026-11-24T13:00:00+00:00"


class _BrokenThread(Thread):
    """模拟操作系统拒绝启动后台线程。"""

    def start(self) -> None:
        """拒绝启动。

        Args:
            无。
        Returns:
            不返回。
        Raises:
            RuntimeError: 模拟线程启动失败。
        """
        raise RuntimeError("线程启动失败")


def test_thread_start_failure_releases_reservation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """验证线程启动异常会记录失败并释放排他锁。

    Args:
        tmp_path: 临时工作区。
        monkeypatch: 线程工厂替换工具。
    Returns:
        无。
    Raises:
        AssertionError: 失败后仍残留运行态。
    """
    service = _ControlledWriteService()
    with monkeypatch.context() as scoped:
        scoped.setattr(report_runtime, "Thread", _BrokenThread)
        with pytest.raises(RuntimeError, match="线程启动失败"):
            start_report_run(workspace_root=tmp_path, write_service=service, request=_request(tmp_path))
    run = load_report_run(tmp_path, _TICKER)
    assert run is not None and run.state is ReportRunState.FAILED
    assert run.error is not None and "线程启动失败" in run.error
    assert "线程启动失败" in Path(run.log_path).read_text(encoding="utf-8")
    assert service.requests == []
    service.release.set()
    assert start_report_run(workspace_root=tmp_path, write_service=service, request=_request(tmp_path))
    assert _wait_terminal(tmp_path).state is ReportRunState.SUCCEEDED


@pytest.mark.parametrize("field_name,invalid", [
    ("state", "unknown"), ("pid", 0), ("pid", "123"),
    ("started_at", "2026-11-24T12:00:00"), ("updated_at", "broken"),
    ("output_dir", ""), ("log_path", None), ("error", 7),
])
def test_invalid_state_is_explicit_and_never_overwritten(
    tmp_path: Path, field_name: str, invalid: str | int | None,
) -> None:
    """验证损坏状态不能被默认成可启动状态。

    Args:
        tmp_path: 临时工作区。
        field_name: 待损坏字段。
        invalid: 非法字段值。
    Returns:
        无。
    Raises:
        AssertionError: 损坏状态被覆盖或服务被调用。
    """
    payload = _payload(tmp_path)
    payload[field_name] = invalid
    _write_payload(tmp_path, payload)
    original = _state_path(tmp_path).read_bytes()
    service = _ControlledWriteService()
    with pytest.raises(ValueError):
        load_report_run(tmp_path, _TICKER)
    with pytest.raises(ValueError):
        start_report_run(workspace_root=tmp_path, write_service=service, request=_request(tmp_path))
    assert _state_path(tmp_path).read_bytes() == original
    assert service.requests == []


@pytest.mark.parametrize("raw", ["{broken", "[]", "{}"])
def test_malformed_json_remains_visible(tmp_path: Path, raw: str) -> None:
    """验证 JSON 语法及结构错误显式报告。

    Args:
        tmp_path: 临时工作区。
        raw: 损坏的 JSON 内容。
    Returns:
        无。
    Raises:
        AssertionError: 损坏内容被忽略。
    """
    path = _state_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(raw, encoding="utf-8")
    with pytest.raises(ValueError):
        load_report_run(tmp_path, _TICKER)


@pytest.mark.parametrize("ticker", ["", "../AAPL", "AAPL/OTHER", "/tmp", "..", "A A", "A\\B"])
def test_invalid_ticker_cannot_escape_state_directory(tmp_path: Path, ticker: str) -> None:
    """验证股票代码无法形成目录穿越。

    Args:
        tmp_path: 临时工作区。
        ticker: 非法股票代码。
    Returns:
        无。
    Raises:
        AssertionError: 非法路径被接受。
    """
    service = _ControlledWriteService()
    with pytest.raises(ValueError):
        load_report_run(tmp_path, ticker)
    with pytest.raises(ValueError):
        start_report_run(workspace_root=tmp_path, write_service=service, request=_request(tmp_path, ticker))
    assert service.requests == []


def test_lock_contention_returns_without_starting_worker(tmp_path: Path) -> None:
    """验证独立文件句柄抢锁失败不会阻塞或启动服务。

    Args:
        tmp_path: 临时工作区。
    Returns:
        无。
    Raises:
        AssertionError: 锁竞争未被正确拒绝。
    """
    path = _state_path(tmp_path).with_suffix(".lock")
    path.parent.mkdir(parents=True)
    service = _ControlledWriteService()
    with path.open("a", encoding="utf-8") as handle:
        acquire_text_file_lock(handle, blocking=False, lock_name="测试报告锁")
        assert not start_report_run(workspace_root=tmp_path, write_service=service, request=_request(tmp_path))
    assert path.exists()
    assert not _state_path(tmp_path).exists()
    assert service.requests == []


def test_late_worker_cannot_overwrite_replacement_run(tmp_path: Path) -> None:
    """旧 worker 收尾不能覆盖同路径上已经替换的新任务。

    参数:
        tmp_path: 独立工作区。
    返回值:
        无。
    异常:
        AssertionError: 新任务记录被旧 worker 改写。
    """
    _write_payload(tmp_path, _payload(tmp_path))
    original = load_report_run(tmp_path, _TICKER)
    assert original is not None
    replacement = replace(original, started_at="2026-11-24T13:00:00+00:00")
    report_runtime.write_report_run(_state_path(tmp_path), replacement)
    report_runtime._finish_report_run(_state_path(tmp_path), original, state=ReportRunState.SUCCEEDED, error=None)
    assert load_report_run(tmp_path, _TICKER) == replacement


def test_log_write_failure_still_persists_terminal_error(tmp_path: Path) -> None:
    """任务执行后的日志写入失败必须落失败态，不能残留 running。

    参数:
        tmp_path: 独立工作区。
    返回值:
        无。
    异常:
        AssertionError: 日志错误吞掉终态。
    """
    service = _ControlledWriteService()
    assert start_report_run(workspace_root=tmp_path, write_service=service, request=_request(tmp_path))
    assert service.entered.wait(_WAIT_SECONDS)
    run = load_report_run(tmp_path, _TICKER)
    assert run is not None
    # 用目录替换日志路径，只让日志写入失败，保留状态目录可写。
    Path(run.log_path).unlink()
    Path(run.log_path).mkdir()
    service.release.set()
    terminal = _wait_terminal(tmp_path)
    assert terminal.state == ReportRunState.FAILED
    assert terminal.error is not None and "日志写入失败" in terminal.error


def test_lock_io_error_is_not_misreported_as_duplicate(tmp_path: Path) -> None:
    """文件锁 I/O 错误必须显式失败，不能伪装成已有任务。

    参数:
        tmp_path: 独立工作区。
    返回值:
        无。
    异常:
        AssertionError: I/O 错误被忽略或服务被调用。
    """
    service = _ControlledWriteService()
    with patch.object(report_runtime, "acquire_text_file_lock", side_effect=OSError(errno.EIO, "锁 I/O 失败")):
        with pytest.raises(OSError, match="锁 I/O 失败"):
            start_report_run(workspace_root=tmp_path, write_service=service, request=_request(tmp_path))
    assert service.requests == []
