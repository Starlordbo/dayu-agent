"""报告后台执行适配：磁盘记录是真源，文件锁串行化启动与状态变更。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
import logging
import os
from pathlib import Path
from threading import Thread
from uuid import uuid4

from dayu.file_lock import acquire_text_file_lock, is_lock_contention_error, release_text_file_lock
from dayu.process_liveness import is_pid_alive
from dayu.services.contracts import WriteRequest
from dayu.services.protocols import WriteServiceProtocol
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
    if run is None or run.state != ReportRunState.RUNNING or is_pid_alive(run.pid):
        return run
    with _report_lock(path, blocking=False) as acquired:
        # 加锁前的快照可能已被另一个页面恢复并替换，不能覆盖新任务。
        current = read_report_run(path)
        if not acquired or current is None or current.state != ReportRunState.RUNNING:
            return current
        if is_pid_alive(current.pid):
            return current
        stale = replace(
            current, state=ReportRunState.STALE,
            updated_at=datetime.now(timezone.utc).isoformat(), error=_INTERRUPTED_ERROR,
        )
        write_report_run(path, stale)
        return stale


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


def _finish_report_run(path: Path, initial: ReportRun, *, error: str | None) -> None:
    """在同一互斥边界提交终态，仅更新仍属于该 worker 的记录。

    参数:
        path: 状态文件路径。
        initial: 启动时预留的运行态，作为任务归属校验。
        error: 失败原因；为空表示成功。
    返回值:
        无。
    异常:
        ValueError: 当前磁盘记录非法。
        OSError: 状态无法落盘。
    """
    with _report_lock(path, blocking=True):
        if read_report_run(path) != initial:
            return
        finished = replace(
            initial,
            state=ReportRunState.SUCCEEDED if error is None else ReportRunState.FAILED,
            updated_at=datetime.now(timezone.utc).isoformat(), error=error,
        )
        try:
            _append_run_log(finished)
        except OSError as exc:
            # 日志失败不能让已退出的 worker 永久残留 running。
            finished = replace(
                finished, state=ReportRunState.FAILED,
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
    try:
        exit_code = write_service.run(request)
        if exit_code != _SUCCESS_EXIT_CODE:
            error = f"写作服务返回非零退出码：{exit_code}"
    except BaseException as exc:
        # SystemExit 等同样只会结束当前线程，必须落失败，不能留下假运行态。
        error = f"{type(exc).__name__}: {exc}"
    try:
        _finish_report_run(path, initial, error=error)
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
        if current is not None and current.state == ReportRunState.RUNNING and is_pid_alive(current.pid):
            return False
        now = datetime.now(timezone.utc).isoformat()
        initial = ReportRun(
            state=ReportRunState.RUNNING, pid=os.getpid(), started_at=now, updated_at=now,
            output_dir=request.write_config.output_dir, error=None,
            log_path=str(path.with_name(f"{ticker}.{uuid4().hex}{_LOG_SUFFIX}")),
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
