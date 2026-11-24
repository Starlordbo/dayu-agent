"""用 Streamlit AppTest 验证页面控件、重绘与单项恢复；不等同真实浏览器验收。"""

from __future__ import annotations

from pathlib import Path
from typing import cast
from urllib.parse import parse_qs, urlsplit

import pytest
from streamlit.proto.Common_pb2 import FileURLs
from streamlit.runtime.uploaded_file_manager import UploadedFile, UploadedFileRec
from streamlit.testing.v1 import AppTest

from dayu.contracts.fins import FinsCommandName
from dayu.fins.service_runtime import DefaultFinsRuntime
from dayu.web.streamlit.pages.material_tab import (
    STATUS_READY, MaterialUploadEntry, MaterialUploadInput, execute_material_input,
)
from tests.application.test_material_tab import RecordingService
from tests.application.test_material_upload_integration import RuntimeSubmissionService

_APP = '''
from pathlib import Path
import streamlit as st
from dayu.web.streamlit.pages.material_tab import render_material_tab
from dayu.web.streamlit.components.watchlist import WatchlistItem
render_material_tab(WatchlistItem("AAPL", "Apple", "", ""), Path(st.session_state["root"]), st.session_state["service"])
'''


def fixture_uploader(label: str, *, accept_multiple_files: bool, type: list[str], key: str) -> list[UploadedFile]:
    """替代 AppTest 尚不支持的浏览器文件上传控件，其他控件均为真实 Streamlit。

    Args:
        label: 控件标题。
        accept_multiple_files: 多文件标记。
        type: 支持的后缀。
        key: 控件键。
    Returns:
        两个真实 UploadedFile 对象。
    Raises:
        无。
    """
    return [UploadedFile(UploadedFileRec(str(index), name, "text/plain", b"call content"), FileURLs())
            for index, name in enumerate(("2023Q4_call.md", "unknown.md"))]


def test_page_confirmation_retry_and_rerun(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """点击真实页面控件，验证失败项单独恢复及重绘不重复提交。

    Args:
        tmp_path: 暂存工作区。
        monkeypatch: 文件上传替身注入工具。
    Returns:
        无。
    Raises:
        AssertionError: 控件或页面状态不符合预期。
    """
    monkeypatch.setattr("dayu.web.streamlit.pages.material_tab.st.file_uploader", fixture_uploader)
    service = RecordingService(fail_processing=True)
    app = AppTest.from_string(_APP)
    app.session_state["root"] = str(tmp_path)
    app.session_state["service"] = service
    app.run()
    assert not app.exception
    assert len(app.text_input) == 4
    assert len(app.checkbox) == 1
    app.button[0].click().run()
    assert not app.exception
    assert service.calls == [FinsCommandName.UPLOAD_MATERIAL, FinsCommandName.PROCESS_MATERIAL]
    assert len(app.dataframe) == 1
    app.run()
    assert len(service.calls) == 2
    service.fail_processing = False
    app.button[1].click().run()
    assert not app.exception
    assert service.calls[-1] == FinsCommandName.PROCESS_MATERIAL
    assert len(service.calls) == 3
    # 补全未知文件；已成功文件继续保留，不随“开始上传”再次提交。
    app.text_input[2].set_value("Custom Call")
    app.text_input[3].set_value("2024")
    app.selectbox[1].set_value("Q1")
    app.checkbox[0].check()
    app.button[0].click().run()
    assert not app.exception
    assert len(service.calls) == 5
    current = cast(RecordingService, app.session_state["service"])
    assert current.calls.count(FinsCommandName.UPLOAD_MATERIAL) == 2


def empty_uploader(label: str, *, accept_multiple_files: bool, type: list[str], key: str) -> list[UploadedFile]:
    """模拟没有选择文件的真实上传控件返回值。

    参数:
        label: 控件标题。
        accept_multiple_files: 多文件标记。
        type: 支持的后缀。
        key: 控件键。
    返回值:
        空文件列表。
    异常:
        无。
    """
    return []


def duplicate_uploader(label: str, *, accept_multiple_files: bool, type: list[str], key: str) -> list[UploadedFile]:
    """模拟两次选择同名同内容文件，但保留不同的浏览器文件编号。

    参数:
        label: 控件标题。
        accept_multiple_files: 多文件标记。
        type: 支持的后缀。
        key: 控件键。
    返回值:
        名称和内容完全一致的两个真实上传文件对象。
    异常:
        无。
    """
    return [
        UploadedFile(UploadedFileRec(str(index), "2023Q4_call.md", "text/plain", b"call content"), FileURLs())
        for index in range(2)
    ]


@pytest.mark.parametrize("has_files", [False, True])
def test_catalog_uses_persisted_title_and_document_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, has_files: bool,
) -> None:
    """通过真实仓储验证目录标题和链接，未选文件时也应展示已入库材料。

    参数:
        tmp_path: 真实服务使用的隔离工作区。
        monkeypatch: 仅替代浏览器上传控件。
        has_files: 页面当前是否另有待上传文件。
    返回值:
        无。
    异常:
        AssertionError: 目录丢失、使用文件名代替标题或丢失会话参数时抛出。
    """
    runtime = DefaultFinsRuntime.create(workspace_root=tmp_path)
    service = RuntimeSubmissionService(runtime)
    title = "Q4 2023 已核实电话会议原文"
    source = Path(__file__).parents[1] / "fixtures" / "earnings_call" / "2023Q4_call.md"
    item = MaterialUploadInput(
        MaterialUploadEntry(source.name, title, 2023, "Q4", False, "confirmed"), source.read_bytes(),
    )
    row = execute_material_input(
        ticker="AAPL", company_name="Apple", item=item, workspace_root=tmp_path, fins_service=service,
    )
    assert row.status == STATUS_READY, row.message
    assert row.document_id is not None
    calls_after_upload = service.calls.copy()
    monkeypatch.setattr(
        "dayu.web.streamlit.pages.material_tab.st.file_uploader", fixture_uploader if has_files else empty_uploader,
    )
    app = AppTest.from_string(_APP)
    app.session_state["root"] = str(tmp_path)
    app.session_state["service"] = service
    app.query_params["chat_tab_AAPL_session"] = "existing-chat-session"
    app.query_params["source_ref"] = "stale-section"
    app.run()
    assert not app.exception
    assert len(app.dataframe) == 1
    assert app.dataframe[0].value["材料标题"].tolist() == [title]
    assert app.dataframe[0].value["文档编号"].tolist() == [row.document_id]
    markdown = [element.value for element in app.markdown]
    assert title in markdown
    links = [value for value in markdown if value.startswith("[文档来源](?")]
    assert len(links) == 1
    query = parse_qs(urlsplit(links[0].removeprefix("[文档来源](").removesuffix(")")).query)
    assert query == {
        "chat_tab_AAPL_session": ["existing-chat-session"], "ticker": ["AAPL"], "view": ["materials"],
        "source_ticker": ["AAPL"], "source_document": [row.document_id],
    }
    assert len(app.text_input) == (4 if has_files else 0)
    assert service.calls == calls_after_upload
    app.run()
    assert not app.exception and len(app.dataframe) == 1
    assert service.calls == calls_after_upload


def test_duplicate_file_selection_has_unique_widgets_and_single_submission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同名同内容重复选择只生成一组控件、一次上传处理，重绘不再提交。

    参数:
        tmp_path: 上传暂存工作区。
        monkeypatch: 注入重复选择的上传控件返回值。
    返回值:
        无。
    异常:
        AssertionError: 控件键冲突、重复处理或重绘再次提交时抛出。
    """
    monkeypatch.setattr("dayu.web.streamlit.pages.material_tab.st.file_uploader", duplicate_uploader)
    service = RecordingService()
    app = AppTest.from_string(_APP)
    app.session_state["root"] = str(tmp_path)
    app.session_state["service"] = service
    app.run()
    assert not app.exception
    assert len(app.text_input) == 2 and len(app.selectbox) == 1
    assert any("已忽略重复选择" in caption.value for caption in app.caption)
    app.button[0].click().run()
    assert not app.exception
    assert service.calls == [FinsCommandName.UPLOAD_MATERIAL, FinsCommandName.PROCESS_MATERIAL]
    assert len(app.dataframe) == 1 and len(app.dataframe[0].value) == 1
    app.run()
    app.button[0].click().run()
    assert not app.exception
    assert service.calls == [FinsCommandName.UPLOAD_MATERIAL, FinsCommandName.PROCESS_MATERIAL]
