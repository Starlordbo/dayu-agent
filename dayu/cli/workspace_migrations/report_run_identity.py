"""将旧报告运行态迁移为包含 Host run_id 的严格新 schema。

旧 schema 仅在 init 迁移入口识别。迁移与报告 worker 共用旁路文件锁，
拒绝改写仍由旧进程持有的任务，并保留已有报告文件与生命周期日志。
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import cast

from dayu.contracts.agent_types import JsonValue
from dayu.file_lock import acquire_text_file_lock, is_lock_contention_error, release_text_file_lock
from dayu.process_liveness import is_pid_alive
from dayu.web.streamlit.report_runs import (
    ReportRun,
    ReportRunState,
    read_report_run,
    report_run_directory,
    write_report_run,
)


_TEXT_ENCODING = "utf-8"
_STATE_GLOB = "*.json"
_LOCK_SUFFIX = ".lock"
_LOCK_NAME = "报告任务状态锁"
_RUN_ID_FIELD = "run_id"
_LEGACY_FIELDS = frozenset({
    "state", "pid", "started_at", "updated_at", "output_dir", "error", "log_path",
})
_LEGACY_STATES = frozenset({
    ReportRunState.RUNNING, ReportRunState.SUCCEEDED, ReportRunState.FAILED, ReportRunState.STALE,
})
_INTERRUPTED_ERROR = "上次运行已中断，可重试。"


def _required_text(payload: dict[str, JsonValue], field_name: str) -> str:
    """读取旧 schema 的必填文本，完整语义交由新 schema 写入校验。

    参数:
        payload: 字段集合已确认完整的旧状态。
        field_name: 待读取的文本字段。
    返回值:
        非空文本字段值。
    异常:
        ValueError: 字段不是文本或为空。
        KeyError: 调用方提供了未知字段名。
    """
    value = payload[field_name]
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"旧报告任务状态字段 {field_name} 必须是非空文本")
    return value


def _required_timestamp(payload: dict[str, JsonValue], field_name: str) -> str:
    """校验旧时间字段，避免 stale 转换覆盖原有的损坏数据。

    参数:
        payload: 字段集合完整的旧状态。
        field_name: 待读取的时间字段。
    返回值:
        带时区的原始 ISO 时间文本。
    异常:
        ValueError: 时间字段非法、为空或不含时区。
        KeyError: 调用方提供了未知字段名。
    """
    value = _required_text(payload, field_name)
    if datetime.fromisoformat(value).utcoffset() is None:
        raise ValueError(f"旧报告任务状态字段 {field_name} 必须包含时区")
    return value


def _convert_legacy_report(payload: dict[str, JsonValue], path: Path) -> ReportRun:
    """将完整旧快照投影到新 schema，并处理旧 worker 存活状态。

    参数:
        payload: 尚不含 run_id 的旧状态对象。
        path: 用于定位拒绝原因的状态文件路径。
    返回值:
        保留历史字段且 run_id 为空的新快照；死进程运行态转为 stale。
    异常:
        ValueError: 旧字段集合、类型或状态值不合法。
        RuntimeError: 旧报告 worker 尚存活，必须停止后重试。
        OSError: 进程存活检查失败。
    """
    if payload.keys() != _LEGACY_FIELDS:
        raise ValueError(f"旧报告任务状态字段不完整或包含未知字段：{path}")
    state = ReportRunState(_required_text(payload, "state"))
    if state not in _LEGACY_STATES:
        raise ValueError(f"旧报告任务状态不支持 {state.value}：{path}")
    pid = payload["pid"]
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        raise ValueError(f"旧报告任务状态 pid 必须是正整数：{path}")
    error = payload["error"]
    if error is not None and not isinstance(error, str):
        raise ValueError(f"旧报告任务状态 error 必须是文本或 null：{path}")
    started_at = _required_timestamp(payload, "started_at")
    updated_at = _required_timestamp(payload, "updated_at")
    if state is ReportRunState.RUNNING:
        if is_pid_alive(pid):
            raise RuntimeError(f"报告任务 {path.stem} 的旧进程 {pid} 仍存活；请停止旧进程后重试 dayu-cli init。")
        state = ReportRunState.STALE
        updated_at = datetime.now(timezone.utc).isoformat()
        error = _INTERRUPTED_ERROR
    return ReportRun(
        state=state, pid=pid, started_at=started_at,
        updated_at=updated_at, output_dir=_required_text(payload, "output_dir"),
        error=error, log_path=_required_text(payload, "log_path"), run_id=None,
    )


def _migrate_locked_report(path: Path) -> bool:
    """在调用方持有任务状态锁时校验并原子发布单文件迁移结果。

    参数:
        path: 待迁移的报告 JSON 文件。
    返回值:
        实际改写旧 schema 时为真；当前新 schema 校验通过时为假。
    异常:
        ValueError: JSON 或报告状态内容不合法。
        RuntimeError: 旧 worker 尚存活，不能迁移。
        OSError: 状态读取、进程检查或原子写入失败。
    """
    payload = cast(JsonValue, json.loads(path.read_text(encoding=_TEXT_ENCODING)))
    if not isinstance(payload, dict):
        raise ValueError(f"报告任务状态必须是 JSON 对象：{path}")
    if _RUN_ID_FIELD in payload:
        # 新 schema 仍严格校验，不能把损坏或半迁移文件静默认定为成功。
        read_report_run(path)
        return False
    run = _convert_legacy_report(payload, path)
    write_report_run(path, run)
    return True


def migrate_report_run_identity(workspace_root: Path) -> int:
    """将工作区全部旧报告运行态原地升级为包含 run_id 的 schema。

    参数:
        workspace_root: 工作区根目录。
    返回值:
        实际完成迁移的状态文件数量；目录缺失时为零。
    异常:
        ValueError: JSON、旧 schema 或已迁移的新 schema 不合法。
        RuntimeError: 报告状态锁被占用，或旧报告 worker 尚存活。
        OSError: 非锁竞争的文件访问、进程检查或原子写入失败。
    """
    directory = report_run_directory(workspace_root)
    if not directory.is_dir():
        return 0
    rewritten = 0
    for path in sorted(directory.glob(_STATE_GLOB)):
        # 固定旁路锁不能删除，原子替换 JSON 时它与 worker 的互斥仍然有效。
        with path.with_suffix(_LOCK_SUFFIX).open("a+", encoding=_TEXT_ENCODING) as stream:
            try:
                acquire_text_file_lock(stream, blocking=False, lock_name=_LOCK_NAME)
            except OSError as exc:
                if is_lock_contention_error(exc):
                    raise RuntimeError(f"报告任务 {path.stem} 的状态锁被占用，请等待当前操作结束后重试 dayu-cli init。") from exc
                raise
            try:
                if _migrate_locked_report(path):
                    rewritten += 1
            finally:
                release_text_file_lock(stream, lock_name=_LOCK_NAME)
    return rewritten
