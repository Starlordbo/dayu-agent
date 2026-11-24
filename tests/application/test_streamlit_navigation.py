"""验证来源返回上下文在全新 Streamlit 会话中的恢复，不冒充浏览器验收。"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

import pytest
from streamlit.testing.v1 import AppTest

from dayu.services.protocols import ChatServiceProtocol, FinsServiceProtocol, WriteServiceProtocol
from dayu.web.streamlit.pages import main_page

from dayu.web.streamlit.components.watchlist import WatchlistItem, save_watchlist_items

_SIDEBAR_APP = """
from pathlib import Path
import streamlit as st
from dayu.web.streamlit.components.sidebar import render_sidebar
selected = render_sidebar(Path(st.session_state["workspace"]))
st.text(selected.ticker if selected is not None else "未选择")
"""


def test_fresh_session_restores_origin_company_from_return_url(tmp_path: Path) -> None:
    """全新会话仅凭返回 URL 恢复原公司，而非依赖旧页面 session_state。

    Args:
        tmp_path: 隔离自选股存储目录。
    Returns:
        无。
    Raises:
        AssertionError: 返回 URL 未恢复原始公司或丢失独立聊天会话时抛出。
    """
    save_watchlist_items(tmp_path, [WatchlistItem("MSFT", "Microsoft", "", "")])
    app = AppTest.from_string(_SIDEBAR_APP)
    app.session_state["workspace"] = str(tmp_path)
    app.query_params.update({"ticker": "MSFT", "view": "analysis", "chat_tab_MSFT_session": "origin-session"})
    app.run()
    assert not app.exception
    assert app.text[0].value == "MSFT"
    assert app.query_params["chat_tab_MSFT_session"] == ["origin-session"]


@pytest.mark.parametrize("requested,expected", [("MSFT", "MSFT"), ("REMOVED", "未选择"), ("", "未选择")])
def test_explicit_origin_overrides_stale_selection(tmp_path: Path, requested: str, expected: str) -> None:
    """显式 URL 优先于旧选择，失效公司不能偷偷退回旧公司。

    Args:
        tmp_path: 自选股存储目录。
        requested: URL 原公司参数。
        expected: 应展示的选择。
    Returns:
        无。
    Raises:
        AssertionError: 旧公司或来源公司覆盖显式返回目标时抛出。
    """
    save_watchlist_items(tmp_path, [WatchlistItem("AAPL", "Apple", "", ""), WatchlistItem("MSFT", "Microsoft", "", "")])
    app = AppTest.from_string(_SIDEBAR_APP)
    app.session_state["workspace"] = str(tmp_path)
    app.session_state["selected_ticker"] = "AAPL"
    app.query_params.update({"ticker": requested, "source_ticker": "AAPL"})
    app.run()
    assert not app.exception and app.text[0].value == expected
    if requested == "REMOVED":
        assert "不在当前自选股" in app.warning[0].value


@pytest.mark.parametrize("old_selection", [None, "MSFT", "REMOVED"])
def test_absent_url_validates_session_selection(tmp_path: Path, old_selection: str | None) -> None:
    """普通入口仅复用仍存在的公司，不能从 source_ticker 猜测返回目标。

    Args:
        tmp_path: 自选股存储目录。
        old_selection: 原页面选择或未选择。
    Returns:
        无。
    Raises:
        AssertionError: 无效旧选择或来源身份被当作返回公司时抛出。
    """
    save_watchlist_items(tmp_path, [WatchlistItem("MSFT", "Microsoft", "", "")])
    app = AppTest.from_string(_SIDEBAR_APP)
    app.session_state["workspace"] = str(tmp_path)
    app.session_state["selected_ticker"] = old_selection
    app.query_params["source_ticker"] = "MSFT"
    app.run()
    assert not app.exception
    assert app.text[0].value == ("MSFT" if old_selection == "MSFT" else "未选择")
    assert ("ticker" in app.query_params) == (old_selection == "MSFT")


@pytest.mark.parametrize("with_callback", [False, True])
def test_sidebar_click_persists_choice_and_keeps_session(tmp_path: Path, with_callback: bool) -> None:
    """点击侧栏产生可恢复 URL，且不丢公司会话和功能页参数。

    Args:
        tmp_path: 自选股存储目录。
        with_callback: 是否启用调用方选择回调。
    Returns:
        无。
    Raises:
        AssertionError: 点击被旧 URL 抵消、回调丢失或参数被清空时抛出。
    """
    save_watchlist_items(tmp_path, [WatchlistItem("MSFT", "Microsoft", "", "")])
    script = _SIDEBAR_APP.replace(
        'selected = render_sidebar(Path(st.session_state["workspace"]))',
        'def record(item: WatchlistItem) -> None:\n    st.session_state["callback"] = item.ticker\n'
        'selected = render_sidebar(Path(st.session_state["workspace"]), '
        'on_select_callback=record if st.session_state["with_callback"] else None)',
    ).replace('import render_sidebar', 'import render_sidebar\nfrom dayu.web.streamlit.components.watchlist import WatchlistItem')
    app = AppTest.from_string(script)
    app.session_state["workspace"] = str(tmp_path)
    app.session_state["with_callback"] = with_callback
    app.query_params.update({"ticker": "REMOVED", "view": "materials", "chat_tab_MSFT_session": "keep"})
    app.run()
    app.button(key="stock_MSFT").click().run()
    assert not app.exception and app.text[0].value == "MSFT"
    assert app.query_params == {"ticker": ["MSFT"], "view": ["materials"], "chat_tab_MSFT_session": ["keep"]}
    if with_callback:
        assert app.session_state["callback"] == "MSFT"
    save_watchlist_items(tmp_path, [])
    app.run()
    assert not app.exception and app.text[0].value == "未选择"
    assert app.session_state["selected_ticker"] is None
    assert "暂无自选股" in app.info[0].value


def test_sidebar_dialog_actions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """导航改动仍保留自选股管理按钮的既有入口。

    Args:
        tmp_path: 自选股目录。
        monkeypatch: 仅替代对话框渲染边界。
    Returns:
        无。
    Raises:
        AssertionError: 管理入口未被调用时抛出。
    """
    dialog = Mock()
    monkeypatch.setattr("dayu.web.streamlit.components.sidebar.render_watchlist_manager", dialog)
    app = AppTest.from_string(_SIDEBAR_APP)
    app.session_state["workspace"] = str(tmp_path)
    app.run()
    app.button(key="manage_watchlist_btn").click().run()
    assert not app.exception
    dialog.assert_called_once_with(tmp_path)


def test_sidebar_read_failure_never_restores_unverified_company(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """自选股加载失败明确报错，不回退到未经核对的旧选择。

    Args:
        tmp_path: 自选股目录。
        monkeypatch: 注入自选股读取失败。
    Returns:
        无。
    Raises:
        AssertionError: 错误被隐藏或旧选择被错误复用时抛出。
    """
    monkeypatch.setattr("dayu.web.streamlit.components.sidebar.load_watchlist_items", Mock(side_effect=OSError("read failed")))
    app = AppTest.from_string(_SIDEBAR_APP)
    app.session_state["workspace"] = str(tmp_path)
    app.session_state["selected_ticker"] = "MSFT"
    app.run()
    assert not app.exception and app.text[0].value == "未选择"
    assert "read failed" in app.error[0].value


_DETAIL_APP = """
from pathlib import Path
import streamlit as st
from dayu.web.streamlit.components.watchlist import WatchlistItem
from dayu.web.streamlit.pages.main_page import render_stock_detail_page
render_stock_detail_page(WatchlistItem("MSFT", "Microsoft", "", ""), Path(st.session_state["workspace"]),
                         st.session_state["fins"], st.session_state["chat"], st.session_state["write"])
"""


@pytest.mark.parametrize("view,index", [(None, 0), ("filings", 0), ("analysis", 1), ("reports", 2), ("materials", 3), ("invalid", 0)])
def test_detail_page_restores_tab_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, view: str | None, index: int,
) -> None:
    """真实 Tab 容器默认页匹配返回来源；非法功能页安全回到首个 Tab。

    Args:
        tmp_path: 工作目录。
        monkeypatch: 仅替代四个 Tab 内容边界，不替代主页面或 Streamlit Tab。
        view: URL 功能页参数。
        index: 预期默认页索引。
    Returns:
        无。
    Raises:
        AssertionError: 默认页协议或详情页服务注入不正确时抛出。
    """
    renderers = [Mock() for _ in range(4)]
    for name, renderer in zip(("render_filing_tab", "render_chat_tab", "render_report_tab", "render_material_tab"), renderers, strict=True):
        monkeypatch.setattr(main_page, name, renderer)
    app = AppTest.from_string(_DETAIL_APP)
    app.session_state["workspace"] = str(tmp_path)
    app.session_state["fins"] = Mock(spec=FinsServiceProtocol)
    app.session_state["chat"] = Mock(spec=ChatServiceProtocol)
    app.session_state["write"] = Mock(spec=WriteServiceProtocol)
    if view is not None:
        app.query_params["view"] = view
    app.run()
    assert not app.exception
    assert app.get("tab_container")[0].proto.tab_container.default_tab_index == index
    assert [tab.label for tab in app.tabs] == ["🧾 财报管理", "🧠 交互式分析", "📊 分析报告", "📞 会议纪要"]
    assert all(renderer.call_count == 1 for renderer in renderers)
