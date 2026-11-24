"""通过真实 main 与来源查看器验证入口路由；AppTest 不替代真实浏览器验收。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
import re
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

import pytest
from streamlit.testing.v1 import AppTest

from dayu.fins.domain.document_reference import (
    DocumentReference,
    DocumentReferenceView,
    DocumentSourceInfo,
    SourceSectionSummary,
    SourceSectionView,
)
from dayu.services.fins_service import FinsService
from dayu.services.protocols import ChatServiceProtocol, FinsServiceProtocol, WriteServiceProtocol
from dayu.services.startup_preparation import PreparedHostRuntimeDependencies, prepare_host_runtime_dependencies
from dayu.web.streamlit.components.watchlist import WatchlistItem, save_watchlist_items
from tests.application.test_material_tab import RecordingService
from tests.application.test_streamlit_chat_render import _ChatService, _turn

_APP = """
from dayu.web.streamlit_app import main
main()
"""
_SOURCE_QUERY = {
    "source_ticker": "AAPL",
    "source_document": "mat_call",
    "source_ref": "sec_call",
    "chat_tab_AAPL_session": "session-keep",
}
_SOURCE_BODY = "Jane Doe - Chief Executive Officer\n\nWe have not provided that revenue forecast."


@dataclass
class _SourceService(RecordingService):
    """继承完整类型的既有服务替身，只提供确定的只读原文视图。"""

    reads: list[DocumentReference] = field(default_factory=list)

    def resolve_document_reference(self, request: DocumentReference) -> DocumentReferenceView:
        """记录查看器的真实请求，拒绝错误公司、文档或章节。

        参数:
            request: 来源查看器传入的身份。
        返回值:
            固定文档头、目录及选中章节。
        异常:
            AssertionError: 主入口错误地使用侧栏身份或改写来源身份时抛出。
        """
        self.reads.append(request)
        assert (request.ticker, request.document_id) == ("AAPL", "mat_call")
        assert request.section_ref in (None, "sec_call")
        info = DocumentSourceInfo(
            ticker="AAPL", document_id="mat_call", company_name="Apple",
            material_name="真实入口电话会议来源", form_type="EARNINGS_CALL",
            fiscal_year=2023, fiscal_period="Q4", filing_date=None,
        )
        section = SourceSectionView(
            ref="sec_call", title="管理层答复", content=_SOURCE_BODY,
            contains_full_text=False, page_range=(2,), passages=(),
        )
        return DocumentReferenceView(
            info=info, sections=(SourceSectionSummary("sec_call", "管理层答复"),),
            selected_section=section if request.section_ref is not None else None,
        )


@dataclass
class _SidebarRecorder:
    """记录入口是否触达侧栏，并模拟与来源链接不同的当前股票。"""

    selected: WatchlistItem | None = None
    roots: list[Path] = field(default_factory=list)

    def render(self, *, workspace_root: Path) -> WatchlistItem | None:
        """提供严格类型的侧栏交互边界，记录执行顺序所需证据。

        参数:
            workspace_root: 主入口解析出的工作区。
        返回值:
            配置的当前自选股，或未选中状态。
        异常:
            无。
        """
        self.roots.append(workspace_root)
        return self.selected


@dataclass(frozen=True)
class _DetailCall:
    """入口传入股票详情页的完整类型参数快照。"""

    stock: WatchlistItem
    workspace_root: Path
    fins_service: FinsServiceProtocol
    chat_service: ChatServiceProtocol
    write_service: WriteServiceProtocol
    model_names: tuple[str, ...]


@dataclass
class _DetailRecorder:
    """仅替代详情页渲染，保留主入口的真实依赖解析与服务构造。"""

    calls: list[_DetailCall] = field(default_factory=list)

    def render(
        self, selected_stock: WatchlistItem, workspace_root: Path,
        fins_service: FinsServiceProtocol, chat_service: ChatServiceProtocol,
        write_service: WriteServiceProtocol, model_names: tuple[str, ...] = (),
        report_model_names: tuple[str, ...] = (),
    ) -> None:
        """记录详情页注入参数，不提交聊天或财报命令。

        参数:
            selected_stock: 侧栏选择的公司。
            workspace_root: 入口解析的工作区。
            fins_service: 真实构造的财报服务。
            chat_service: 真实构造的聊天服务。
            write_service: 真实构造的写作服务。
            model_names: 对话场景允许且已配置的模型名。
            report_model_names: 报告场景允许且已配置的模型名。
        返回值:
            无。
        异常:
            无。
        """
        self.calls.append(_DetailCall(
            selected_stock, workspace_root, fins_service, chat_service, write_service, model_names,
        ))


def _fail_preparation() -> PreparedHostRuntimeDependencies | None:
    """注入初始化失败，或充当重绘时不得重新初始化的哨兵。

    参数:
        无。
    返回值:
        不返回。
    异常:
        RuntimeError: 始终抛出可辨认的初始化失败。
    """
    raise RuntimeError("测试注入的启动装配失败")


@pytest.fixture
def prepared_deps(tmp_path: Path) -> PreparedHostRuntimeDependencies:
    """使用真实启动装配构建完整依赖，仅建本地仓储，不执行模型或网络请求。

    参数:
        tmp_path: 隔离的本地工作区。
    返回值:
        完整类型的 Host、Service 准备依赖。
    异常:
        OSError: 隔离工作区无法创建时抛出。
    """
    return prepare_host_runtime_dependencies(
        workspace_root=tmp_path, config_root=None, execution_options=None,
        runtime_label="source-routing-test", log_module="TEST.SOURCE_ROUTING",
    )


def _build_app(
    deps: PreparedHostRuntimeDependencies | None,
    service: _SourceService,
    query: Mapping[str, str],
) -> AppTest:
    """建立调用真实 main 的 AppTest，保留真实服务解析与查看器实现。

    参数:
        deps: 已初始化的真实依赖，或明确不可用状态。
        service: 注入会话缓存的只读财报服务。
        query: 模拟链接中的查询参数。
    返回值:
        尚未执行的入口测试应用。
    异常:
        无。
    """
    app = AppTest.from_string(_APP)
    app.session_state["initialized"] = True
    app.session_state["prepared_deps"] = deps
    app.session_state["fins_service"] = service
    app.session_state["selected_ticker"] = "MSFT"
    for key, value in query.items():
        app.query_params[key] = value
    return app


@pytest.mark.parametrize("section_ref", [None, "sec_call"])
def test_cross_company_source_returns_to_fresh_main_and_original_chat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    prepared_deps: PreparedHostRuntimeDependencies, section_ref: str | None,
) -> None:
    """真实入口串联跨公司引用、查看器与全新会话返回，不依赖旧选股缓存。

    参数:
        tmp_path: 持久自选股和服务装配目录。
        monkeypatch: 只隔离无关 Tab 与模型目录，保留真实入口、侧栏、主页面、聊天和查看器。
        prepared_deps: 本地完整运行依赖，不调用模型。
        section_ref: 文档或章节级引用。
    返回值:
        无。
    异常:
        AssertionError: 引用公司、默认页或原会话串扰，或产生写入/模型提交时抛出。
    """
    save_watchlist_items(tmp_path, [WatchlistItem("MSFT", "Microsoft", "", ""), WatchlistItem("AAPL", "Apple", "", "")])
    monkeypatch.setenv("DAYU_WORKSPACE", str(tmp_path))
    for name in ("render_filing_tab", "render_report_tab", "render_material_tab"):
        monkeypatch.setattr("dayu.web.streamlit.pages.main_page." + name, Mock())
    catalog = prepared_deps.scene_execution_acceptance_preparer.model_catalog
    monkeypatch.setattr(type(catalog), "load_models", Mock(return_value={}))
    marker = "[[source:AAPL|mat_call" + ("|" + section_ref if section_ref else "") + "]]"
    chat = _ChatService(histories={"original": [_turn(user="对照两家公司", answer="跨公司证据 " + marker)]})
    service = _SourceService()
    origin_query = {"ticker": "MSFT", "view": "analysis", "chat_tab_MSFT_session": "original", "other": "a & b"}
    origin = _build_app(prepared_deps, service, origin_query)
    origin.session_state["chat_service"] = chat
    origin.session_state["selected_ticker"] = "AAPL"
    origin.run()
    assert not origin.exception and not origin.error
    assert origin.session_state["selected_ticker"] == "MSFT"
    rendered = next(item.value for item in origin.markdown if "跨公司证据" in item.value)
    match = re.search(r"\[(?:章节|文档)来源\]\(([^)]+)\)", rendered)
    assert match is not None
    source_query = {key: values[-1] for key, values in parse_qs(urlsplit(match.group(1)).query).items()}
    assert source_query["ticker"] == "MSFT" and source_query["source_ticker"] == "AAPL"
    viewer = _build_app(prepared_deps, service, source_query).run()
    assert not viewer.exception and not viewer.error
    assert service.reads == [DocumentReference("AAPL", "mat_call", section_ref)]
    back_url = viewer.get("link_button")[0].proto.url
    back_query = {key: values[-1] for key, values in parse_qs(urlsplit(back_url).query).items()}
    assert back_query == origin_query
    restored = _build_app(prepared_deps, service, back_query)
    del restored.session_state["selected_ticker"]
    restored.session_state["chat_service"] = chat
    restored.run()
    assert not restored.exception and not restored.error
    assert restored.session_state["selected_ticker"] == "MSFT"
    assert restored.get("tab_container")[0].proto.tab_container.default_tab_index == 1
    assert chat.reads == [("original", 50), ("original", 50)]
    assert any("跨公司证据" in item.value for item in restored.markdown)
    assert chat.submissions == [] and service.calls == []
    assert restored.query_params["chat_tab_MSFT_session"] == ["original"]


@pytest.mark.parametrize("section_ref", [None, "sec_call"])
def test_main_routes_source_before_sidebar_and_preserves_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    prepared_deps: PreparedHostRuntimeDependencies, section_ref: str | None,
) -> None:
    """真实入口优先调用来源查看器，忽略侧栏 MSFT 身份并保留会话查询。

    参数:
        tmp_path: 工作区路径。
        monkeypatch: 仅替代侧栏交互并指定工作区。
        prepared_deps: 正常装配的完整依赖。
        section_ref: 文档级或章节级来源。
    返回值:
        无。
    异常:
        AssertionError: 入口未接线、先渲染侧栏、串公司或丢失会话时抛出。
    """
    sidebar = _SidebarRecorder(WatchlistItem("MSFT", "Microsoft", "", ""))
    monkeypatch.setattr("dayu.web.streamlit.components.sidebar.render_sidebar", sidebar.render)
    monkeypatch.setenv("DAYU_WORKSPACE", str(tmp_path))
    service = _SourceService()
    query = _SOURCE_QUERY.copy()
    if section_ref is None:
        del query["source_ref"]
    app = _build_app(prepared_deps, service, query).run()
    assert not app.exception and not app.error
    assert sidebar.roots == []
    assert service.reads == [DocumentReference("AAPL", "mat_call", section_ref)]
    assert service.calls == []
    assert app.header[0].value == "真实入口电话会议来源"
    assert app.selectbox[0].options == ["管理层答复 [sec_call]"]
    assert app.session_state["selected_ticker"] == "MSFT"
    assert app.query_params["chat_tab_AAPL_session"] == ["session-keep"]
    assert app.query_params["source_ticker"] == ["AAPL"]
    links = app.get("link_button")
    assert len(links) == 1
    assert parse_qs(urlsplit(links[0].proto.url).query) == {"chat_tab_AAPL_session": ["session-keep"]}
    if section_ref is None:
        assert any("文档级引用" in element.value for element in app.info)
        assert not app.text
    else:
        assert app.text[0].value == _SOURCE_BODY
    app.run()
    assert not app.exception and not app.error
    assert sidebar.roots == [] and len(service.reads) == 2
    assert service.calls == []
    assert app.query_params["chat_tab_AAPL_session"] == ["session-keep"]


def test_main_without_source_uses_normal_sidebar_and_welcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, prepared_deps: PreparedHostRuntimeDependencies,
) -> None:
    """没有来源参数时仍走普通侧栏与欢迎页，普通会话参数不触发来源路由。

    参数:
        tmp_path: 工作区路径。
        monkeypatch: 注入未选中股票的侧栏。
        prepared_deps: 正常装配的完整依赖。
    返回值:
        无。
    异常:
        AssertionError: 正常入口被来源分支截断时抛出。
    """
    sidebar = _SidebarRecorder()
    monkeypatch.setattr("dayu.web.streamlit.components.sidebar.render_sidebar", sidebar.render)
    monkeypatch.setenv("DAYU_WORKSPACE", str(tmp_path))
    service = _SourceService()
    app = _build_app(prepared_deps, service, {"chat_tab_AAPL_session": "session-keep"}).run()
    assert not app.exception and not app.error
    assert sidebar.roots == [tmp_path]
    assert app.title[0].value == "大禹 Agent"
    assert not app.get("link_button")
    assert service.reads == [] and service.calls == []
    assert app.query_params["chat_tab_AAPL_session"] == ["session-keep"]


@pytest.mark.parametrize("query", [
    {"source_ticker": "AAPL"},
    {"source_document": "mat_call"},
    {"source_ref": "sec_call"},
    {"source_ticker": "", "source_document": "mat_call"},
])
def test_main_incomplete_source_errors_without_sidebar_fallback(
    monkeypatch: pytest.MonkeyPatch, prepared_deps: PreparedHostRuntimeDependencies,
    query: dict[str, str],
) -> None:
    """残缺来源在主入口显示明确错误，不回退侧栏或读取默认公司。

    参数:
        monkeypatch: 记录侧栏是否被错误调用。
        prepared_deps: 正常装配的完整依赖。
        query: 缺少来源身份字段的链接参数。
    返回值:
        无。
    异常:
        AssertionError: 错误链接回退普通页面或发起读取时抛出。
    """
    sidebar = _SidebarRecorder(WatchlistItem("MSFT", "Microsoft", "", ""))
    monkeypatch.setattr("dayu.web.streamlit.components.sidebar.render_sidebar", sidebar.render)
    service = _SourceService()
    app = _build_app(prepared_deps, service, {**query, "chat_tab_AAPL_session": "session-keep"}).run()
    assert not app.exception
    assert len(app.error) == 1 and "缺少公司或文档标识" in app.error[0].value
    assert sidebar.roots == [] and not app.title and not app.header
    assert service.reads == [] and service.calls == []
    assert app.query_params["chat_tab_AAPL_session"] == ["session-keep"]


def test_main_source_without_dependencies_errors_without_sidebar_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """来源链接遇到不可用依赖直接报错，不能误入普通股票详情页面。

    参数:
        monkeypatch: 记录侧栏是否被错误调用。
    返回值:
        无。
    异常:
        AssertionError: 缺少服务时来源路由仍回退侧栏时抛出。
    """
    sidebar = _SidebarRecorder(WatchlistItem("MSFT", "Microsoft", "", ""))
    monkeypatch.setattr("dayu.web.streamlit.components.sidebar.render_sidebar", sidebar.render)
    service = _SourceService()
    app = _build_app(None, service, _SOURCE_QUERY).run()
    assert not app.exception
    assert len(app.error) == 1 and "服务不可用" in app.error[0].value
    assert sidebar.roots == [] and not app.title and not app.header
    assert service.reads == [] and service.calls == []
    assert app.query_params["chat_tab_AAPL_session"] == ["session-keep"]


@pytest.mark.parametrize("workspace_exists", [False, True])
def test_main_initializes_real_runtime_once_and_reuses_on_rerun(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, workspace_exists: bool,
) -> None:
    """首次进入真实初始化并创建工作区，后续重绘复用同一依赖。

    参数:
        tmp_path: 隔离的工作区父目录。
        monkeypatch: 指定工作区并记录侧栏；重绘时禁止再次初始化。
        workspace_exists: 首次进入前工作区是否已存在。
    返回值:
        无。
    异常:
        AssertionError: 初始化未完成、目录未创建或重绘重复初始化时抛出。
    """
    workspace = tmp_path / "fresh-workspace"
    if workspace_exists:
        workspace.mkdir()
    monkeypatch.setenv("DAYU_WORKSPACE", str(workspace))
    sidebar = _SidebarRecorder()
    monkeypatch.setattr("dayu.web.streamlit.components.sidebar.render_sidebar", sidebar.render)
    app = AppTest.from_string(_APP).run()
    assert not app.exception and not app.error
    assert workspace.is_dir()
    assert app.session_state["initialized"] is True
    deps = app.session_state["prepared_deps"]
    assert isinstance(deps, PreparedHostRuntimeDependencies)
    assert sidebar.roots == [workspace]
    assert app.title[0].value == "大禹 Agent"
    monkeypatch.setattr("dayu.web.streamlit_app._prepare_host_runtime", _fail_preparation)
    app.run()
    assert not app.exception and not app.error
    assert app.session_state["prepared_deps"] is deps
    assert sidebar.roots == [workspace, workspace]


def test_main_initialization_failure_stops_before_source_or_sidebar(monkeypatch: pytest.MonkeyPatch) -> None:
    """初始化异常显示原始错误并停止页面，不继续原文读取或侧栏渲染。

    参数:
        monkeypatch: 注入明确的装配失败并记录侧栏调用。
    返回值:
        无。
    异常:
        AssertionError: 初始化失败后仍执行页面或错误标记为已初始化时抛出。
    """
    monkeypatch.setattr("dayu.web.streamlit_app._prepare_host_runtime", _fail_preparation)
    sidebar = _SidebarRecorder(WatchlistItem("MSFT", "Microsoft", "", ""))
    monkeypatch.setattr("dayu.web.streamlit.components.sidebar.render_sidebar", sidebar.render)
    service = _SourceService()
    app = _build_app(None, service, _SOURCE_QUERY)
    del app.session_state["initialized"]
    del app.session_state["prepared_deps"]
    app.run()
    assert not app.exception
    assert len(app.error) == 1 and "服务初始化失败: 测试注入的启动装配失败" in app.error[0].value
    assert app.session_state["initialized"] is False
    assert app.session_state["prepared_deps"] is None
    assert sidebar.roots == [] and service.reads == [] and service.calls == []
    assert not app.title and not app.header
    assert app.query_params["chat_tab_AAPL_session"] == ["session-keep"]


def test_main_selected_stock_constructs_services_and_caches_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, prepared_deps: PreparedHostRuntimeDependencies,
) -> None:
    """普通选股路径构造真实三个服务、注入场景模型并跨重绘复用缓存。

    参数:
        tmp_path: 隔离的工作区。
        monkeypatch: 仅替代侧栏和详情页渲染边界。
        prepared_deps: 真实装配的完整依赖。
    返回值:
        无。
    异常:
        AssertionError: 服务装配错误、身份未保持或允许模型过滤错误时抛出。
    """
    stock = WatchlistItem("MSFT", "Microsoft", "", "")
    sidebar = _SidebarRecorder(stock)
    details = _DetailRecorder()
    monkeypatch.setattr("dayu.web.streamlit.components.sidebar.render_sidebar", sidebar.render)
    monkeypatch.setattr("dayu.web.streamlit.pages.main_page.render_stock_detail_page", details.render)
    monkeypatch.setenv("DAYU_WORKSPACE", str(tmp_path))
    app = AppTest.from_string(_APP)
    app.session_state["initialized"] = True
    app.session_state["prepared_deps"] = prepared_deps
    app.query_params["chat_tab_MSFT_session"] = "existing-msft-session"
    app.run()
    assert not app.exception and not app.error
    assert sidebar.roots == [tmp_path] and len(details.calls) == 1
    first = details.calls[0]
    assert first.stock == stock and first.workspace_root == tmp_path
    assert isinstance(first.fins_service, FinsService)
    assert first.fins_service.host is prepared_deps.host
    assert first.fins_service.fins_runtime is prepared_deps.fins_runtime
    preparer = prepared_deps.scene_execution_acceptance_preparer
    assert set(first.model_names) == (
        set(preparer.model_catalog.load_models()) & set(preparer.read_scene_definition("interactive").model.allowed_names)
    )
    assert app.session_state["fins_service"] is first.fins_service
    assert app.session_state["chat_service"] is first.chat_service
    assert app.session_state["write_service"] is first.write_service
    app.run()
    assert not app.exception and not app.error
    assert len(details.calls) == 2 and sidebar.roots == [tmp_path, tmp_path]
    second = details.calls[1]
    assert second.fins_service is first.fins_service
    assert second.chat_service is first.chat_service
    assert second.write_service is first.write_service
    assert app.query_params["chat_tab_MSFT_session"] == ["existing-msft-session"]


def test_main_selected_stock_without_dependencies_shows_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """普通选股但依赖不可用时显示股票详情错误，不构造或调用详情页。

    参数:
        monkeypatch: 提供已选股票并记录详情页是否被错误调用。
    返回值:
        无。
    异常:
        AssertionError: 缺失依赖仍进入详情页时抛出。
    """
    sidebar = _SidebarRecorder(WatchlistItem("MSFT", "Microsoft", "", ""))
    details = _DetailRecorder()
    monkeypatch.setattr("dayu.web.streamlit.components.sidebar.render_sidebar", sidebar.render)
    monkeypatch.setattr("dayu.web.streamlit.pages.main_page.render_stock_detail_page", details.render)
    service = _SourceService()
    app = _build_app(None, service, {}).run()
    assert not app.exception
    assert len(app.error) == 1 and "无法展示股票详情" in app.error[0].value
    assert len(sidebar.roots) == 1 and details.calls == []
    assert service.reads == [] and service.calls == []
