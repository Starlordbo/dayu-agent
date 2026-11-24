"""报告后台任务的磁盘状态模型、严格解析与原子发布。

本模块只负责运行元数据，不读取财报文档，也不持有线程或文件锁。
运行互斥与存活判断由调用方在状态读写之外协调。
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import datetime
from enum import Enum
import json
import os
from pathlib import Path
import re
from tempfile import NamedTemporaryFile
from typing import cast

from dayu.contracts.agent_types import JsonValue


_RUN_DIRECTORY = Path(".dayu") / "streamlit" / "report_runs"
_TICKER_PATTERN = re.compile(r"[A-Z0-9][A-Z0-9._-]*")
_TEXT_ENCODING = "utf-8"
_JSON_INDENT = 2
_TEMP_SUFFIX = ".tmp"


class ReportRunState(str, Enum):
    """报告任务在磁盘上持久化的生命周期状态。"""

    RUNNING = "running"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    STALE = "stale"

    @property
    def is_active(self) -> bool:
        """判断任务是否仍占用该公司的生成入口。

        参数:
            无。
        返回值:
            正在执行或等待取消收敛时为真。
        异常:
            无。
        """
        return self in {ReportRunState.RUNNING, ReportRunState.CANCELLING}


@dataclass(frozen=True)
class ReportRun:
    """报告任务的不可变磁盘快照；时间保存为带时区 ISO 8601 字符串。"""

    state: ReportRunState
    pid: int
    started_at: str
    updated_at: str
    output_dir: str
    error: str | None
    log_path: str
    run_id: str | None


_RUN_FIELDS = frozenset(field.name for field in fields(ReportRun))


def normalize_report_ticker(ticker: str) -> str:
    """将股票代码转换为可安全用作状态文件名的统一大小写形式。

    Args:
        ticker: 原始股票代码，支持数字、字母以及点、横线、下划线。
    Returns:
        去除首尾空白并转为大写的代码，保留市场后缀与类别符号。
    Raises:
        ValueError: 代码为空或包含路径分隔符、空白等非法字符。
    """
    normalized = ticker.strip().upper()
    if _TICKER_PATTERN.fullmatch(normalized) is None:
        raise ValueError(f"报告股票代码无效：{ticker!r}")
    return normalized


def report_run_directory(workspace_root: Path) -> Path:
    """定位报告运行元数据目录，供运行时与工作区迁移共用。

    参数:
        workspace_root: 当前工作区根目录。
    返回值:
        运行状态目录，不创建文件。
    异常:
        无。
    """
    return workspace_root / _RUN_DIRECTORY


def report_run_path(workspace_root: Path, ticker: str) -> Path:
    """计算按股票代码隔离的报告任务状态路径，不创建目录。

    Args:
        workspace_root: 当前工作区根目录。
        ticker: 原始股票代码。
    Returns:
        工作区内对应股票的 JSON 状态文件路径。
    Raises:
        ValueError: 股票代码无法安全构成文件名。
    """
    return report_run_directory(workspace_root) / f"{normalize_report_ticker(ticker)}.json"


def _required_text(payload: dict[str, JsonValue], field_name: str) -> str:
    """从完整状态载荷读取非空文本字段。

    Args:
        payload: 已确认字段集合完整的状态载荷。
        field_name: 要读取的字段名。
    Returns:
        原始非空文本，不修改路径或时间的内容。
    Raises:
        ValueError: 字段不是文本或仅含空白。
        KeyError: 调用方提供的字段不在载荷中。
    """
    value = payload[field_name]
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"报告任务状态字段 {field_name} 必须是非空文本")
    return value


def _required_timestamp(payload: dict[str, JsonValue], field_name: str) -> str:
    """校验并读取带时区的 ISO 8601 时间文本。

    Args:
        payload: 已确认字段集合完整的状态载荷。
        field_name: 要读取的时间字段名。
    Returns:
        可解析且带时区的原始时间文本。
    Raises:
        ValueError: 字段不是有效时间或未包含时区。
        KeyError: 调用方提供的字段不在载荷中。
    """
    value = _required_text(payload, field_name)
    timestamp = datetime.fromisoformat(value)
    if timestamp.utcoffset() is None:
        raise ValueError(f"报告任务状态字段 {field_name} 必须包含时区")
    return value


def _parse_report_run(payload: JsonValue) -> ReportRun:
    """将 JSON 值严格收窄为完整的报告运行态。

    Args:
        payload: 从 JSON 解码得到的值。
    Returns:
        字段、类型与必要语义均有效的不可变运行快照。
    Raises:
        ValueError: 状态结构、字段类型、进程号、时间或失败原因无效。
    """
    if not isinstance(payload, dict) or payload.keys() != _RUN_FIELDS:
        raise ValueError("报告任务状态字段不完整或包含未知字段")
    state = ReportRunState(_required_text(payload, "state"))
    pid = payload["pid"]
    # bool 是 int 的子类，但 JSON 布尔值不能作为进程号被接受。
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        raise ValueError("报告任务状态 pid 必须是正整数")
    error = payload["error"]
    if error is not None and not isinstance(error, str):
        raise ValueError("报告任务状态 error 必须是文本或 null")
    if state is ReportRunState.FAILED and (error is None or not error.strip()):
        raise ValueError("失败的报告任务必须包含错误原因")
    run_id = payload["run_id"]
    if run_id is not None and (not isinstance(run_id, str) or not run_id.strip()):
        raise ValueError("报告任务状态 run_id 必须是非空文本或 null")
    return ReportRun(
        state=state,
        pid=pid,
        started_at=_required_timestamp(payload, "started_at"),
        updated_at=_required_timestamp(payload, "updated_at"),
        output_dir=_required_text(payload, "output_dir"),
        error=error,
        log_path=_required_text(payload, "log_path"),
        run_id=run_id,
    )


def read_report_run(path: Path) -> ReportRun | None:
    """读取磁盘运行快照；缺失文件返回空，损坏状态显式报错。

    Args:
        path: 报告运行态 JSON 文件路径。
    Returns:
        完整运行快照；文件尚不存在时返回 None。
    Raises:
        ValueError: 文件不是有效 UTF-8 JSON 或状态字段不符合契约。
        OSError: 除文件不存在之外的读取错误。
    """
    try:
        content = path.read_text(encoding=_TEXT_ENCODING)
    except FileNotFoundError:
        return None
    payload = cast(JsonValue, json.loads(content))
    return _parse_report_run(payload)


def write_report_run(path: Path, run: ReportRun) -> None:
    """校验快照后通过同目录临时文件和原子替换发布状态。

    Args:
        path: 目标 JSON 文件路径；父目录不存在时创建。
        run: 需要完整持久化的报告运行快照。
    Returns:
        无。
    Raises:
        ValueError: 快照不满足磁盘状态契约。
        OSError: 创建目录、写入、刷盘、替换或清理临时文件失败。
    """
    payload: dict[str, JsonValue] = {
        "state": run.state.value,
        "pid": run.pid,
        "started_at": run.started_at,
        "updated_at": run.updated_at,
        "output_dir": run.output_dir,
        "error": run.error,
        "log_path": run.log_path,
        "run_id": run.run_id,
    }
    _parse_report_run(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with NamedTemporaryFile(
            mode="w", encoding=_TEXT_ENCODING, dir=path.parent,
            prefix=f".{path.name}.", suffix=_TEMP_SUFFIX, delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            json.dump(payload, handle, ensure_ascii=False, indent=_JSON_INDENT)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        # 每次写入使用独占临时文件，清理不会误删另一写入者的中间状态。
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
