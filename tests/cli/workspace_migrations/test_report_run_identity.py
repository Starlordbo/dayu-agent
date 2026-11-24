"""报告运行态 run_id 迁移测试，覆盖旧状态、互斥、幂等与失败保留。"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from dayu.cli.workspace_migrations import report_run_identity
from dayu.cli.workspace_migrations.report_run_identity import migrate_report_run_identity
from dayu.cli.workspace_migrations.runner import apply_all_workspace_migrations
from dayu.contracts.agent_types import JsonValue
from dayu.file_lock import acquire_text_file_lock, release_text_file_lock
from dayu.web.streamlit.report_runs import ReportRun, ReportRunState, read_report_run, report_run_path


_TIMESTAMP = "2026-11-24T01:00:00+00:00"
_TICKER = "AAPL"
_LOCK_NAME = "报告任务状态锁"


def _legacy_payload(*, state: str = "succeeded") -> dict[str, JsonValue]:
    """构造迁移前的报告状态。

    参数:
        state: 旧版本持久状态值。
    返回值:
        不含 run_id 的完整旧 schema。
    异常:
        无。
    """
    return {
        "state": state, "pid": os.getpid(), "started_at": _TIMESTAMP,
        "updated_at": _TIMESTAMP, "output_dir": "reports/AAPL", "error": None,
        "log_path": "report.log",
    }


def _write_payload(workspace_root: Path, payload: JsonValue) -> Path:
    """将测试状态放入真实报告目录。

    参数:
        workspace_root: 临时工作区。
        payload: 待序列化的状态值。
    返回值:
        状态文件路径。
    异常:
        OSError: 测试文件创建失败。
    """
    path = report_run_path(workspace_root, _TICKER)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _dead_pid(pid: int) -> bool:
    """模拟旧报告所属进程已退出。

    参数:
        pid: 待检查的旧进程号。
    返回值:
        恒为假。
    异常:
        无。
    """
    del pid
    return False


def _reject_write(path: Path, run: ReportRun) -> None:
    """模拟迁移提交失败。

    参数:
        path: 待写入的状态文件。
        run: 已转换的新状态。
    返回值:
        无。
    异常:
        OSError: 恒定模拟磁盘写入失败。
    """
    del path, run
    raise OSError("模拟磁盘写入失败")


@pytest.mark.parametrize("state", ["succeeded", "failed", "stale"])
def test_migration_preserves_terminal_status_and_artifacts(tmp_path: Path, state: str) -> None:
    """迁移补充 run_id，保留终态、历史时间与产物。

    参数:
        tmp_path: 临时工作区。
        state: 要覆盖的旧终态。
    返回值:
        无。
    异常:
        无。
    """
    payload = _legacy_payload(state=state)
    payload["error"] = "原失败原因" if state == "failed" else None
    log_path = tmp_path / "report.log"
    log_path.write_text("历史运行日志", encoding="utf-8")
    output_dir = tmp_path / "reports" / _TICKER
    output_dir.mkdir(parents=True)
    report_path = output_dir / "report.md"
    report_path.write_text("已有报告", encoding="utf-8")
    payload["log_path"] = str(log_path)
    payload["output_dir"] = str(output_dir)
    path = _write_payload(tmp_path, payload)

    assert migrate_report_run_identity(tmp_path) == 1

    run = read_report_run(path)
    assert run is not None
    assert run.run_id is None
    assert run.state.value == state
    assert run.started_at == run.updated_at == _TIMESTAMP
    assert run.error == payload["error"]
    assert json.loads(path.read_text(encoding="utf-8")) == {**payload, "run_id": None}
    assert log_path.read_text(encoding="utf-8") == "历史运行日志"
    assert report_path.read_text(encoding="utf-8") == "已有报告"


def test_migration_refuses_live_legacy_worker(tmp_path: Path) -> None:
    """旧 worker 存活时拒绝改写，避免旧进程再次提交旧 schema。

    参数:
        tmp_path: 临时工作区。
    返回值:
        无。
    异常:
        无。
    """
    path = _write_payload(tmp_path, _legacy_payload(state="running"))
    original = path.read_bytes()

    with pytest.raises(RuntimeError, match="停止旧进程.*重试"):
        migrate_report_run_identity(tmp_path)

    assert path.read_bytes() == original


def test_migration_marks_dead_legacy_worker_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """旧 worker 已退出时恢复为中断状态，不遗留假运行态。

    参数:
        tmp_path: 临时工作区。
        monkeypatch: 进程存活检查替换工具。
    返回值:
        无。
    异常:
        无。
    """
    path = _write_payload(tmp_path, _legacy_payload(state="running"))
    monkeypatch.setattr(report_run_identity, "is_pid_alive", _dead_pid)

    assert migrate_report_run_identity(tmp_path) == 1

    run = read_report_run(path)
    assert run is not None
    assert run.state is ReportRunState.STALE
    assert run.run_id is None
    assert run.started_at == _TIMESTAMP
    assert run.updated_at != _TIMESTAMP
    assert run.error is not None and "中断" in run.error


def test_migration_is_idempotent_and_handles_absent_directory(tmp_path: Path) -> None:
    """不存在的目录无操作，已迁移文件再次执行不会被写回。

    参数:
        tmp_path: 临时工作区。
    返回值:
        无。
    异常:
        无。
    """
    assert migrate_report_run_identity(tmp_path) == 0
    assert not report_run_path(tmp_path, _TICKER).parent.exists()
    path = _write_payload(tmp_path, _legacy_payload())
    assert migrate_report_run_identity(tmp_path) == 1
    migrated = path.read_bytes()
    modified_at = path.stat().st_mtime_ns

    assert migrate_report_run_identity(tmp_path) == 0
    assert path.read_bytes() == migrated
    assert path.stat().st_mtime_ns == modified_at


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("pid", True), ("pid", 0), ("error", []), ("state", "cancelling"),
        ("output_dir", ""), ("started_at", "invalid-time"),
        ("updated_at", "2026-11-24T01:00:00"),
    ],
)
def test_migration_rejects_invalid_legacy_fields(
    tmp_path: Path, field: str, value: JsonValue,
) -> None:
    """不完整或非法的旧状态必须显式失败并保留原始数据。

    参数:
        tmp_path: 临时工作区。
        field: 被破坏的字段。
        value: 非法字段值。
    返回值:
        无。
    异常:
        无。
    """
    payload = _legacy_payload()
    payload[field] = value
    path = _write_payload(tmp_path, payload)
    original = path.read_bytes()

    with pytest.raises(ValueError):
        migrate_report_run_identity(tmp_path)

    assert path.read_bytes() == original


def test_dead_worker_migration_rejects_invalid_prior_timestamp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """恢复死进程任务前仍校验原更新时间，不让状态变更掩盖损坏。

    参数:
        tmp_path: 临时工作区。
        monkeypatch: 进程存活检查替换工具。
    返回值:
        无。
    异常:
        无。
    """
    payload = _legacy_payload(state="running")
    payload["updated_at"] = "损坏时间"
    path = _write_payload(tmp_path, payload)
    original = path.read_bytes()
    monkeypatch.setattr(report_run_identity, "is_pid_alive", _dead_pid)

    with pytest.raises(ValueError):
        migrate_report_run_identity(tmp_path)

    assert path.read_bytes() == original


@pytest.mark.parametrize("payload", [[], {"state": "succeeded"}, {"run_id": None}])
def test_migration_rejects_unknown_or_broken_schema(tmp_path: Path, payload: JsonValue) -> None:
    """非对象、残缺旧 schema 与残缺新 schema 都不可静默跳过。

    参数:
        tmp_path: 临时工作区。
        payload: 损坏的状态载荷。
    返回值:
        无。
    异常:
        无。
    """
    path = _write_payload(tmp_path, payload)
    original = path.read_bytes()

    with pytest.raises(ValueError):
        migrate_report_run_identity(tmp_path)

    assert path.read_bytes() == original


def test_migration_does_not_ignore_invalid_json(tmp_path: Path) -> None:
    """无效 JSON 必须中断 init 并保留文件。

    参数:
        tmp_path: 临时工作区。
    返回值:
        无。
    异常:
        无。
    """
    path = _write_payload(tmp_path, _legacy_payload())
    path.write_text("{", encoding="utf-8")

    with pytest.raises(json.JSONDecodeError):
        migrate_report_run_identity(tmp_path)

    assert path.read_text(encoding="utf-8") == "{"


def test_migration_respects_report_state_lock(tmp_path: Path) -> None:
    """迁移与报告 worker 使用同一旁路文件锁，竞争时不改写状态。

    参数:
        tmp_path: 临时工作区。
    返回值:
        无。
    异常:
        无。
    """
    path = _write_payload(tmp_path, _legacy_payload())
    original = path.read_bytes()
    with path.with_suffix(".lock").open("a+", encoding="utf-8") as stream:
        acquire_text_file_lock(stream, blocking=False, lock_name=_LOCK_NAME)
        try:
            with pytest.raises(RuntimeError, match="状态锁"):
                migrate_report_run_identity(tmp_path)
        finally:
            release_text_file_lock(stream, lock_name=_LOCK_NAME)

    assert path.read_bytes() == original
    assert migrate_report_run_identity(tmp_path) == 1


def test_migration_propagates_write_failure_and_releases_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """写入异常上抛并保留旧文件，失败后锁仍可被重新取得。

    参数:
        tmp_path: 临时工作区。
        monkeypatch: 提交函数替换工具。
    返回值:
        无。
    异常:
        无。
    """
    path = _write_payload(tmp_path, _legacy_payload())
    original = path.read_bytes()
    with monkeypatch.context() as patch:
        patch.setattr(report_run_identity, "write_report_run", _reject_write)
        with pytest.raises(OSError, match="磁盘写入失败"):
            migrate_report_run_identity(tmp_path)

    assert path.read_bytes() == original
    assert migrate_report_run_identity(tmp_path) == 1


def test_init_runner_invokes_report_schema_migration(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """init 的迁移入口真实执行并汇报报告状态升级。

    参数:
        tmp_path: 临时工作区。
        capsys: 标准输出捕获工具。
    返回值:
        无。
    异常:
        无。
    """
    path = _write_payload(tmp_path, _legacy_payload())

    apply_all_workspace_migrations(base_dir=tmp_path, config_dir=tmp_path / "config")

    run = read_report_run(path)
    assert run is not None and run.run_id is None
    assert "报告运行态已补齐 run_id（共 1 个任务）" in capsys.readouterr().out
