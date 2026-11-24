"""Streamlit 侧边栏组件。

提供自选股列表展示和选择功能，并展示当前工作区目录。
使用本地JSON文件存储（workspace/.dayu/streamlit/watchlist.json），刷新不丢失。
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

import streamlit as st

from dayu.web.streamlit.components.watchlist import WatchlistItem, load_watchlist_items, render_watchlist_manager
from dayu.web.streamlit.navigation import TICKER_QUERY_KEY


def render_sidebar(
    workspace_root: Path,
    on_select_callback: Callable[[WatchlistItem], None] | None = None,
) -> WatchlistItem | None:
    """渲染侧边栏，展示自选股列表并处理选择。

    参数:
        workspace_root: 工作区根目录，用于读取自选股存储文件。
        on_select_callback: 选中自选股后的回调函数，接收 WatchlistItem 参数。

    返回值:
        URL 或页面状态中选定且仍在自选股中的条目；未选中或失效时返回 None。
    异常:
        无；自选股加载失败在侧栏显示错误。
    """

    st.sidebar.title("大禹 Agent")

    # 工作区信息展示（优化样式）
    workspace_resolved = workspace_root.resolve()

    # 使用 caption 样式展示工作区路径
    st.sidebar.markdown(f"**📁 工作区**  \n`{workspace_resolved}`")

    st.sidebar.markdown("---")

    # 选中按钮使用 primary 类型，通过 CSS 改为仅边框高亮
    st.sidebar.markdown(
        """
        <style>
        section[data-testid="stSidebar"] div[data-testid="stButton"] > button[kind="primary"] {
            background-color: transparent;
            color: inherit;
            border: 1px solid #ff4b4b;
            box-shadow: 0 0 0 1px rgba(255, 75, 75, 0.25);
        }
        section[data-testid="stSidebar"] div[data-testid="stButton"] > button[kind="primary"]:hover {
            background-color: rgba(255, 75, 75, 0.06);
            color: inherit;
            border: 1px solid #ff4b4b;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    # 每次从持久化自选股核对选择，对话框与外部改动均不能留下失效公司。
    st.session_state.pop("watchlist_needs_refresh", None)

    # 获取自选股列表（从文件读取）
    try:
        watchlist = load_watchlist_items(workspace_root)
    except Exception as e:
        st.sidebar.error(f"加载自选股失败: {e}")
        watchlist = []

    # URL 是跨刷新/新会话的返回上下文；来源公司的 source_ticker 不能参与选股。
    requested_ticker = st.query_params.get(TICKER_QUERY_KEY)
    selected_ticker = requested_ticker if requested_ticker is not None else st.session_state.get("selected_ticker")
    selected_item = next((item for item in watchlist if item.ticker == selected_ticker), None)
    st.session_state["selected_ticker"] = selected_item.ticker if selected_item is not None else None
    if selected_item is not None:
        st.query_params[TICKER_QUERY_KEY] = selected_item.ticker
    elif requested_ticker:
        st.sidebar.warning("返回链接中的公司不在当前自选股中，请重新选择或添加；不会自动切换到来源公司。")

    # 自选股标题行：左侧标题，右侧管理按钮（icon 按钮，更小）
    col1, col2 = st.sidebar.columns([5, 1], vertical_alignment="center")
    with col1:
        st.markdown("**❤️ 自选股**")

    with col2:
        if st.button("", key="manage_watchlist_btn", icon=":material/list_alt_add:", type="tertiary", help="管理自选股"):
            # 调用对话框函数（装饰器会自动处理）
            render_watchlist_manager(workspace_root)

    # 展示自选股列表
    for item in watchlist:
        display_name = f"{item.company_name} ({item.ticker})"
        current_selected_ticker = st.session_state.get("selected_ticker")
        is_selected = isinstance(current_selected_ticker, str) and current_selected_ticker == item.ticker

        # 使用按钮展示每个自选股
        button_type = "primary" if is_selected else "secondary"
        if st.sidebar.button(display_name, key=f"stock_{item.ticker}", type=button_type, width="stretch"):
            st.session_state["selected_ticker"] = item.ticker
            st.query_params[TICKER_QUERY_KEY] = item.ticker
            selected_item = item
            if on_select_callback:
                on_select_callback(item)
            st.rerun()

    if not watchlist:
        st.sidebar.info("暂无自选股，请点击管理按钮添加")

    st.sidebar.markdown("---")

    return selected_item
