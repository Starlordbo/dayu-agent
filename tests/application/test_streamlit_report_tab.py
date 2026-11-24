"""报告页真实 AppTest，全部使用可控假写作服务，禁止调用模型。"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from dayu.services.contracts import WriteRequest
from dayu.web.streamlit.report_runs import ReportRunState
from dayu.web.streamlit.report_runtime import load_report_run

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
)
'''


@dataclass
class FakeWriteService:
    """可由事件释放的离线服务，记录请求并模拟成功、失败和异常。"""

    exit_code: int = 0
    error: str = ""
    produce_report: bool = True
    entered: threading.Event = field(default_factory=threading.Event)
    release: threading.Event = field(default_factory=threading.Event)
    requests: list[WriteRequest] = field(default_factory=list)

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
        if not self.release.wait(_TIMEOUT):
            raise TimeoutError("测试未释放假写作服务")
        if self.error:
            raise RuntimeError(self.error)
        if self.produce_report:
            output = Path(request.write_config.output_dir)
            output.mkdir(parents=True, exist_ok=True)
            (output / f"{request.write_config.ticker}_qual_report.md").write_text(_REPORT, encoding="utf-8")
        return self.exit_code

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


def _app(workspace: Path, service: FakeWriteService | None, ticker: str = "AAPL") -> AppTest:
    """构建全新会话，只有输入依赖放在测试会话中。

    参数:
        workspace: 测试工作区。
        service: 假服务或缺失服务。
        ticker: 当前公司。
    返回值:
        尚未执行的 AppTest。
    异常:
        无。
    """
    app = AppTest.from_string(_APP)
    app.session_state["workspace"] = str(workspace)
    app.session_state["service"] = service
    app.session_state["ticker"] = ticker
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
        if run is not None and run.state != ReportRunState.RUNNING:
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
        "log_path": str(path.with_suffix(".log")),
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
