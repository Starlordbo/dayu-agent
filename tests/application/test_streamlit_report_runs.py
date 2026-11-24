"""报告任务状态文件的严格解析与原子写入测试。"""

from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from dayu.contracts.agent_types import JsonValue
from dayu.web.streamlit.report_runs import (
    ReportRun,
    ReportRunState,
    normalize_report_ticker,
    read_report_run,
    report_run_path,
    write_report_run,
)


_TIMESTAMP = "2026-11-24T12:00:00+00:00"
_TICKER = "AAPL"


def _run(workspace: Path) -> ReportRun:
    """构造完整的合法运行态。

    Args:
        workspace: 测试工作区。
    Returns:
        当前进程的报告运行态。
    Raises:
        无。
    """
    return ReportRun(
        state=ReportRunState.RUNNING,
        pid=os.getpid(),
        started_at=_TIMESTAMP,
        updated_at=_TIMESTAMP,
        output_dir=str(workspace / "draft" / _TICKER),
        error=None,
        log_path=str(workspace / "report.log"),
        run_id=None,
    )


def _payload(workspace: Path) -> dict[str, JsonValue]:
    """构造可被局部篡改的合法 JSON 载荷。

    Args:
        workspace: 测试工作区。
    Returns:
        包含完整字段的字典。
    Raises:
        无。
    """
    run = _run(workspace)
    return {
        "state": run.state.value,
        "pid": run.pid,
        "started_at": run.started_at,
        "updated_at": run.updated_at,
        "output_dir": run.output_dir,
        "error": run.error,
        "log_path": run.log_path,
        "run_id": run.run_id,
    }


@pytest.mark.parametrize("ticker,expected", [
    (" aapl ", "AAPL"), ("brk.b", "BRK.B"), ("bf-b", "BF-B"),
    ("0700.hk", "0700.HK"), ("600519.sh", "600519.SH"),
])
def test_ticker_normalization_preserves_market_symbols(ticker: str, expected: str) -> None:
    """验证文件名归一化不改写市场符号语义。

    Args:
        ticker: 原始代码。
        expected: 预期归一化结果。
    Returns:
        无。
    Raises:
        AssertionError: 代码被错误修改。
    """
    assert normalize_report_ticker(ticker) == expected


@pytest.mark.parametrize("ticker", ["", " ", "..", "../AAPL", "/tmp", "A/B", "A\\B", "A A", "A\x00"])
def test_invalid_ticker_is_rejected(ticker: str) -> None:
    """验证目录穿越与非法文件名被拒绝。

    Args:
        ticker: 非法股票代码。
    Returns:
        无。
    Raises:
        AssertionError: 非法代码被接受。
    """
    with pytest.raises(ValueError):
        normalize_report_ticker(ticker)


def test_path_and_missing_read_have_no_side_effects(tmp_path: Path) -> None:
    """验证路径计算与首次读取不创建状态目录。

    Args:
        tmp_path: 测试工作区。
    Returns:
        无。
    Raises:
        AssertionError: 首次读取产生磁盘状态。
    """
    path = report_run_path(tmp_path, " aapl ")
    assert path == tmp_path / ".dayu" / "streamlit" / "report_runs" / "AAPL.json"
    assert read_report_run(path) is None
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("state", list(ReportRunState))
def test_state_round_trip_and_atomic_replace(tmp_path: Path, state: ReportRunState) -> None:
    """验证各状态可完整往返且最终文件通过原子替换发布。

    Args:
        tmp_path: 测试工作区。
        state: 待验证状态。
    Returns:
        无。
    Raises:
        AssertionError: 序列化丢失字段或未使用原子替换。
    """
    path = report_run_path(tmp_path, _TICKER)
    error = "模拟中断或失败" if state in {ReportRunState.FAILED, ReportRunState.STALE} else None
    run = replace(_run(tmp_path), state=state, error=error, run_id="host-run-1")
    with patch("dayu.web.streamlit.report_runs.os.replace", wraps=os.replace) as atomic_replace:
        write_report_run(path, run)
    atomic_replace.assert_called_once()
    source, destination = atomic_replace.call_args.args
    assert Path(source).parent == path.parent
    assert destination == path
    assert read_report_run(path) == run
    assert list(path.parent.iterdir()) == [path]


@pytest.mark.parametrize("field_name,invalid", [
    ("state", "unknown"), ("state", 1), ("pid", True), ("pid", 0), ("pid", -1),
    ("pid", "123"), ("pid", 1.5), ("started_at", "2026-11-24T12:00:00"),
    ("started_at", "invalid"), ("updated_at", None), ("output_dir", " "),
    ("log_path", ""), ("log_path", []), ("error", 3),
    ("run_id", ""), ("run_id", " "), ("run_id", 123), ("run_id", False),
])
def test_invalid_field_is_rejected_without_changing_file(
    tmp_path: Path, field_name: str, invalid: JsonValue,
) -> None:
    """验证 JSON 字段类型与语义均严格检查。

    Args:
        tmp_path: 测试工作区。
        field_name: 待破坏字段。
        invalid: 非法字段值。
    Returns:
        无。
    Raises:
        AssertionError: 损坏状态被接受或读操作修改文件。
    """
    payload = _payload(tmp_path)
    payload[field_name] = invalid
    path = tmp_path / "run.json"
    raw = json.dumps(payload)
    path.write_text(raw, encoding="utf-8")
    with pytest.raises(ValueError):
        read_report_run(path)
    assert path.read_text(encoding="utf-8") == raw


@pytest.mark.parametrize("raw", ["{broken", "[]", "null", "{}"])
def test_malformed_state_is_explicit(tmp_path: Path, raw: str) -> None:
    """验证语法错误或不完整结构不会被视为尚未运行。

    Args:
        tmp_path: 测试工作区。
        raw: 损坏 JSON 文本。
    Returns:
        无。
    Raises:
        AssertionError: 损坏状态被忽略。
    """
    path = tmp_path / "run.json"
    path.write_text(raw, encoding="utf-8")
    with pytest.raises(ValueError):
        read_report_run(path)


def test_unknown_fields_are_rejected(tmp_path: Path) -> None:
    """验证磁盘模型不静默吞掉额外字段。

    Args:
        tmp_path: 测试工作区。
    Returns:
        无。
    Raises:
        AssertionError: 未声明字段被接受。
    """
    payload = _payload(tmp_path)
    payload["extra"] = "unexpected"
    path = tmp_path / "run.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError):
        read_report_run(path)


@pytest.mark.parametrize("error", [None, "", " "])
def test_failed_state_requires_explanation(tmp_path: Path, error: str | None) -> None:
    """验证失败状态必须提供可展示的错误原因。

    Args:
        tmp_path: 测试工作区。
        error: 缺失或不可展示的错误说明。
    Returns:
        无。
    Raises:
        AssertionError: 没有错误说明的失败状态被接受。
    """
    payload = _payload(tmp_path)
    payload["state"] = ReportRunState.FAILED.value
    payload["error"] = error
    path = tmp_path / "run.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="错误原因"):
        read_report_run(path)


def test_replace_failure_preserves_previous_state_and_removes_temp_file(tmp_path: Path) -> None:
    """验证替换失败不会破坏原状态或遗留临时文件。

    Args:
        tmp_path: 测试工作区。
    Returns:
        无。
    Raises:
        AssertionError: 原状态被破坏或临时文件未清理。
    """
    path = report_run_path(tmp_path, _TICKER)
    previous = _run(tmp_path)
    write_report_run(path, previous)
    with patch("dayu.web.streamlit.report_runs.os.replace", side_effect=OSError("替换失败")):
        with pytest.raises(OSError, match="替换失败"):
            write_report_run(path, replace(previous, state=ReportRunState.SUCCEEDED))
    assert read_report_run(path) == previous
    assert list(path.parent.iterdir()) == [path]


def test_invalid_write_fails_before_creating_directory(tmp_path: Path) -> None:
    """验证无效内存状态不会被发布为磁盘事实。

    Args:
        tmp_path: 测试工作区。
    Returns:
        无。
    Raises:
        AssertionError: 无效状态被写出或创建了目录。
    """
    path = report_run_path(tmp_path, _TICKER)
    with pytest.raises(ValueError):
        write_report_run(path, replace(_run(tmp_path), pid=0))
    assert not path.parent.exists()


def test_read_permission_error_is_not_hidden(tmp_path: Path) -> None:
    """验证文件系统错误不会被混同为没有状态。

    Args:
        tmp_path: 测试工作区。
    Returns:
        无。
    Raises:
        AssertionError: 读取失败被吞掉。
    """
    with patch("pathlib.Path.read_text", side_effect=PermissionError("不可读")):
        with pytest.raises(PermissionError, match="不可读"):
            read_report_run(tmp_path / "run.json")
