"""报告后台执行适配：磁盘记录是真源，文件锁串行化启动与状态变更。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import logging
import os
from pathlib import Path
from threading import Thread
from uuid import uuid4

from dayu.file_lock import acquire_text_file_lock, is_lock_contention_error, release_text_file_lock
from dayu.process_liveness import is_pid_alive
from dayu.services.contracts import WriteRequest
from dayu.services.protocols import HostAdminServiceProtocol, WriteServiceProtocol
from dayu.services.write_service import WRITE_CANCELLED_EXIT_CODE
from dayu.web.streamlit.report_runs import (
    ReportRun,
    ReportRunState,
    normalize_report_ticker,
    read_report_run,
    report_run_path,
    write_report_run,
)

_LOCK_SUFFIX = ".lock"
_LOG_SUFFIX = ".log"
_LOCK_NAME = "报告任务状态锁"
_SUCCESS_EXIT_CODE = 0
_INTERRUPTED_ERROR = "上次运行已中断，可重试。"
_LOGGER = logging.getLogger(__name__)


@dataclass
class _ReportTaskContext:
    """单个 worker 的明确任务归属及瞬时登记，不能跨线程共享。"""

    path: Path
    initial: ReportRun
    active_run_id: str | None = None


_REPORT_TASK: ContextVar[_ReportTaskContext | None] = ContextVar("report_task", default=None)


def _same_report_task(current: ReportRun | None, initial: ReportRun) -> bool:
    """用启动时固定身份核对任务，允许 run_id 与取消状态独立更新。

    参数:
        current: 当前磁盘快照。
        initial: 调用方持有的任务快照。
    返回值:
        启动时间、进程及唯一日志路径均一致时为真。
    异常:
        无。
    """
    return current is not None and (
        current.pid, current.started_at, current.log_path
    ) == (initial.pid, initial.started_at, initial.log_path)


def _submit_report_cancellation(
    path: Path, current: ReportRun, host_admin_service: HostAdminServiceProtocol,
) -> ReportRun:
    """在报告锁内向 Service 提交已持久化意图，失败后允许页面重试。

    参数:
        path: 已锁定的状态文件。
        current: 已持久化的 cancelling 快照。
        host_admin_service: Host 取消操作的 Service 边界。
    返回值:
        提交后的快照；尚无身份时保留意图，失败时恢复 running 并记录原因。
    异常:
        OSError: 无法写入失败状态。
        ValueError: 状态不符合磁盘契约。
    """
    if current.run_id is None:
        return current
    try:
        # cancel_run 只提交意图，不等待 worker；持锁提交关闭跨页面重复请求窗口。
        host_admin_service.cancel_run(current.run_id)
    except Exception as exc:
        current = replace(
            current, state=ReportRunState.RUNNING,
            updated_at=datetime.now(timezone.utc).isoformat(),
            error=f"取消请求失败：{type(exc).__name__}: {exc}",
        )
        write_report_run(path, current)
    return current


class ReportRunLifecycleObserver:
    """把同步 Service 回调绑定到 worker 上下文，按 ticker 持久化 Host 身份。"""

    def __init__(self, host_admin_service: HostAdminServiceProtocol) -> None:
        """保存取消入口；任务身份由每个 worker 独立绑定。

        参数:
            host_admin_service: 已装配的 Host 管理服务。
        返回值:
            无。
        异常:
            无。
        """
        self._host_admin_service = host_admin_service

    def register_active_run(self, run_id: str) -> None:
        """登记 Host 身份，立即消费该任务在登记前提交的取消意图。

        参数:
            run_id: 当前同步写作服务的 Host 运行身份。
        返回值:
            无。
        异常:
            RuntimeError: 未在报告 worker 上下文调用或任务归属已失效。
            ValueError: 身份或磁盘状态非法。
            OSError: 状态文件或锁操作失败。
        """
        task = _REPORT_TASK.get()
        if task is None:
            raise RuntimeError("报告生命周期回调缺少 worker 上下文")
        with _report_lock(task.path, blocking=True):
            current = read_report_run(task.path)
            if current is None or not _same_report_task(current, task.initial) or not current.state.is_active:
                raise RuntimeError("报告生命周期回调的任务归属已失效")
            if current.run_id is not None and current.run_id != run_id:
                raise RuntimeError("报告任务不能登记不同的 Host run_id")
            registered = replace(current, run_id=run_id, updated_at=datetime.now(timezone.utc).isoformat())
            write_report_run(task.path, registered)
            task.active_run_id = run_id
            if registered.state is ReportRunState.CANCELLING:
                _submit_report_cancellation(task.path, registered, self._host_admin_service)

    def clear_active_run(self, run_id: str) -> None:
        """清除瞬时登记，保留磁盘身份直至 worker 按 Host 退出契约落终态。

        参数:
            run_id: 当前服务释放的 Host 运行身份。
        返回值:
            无。
        异常:
            RuntimeError: 回调上下文缺失或释放了其他任务的身份。
        """
        task = _REPORT_TASK.get()
        if task is None or task.active_run_id != run_id:
            raise RuntimeError("报告生命周期清理的上下文或 run_id 不匹配")
        task.active_run_id = None


@contextmanager
def _report_lock(path: Path, *, blocking: bool) -> Iterator[bool]:
    """锁住固定旁路文件，避免原子替换 JSON 后丢失互斥关系。

    参数:
        path: 状态文件路径，父目录须已存在。
        blocking: worker 收尾可等待短临界区，UI 加锁始终不等待。
    返回值:
        上下文中是否取得锁；非阻塞竞争失败时为假。
    异常:
        OSError: 非锁竞争的文件系统错误。
    """
    # 锁文件不能删除，否则不同调用者可能锁住不同 inode。
    with path.with_suffix(_LOCK_SUFFIX).open("a+", encoding="utf-8") as stream:
        try:
            acquire_text_file_lock(stream, blocking=blocking, lock_name=_LOCK_NAME)
        except OSError as exc:
            if blocking or not is_lock_contention_error(exc):
                raise
            yield False
            return
        try:
            yield True
        finally:
            release_text_file_lock(stream, lock_name=_LOCK_NAME)


def load_report_run(workspace_root: Path, ticker: str) -> ReportRun | None:
    """从磁盘恢复报告状态，并在再次核对后将死进程记录标记为中断。

    参数:
        workspace_root: 工作区根目录。
        ticker: 股票代码，按去除首尾空格后的大写隔离。
    返回值:
        当前持久记录；首次访问尚无记录时为空。
    异常:
        ValueError: 股票代码或状态文件内容非法。
        OSError: 文件系统或进程存活检查失败。
    """
    path = report_run_path(workspace_root, ticker)
    run = read_report_run(path)
    if run is None or not run.state.is_active or is_pid_alive(run.pid):
        return run
    with _report_lock(path, blocking=False) as acquired:
        # 加锁前的快照可能已被另一个页面恢复并替换，不能覆盖新任务。
        current = read_report_run(path)
        if not acquired or current is None or not current.state.is_active:
            return current
        if is_pid_alive(current.pid):
            return current
        stale = replace(
            current, state=ReportRunState.STALE,
            updated_at=datetime.now(timezone.utc).isoformat(), error=_INTERRUPTED_ERROR,
        )
        write_report_run(path, stale)
        return stale


def cancel_report_run(
    *, workspace_root: Path, ticker: str, host_admin_service: HostAdminServiceProtocol,
    expected_run: ReportRun,
) -> ReportRun | None:
    """幂等提交当前页面所见任务的取消，身份未出现时先持久保存意图。

    参数:
        workspace_root: 工作区根目录。
        ticker: 当前股票代码。
        host_admin_service: 已装配的 Host 管理服务。
        expected_run: 点击前页面所见任务，用于拒绝迟到按钮取消后续任务。
    返回值:
        当前磁盘快照；不存在时为空，终态或其他任务保持不变。
    异常:
        RuntimeError: 状态锁正在使用，请稍后重试。
        ValueError: 股票代码或磁盘状态非法。
        OSError: 文件、锁或进程探测失败。
    """
    if load_report_run(workspace_root, ticker) is None:
        return None
    path = report_run_path(workspace_root, ticker)
    with _report_lock(path, blocking=False) as acquired:
        if not acquired:
            raise RuntimeError("报告状态正在更新，请重试取消。")
        current = read_report_run(path)
        if current is None or not _same_report_task(current, expected_run) or current.state is not ReportRunState.RUNNING:
            return current
        requested = replace(
            current, state=ReportRunState.CANCELLING, error=None,
            updated_at=datetime.now(timezone.utc).isoformat(),
        )
        write_report_run(path, requested)
        return _submit_report_cancellation(path, requested, host_admin_service)


def _append_run_log(run: ReportRun) -> None:
    """记录任务生命周期与错误，不重定向进程共享的 stdout/stderr。

    参数:
        run: 本次要记录的任务状态。
    返回值:
        无。
    异常:
        OSError: 日志无法创建或写入。
    """
    with Path(run.log_path).open("a", encoding="utf-8") as stream:
        stream.write(f"{run.updated_at} {run.state.value} pid={run.pid} {run.error or ''}\n")


def _finish_report_run(path: Path, initial: ReportRun, *, state: ReportRunState, error: str | None) -> None:
    """在同一互斥边界提交终态，仅更新仍属于该 worker 的记录。

    参数:
        path: 状态文件路径。
        initial: 启动时预留的运行态，作为任务归属校验。
        state: Service 退出码或异常确定的最终状态。
        error: 失败原因；成功或取消时为空。
    返回值:
        无。
    异常:
        ValueError: 当前磁盘记录非法。
        OSError: 状态无法落盘。
    """
    with _report_lock(path, blocking=True):
        current = read_report_run(path)
        if current is None or not _same_report_task(current, initial) or not current.state.is_active:
            return
        finished = replace(
            current,
            state=state,
            updated_at=datetime.now(timezone.utc).isoformat(), error=error,
        )
        try:
            _append_run_log(finished)
        except OSError as exc:
            # 日志失败不能让已退出的 worker 永久残留 running。
            finished = replace(
                finished, state=state if state is ReportRunState.CANCELLED else ReportRunState.FAILED,
                error=f"{error + '；' if error else ''}任务日志写入失败：{exc}",
            )
        write_report_run(path, finished)


def _run_report_worker(
    path: Path, initial: ReportRun, write_service: WriteServiceProtocol, request: WriteRequest,
) -> None:
    """在独立线程调用同步 Service，并将退出码或异常收口为持久终态。

    参数:
        path: 状态文件路径。
        initial: 启动前持久化的运行态。
        write_service: 唯一业务执行入口。
        request: 已归一化股票代码的写作请求。
    返回值:
        无。
    异常:
        无；服务异常转成失败，无法持久化时记录服务端错误日志。
    """
    error: str | None = None
    state = ReportRunState.SUCCEEDED
    # Host 同步入口直接调用 operation；在 worker 内绑定，不能依赖新线程继承上下文。
    token = _REPORT_TASK.set(_ReportTaskContext(path=path, initial=initial))
    try:
        exit_code = write_service.run(request)
        if exit_code == WRITE_CANCELLED_EXIT_CODE:
            state = ReportRunState.CANCELLED
        elif exit_code != _SUCCESS_EXIT_CODE:
            state = ReportRunState.FAILED
            error = f"写作服务返回非零退出码：{exit_code}"
    except BaseException as exc:
        # SystemExit 等同样只会结束当前线程，必须落失败，不能留下假运行态。
        error = f"{type(exc).__name__}: {exc}"
        state = ReportRunState.FAILED
    finally:
        _REPORT_TASK.reset(token)
    try:
        _finish_report_run(path, initial, state=state, error=error)
    except (OSError, ValueError):
        _LOGGER.exception("报告任务终态无法持久化：%s", path)


def start_report_run(
    *, workspace_root: Path, write_service: WriteServiceProtocol, request: WriteRequest,
) -> bool:
    """非阻塞地排他启动报告任务，跨页面和进程共用磁盘状态。

    参数:
        workspace_root: 工作区根目录。
        write_service: 同步写作服务协议实例。
        request: 表单构造的写作请求。
    返回值:
        成功预留并启动线程时为真；锁竞争或已有存活任务时为假。
    异常:
        ValueError: 股票代码、输出目录或已有状态非法。
        OSError: 文件、日志或锁操作失败。
        RuntimeError: 操作系统拒绝启动线程，已尽可能落盘失败状态。
    """
    ticker = normalize_report_ticker(request.write_config.ticker)
    if not request.write_config.output_dir.strip():
        raise ValueError("报告输出目录不能为空")
    request = replace(request, write_config=replace(request.write_config, ticker=ticker))
    path = report_run_path(workspace_root, ticker)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _report_lock(path, blocking=False) as acquired:
        if not acquired:
            return False
        current = read_report_run(path)
        if current is not None and current.state.is_active and is_pid_alive(current.pid):
            return False
        now = datetime.now(timezone.utc).isoformat()
        initial = ReportRun(
            state=ReportRunState.RUNNING, pid=os.getpid(), started_at=now, updated_at=now,
            output_dir=request.write_config.output_dir, error=None,
            log_path=str(path.with_name(f"{ticker}.{uuid4().hex}{_LOG_SUFFIX}")),
            run_id=None,
        )
        _append_run_log(initial)
        # 检查、预留与线程启动必须处于同一临界区，关闭双标签页重复启动窗口。
        write_report_run(path, initial)
        try:
            worker = Thread(
                target=_run_report_worker, args=(path, initial, write_service, request),
                name=f"report-{ticker}", daemon=True,
            )
            worker.start()
        except RuntimeError as exc:
            failed = replace(
                initial, state=ReportRunState.FAILED,
                updated_at=datetime.now(timezone.utc).isoformat(), error=f"线程启动失败：{exc}",
            )
            try:
                _append_run_log(failed)
            finally:
                write_report_run(path, failed)
            raise
    return True
