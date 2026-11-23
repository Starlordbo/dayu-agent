"""验证引用查询参数到只读查看器的无头交互；不替代真实浏览器验收。"""

from __future__ import annotations

# pyright: strict

from dataclasses import dataclass, field, replace
from typing import cast

import pytest
from streamlit.testing.v1 import AppTest

from dayu.fins.domain.document_models import FilingSummary
from dayu.fins.domain.document_reference import (
    DocumentReference,
    DocumentReferenceView,
    DocumentSourceInfo,
    SourceSectionSummary,
    SourceSectionView,
)
from dayu.fins.earnings_call_passages import parse_earnings_call_passages
from dayu.services.contracts import FinsSubmission, FinsSubmitRequest
from dayu.services.protocols import FinsServiceProtocol
from dayu.web.streamlit.pages.source_viewer import reference_from_query

_APP = """
from typing import cast
import streamlit as st
from dayu.services.protocols import FinsServiceProtocol
from dayu.web.streamlit.pages.source_viewer import reference_from_query, render_source_viewer
try:
    query = st.query_params.to_dict()
    reference = reference_from_query(query)
    if reference is not None:
        render_source_viewer(service=cast(FinsServiceProtocol, st.session_state["service"]),
                             request=reference, query_params=query)
except ValueError as error:
    st.error(str(error))
"""
_INFO = DocumentSourceInfo(
    "AAPL", "mat_0001", "Reference Company", "Q4 2023 Earnings Call", "EARNINGS_CALL", 2023, "Q4", "2024-02-01"
)
_QA_BODY = (
    "Alex Smith - Analyst\nCould revenue reach 100 million dollars?\n"
    "Jane Doe - Chief Executive Officer\nWe have not provided that revenue forecast.\n"
    "**Unidentified Participant**\nThank you.\n"
)
_SECTIONS = (SourceSectionSummary("qa", "Questions and Answers"), SourceSectionSummary("full", None))


@dataclass
class _RecordingReadService:
    """完整满足服务协议的最小只读替身，记录请求并拒绝命令入口。"""

    info: DocumentSourceInfo = _INFO
    requests: list[DocumentReference] = field(default_factory=list[DocumentReference])
    submit_calls: int = 0
    failure: str | None = None

    def resolve_document_reference(self, request: DocumentReference) -> DocumentReferenceView:
        """记录准确定位，并返回对应文档、问答或全文视图。

        Args:
            request: 查看器从 URL 或目录构造的请求。
        Returns:
            区分文档范围、章节范围与全文的固定 DTO。
        Raises:
            ValueError: 测试要求模拟错误或章节 ref 不存在。
            FileNotFoundError: 公司或文档不属于该测试材料。
        """
        self.requests.append(request)
        if self.failure is not None:
            raise ValueError(self.failure)
        if (request.ticker, request.document_id) != (self.info.ticker, self.info.document_id):
            raise FileNotFoundError("请求的公司文档不存在")
        section = None
        if request.section_ref is not None:
            if request.section_ref not in {summary.ref for summary in _SECTIONS}:
                raise ValueError("无效章节 ref")
            full_text = request.section_ref == "full"
            section = SourceSectionView(
                ref=request.section_ref,
                title=None if full_text else "Questions and Answers",
                content=_QA_BODY,
                contains_full_text=full_text,
                page_range=() if full_text else (2, 3),
                passages=parse_earnings_call_passages(_QA_BODY),
            )
        return DocumentReferenceView(self.info, _SECTIONS, section)

    def submit(self, request: FinsSubmitRequest) -> FinsSubmission:
        """拒绝查看原文时提交上传、处理或模型命令。

        Args:
            request: 意外提交的命令。
        Returns:
            不返回。
        Raises:
            AssertionError: 查看器触发非只读操作。
        """
        self.submit_calls += 1
        raise AssertionError(f"查看器不应提交：{request.command.name}")

    def list_materials(self, ticker: str) -> list[DocumentSourceInfo]:
        """拒绝查看器查询无关材料列表。

        Args:
            ticker: 意外请求的公司。
        Returns:
            不返回。
        Raises:
            AssertionError: 查看器偏离明确引用定位。
        """
        raise AssertionError(f"查看器不应查询材料列表：{ticker}")

    def list_filings(self, ticker: str) -> list[FilingSummary]:
        """拒绝查看器查询无关财报列表。

        Args:
            ticker: 意外请求的公司。
        Returns:
            不返回。
        Raises:
            AssertionError: 查看器偏离明确引用定位。
        """
        raise AssertionError(f"查看器不应查询财报列表：{ticker}")


def _make_app(service: _RecordingReadService, section_ref: str | None = None) -> AppTest:
    """从引用 URL 参数初始化真实 Streamlit 控件树。

    Args:
        service: 记录只读请求的 typed 服务替身。
        section_ref: 可选章节 ref；为空时请求文档级视图。
    Returns:
        尚未运行的 AppTest 实例。
    Raises:
        AssertionError: 替身没有实现完整服务协议。
    """
    assert isinstance(service, FinsServiceProtocol)
    app = AppTest.from_string(_APP)
    app.session_state["service"] = service
    app.query_params["source_ticker"] = service.info.ticker
    app.query_params["source_document"] = service.info.document_id
    app.query_params["ticker"] = "MSFT"
    app.query_params["session"] = "session-keep"
    if section_ref is not None:
        app.query_params["source_ref"] = section_ref
    return app


@pytest.mark.parametrize(
    "query,expected",
    [
        ({}, None),
        ({"ticker": "MSFT"}, None),
        ({"source_ticker": "AAPL", "source_document": "mat_0001"}, DocumentReference("AAPL", "mat_0001")),
        (
            {"ticker": "MSFT", "source_ticker": "AAPL", "source_document": "mat_0001", "source_ref": "qa"},
            DocumentReference("AAPL", "mat_0001", "qa"),
        ),
    ],
)
def test_query_identity_is_explicit(query: dict[str, str], expected: DocumentReference | None) -> None:
    """引用身份来自完整来源参数，不从当前侧栏公司补齐或覆盖。

    Args:
        query: 原始查询参数。
        expected: 期望精确引用或无引用。
    Returns:
        无。
    Raises:
        AssertionError: 查询身份被猜测或替换。
    """
    assert reference_from_query(query) == expected


@pytest.mark.parametrize(
    "query",
    [
        {"source_ref": "qa"},
        {"source_ticker": "AAPL"},
        {"source_document": "mat_0001", "ticker": "AAPL"},
        {"source_ticker": "", "source_document": "mat_0001"},
        {"source_ticker": "AAPL", "source_document": ""},
    ],
)
def test_partial_query_fails_without_sidebar_fallback(query: dict[str, str]) -> None:
    """残缺链接明确失败，即使旁边存在公司参数也不进行隐式补全。

    Args:
        query: 缺少公司或文档的来源参数。
    Returns:
        无。
    Raises:
        AssertionError: 残缺参数被接受。
    """
    with pytest.raises(ValueError, match="缺少公司或文档"):
        reference_from_query(query)


def test_query_to_section_view_and_refresh_are_read_only() -> None:
    """带章节引用展示真实 DTO 头与完整正文，刷新保持定位且不提交命令。

    Args:
        无。
    Returns:
        无。
    Raises:
        AssertionError: 页面定位、来源上下文、正文或只读契约错误。
    """
    service = _RecordingReadService()
    app = _make_app(service, "qa").run()
    assert not app.exception and not app.error
    assert service.requests == [DocumentReference("AAPL", "mat_0001", "qa")]
    assert app.header[0].value == "Q4 2023 Earnings Call"
    assert any("Reference Company（AAPL）" in item.value for item in app.markdown)
    assert any("2023 Q4" in item.value for item in app.markdown)
    assert app.subheader[0].value == "章节级原文"
    assert app.text[0].value == _QA_BODY.strip()
    assert any("ref: qa" in item.value for item in app.caption)
    assert any("页码：2–3" in item.value for item in app.caption)
    app.run()
    assert not app.exception and not app.error
    assert service.requests == [DocumentReference("AAPL", "mat_0001", "qa")] * 2
    assert service.submit_calls == 0
    assert app.text[0].value == _QA_BODY.strip()


def test_document_scope_then_select_section_updates_query() -> None:
    """文档级引用先显示范围警告，选择真实目录条目后请求对应章节。

    Args:
        无。
    Returns:
        无。
    Raises:
        AssertionError: 未选择章节就展示章节证据或目录选择未更新定位。
    """
    service = _RecordingReadService()
    app = _make_app(service).run()
    assert not app.exception and not app.error
    assert len(app.info) == 1 and "文档级引用" in app.info[0].value
    assert not app.text
    assert app.selectbox[0].value is None
    assert "Questions and Answers [qa]" in app.selectbox[0].options
    app.selectbox[0].set_value("qa").run()
    assert not app.exception and not app.error
    assert service.requests[-1] == DocumentReference("AAPL", "mat_0001", "qa")
    assert app.query_params["source_ref"] == ["qa"]
    assert app.text[0].value == _QA_BODY.strip()
    app.run()
    assert service.requests[-1] == DocumentReference("AAPL", "mat_0001", "qa")
    assert service.submit_calls == 0


@pytest.mark.parametrize(
    "initial_ref,first_ref,second_ref",
    [(None, "qa", "full"), ("qa", "full", "qa")],
)
def test_consecutive_section_selections_do_not_drop_second_click(
    initial_ref: str | None, first_ref: str, second_ref: str
) -> None:
    """连续切换目录不能因 URL 与控件标识不同步而丢失第二次选择。

    Args:
        initial_ref: 页面首次打开时的文档级或章节级引用。
        first_ref: 第一次点击选择的章节。
        second_ref: 紧接着点击选择的另一个章节。
    Returns:
        无。
    Raises:
        AssertionError: 第二次点击后目录、URL、正文范围或服务定位仍停留在上一章。
    """
    service = _RecordingReadService()
    app = _make_app(service, initial_ref).run()
    app.selectbox[0].set_value(first_ref).run()
    assert not app.exception and not app.error
    assert app.query_params["source_ref"] == [first_ref]
    assert app.selectbox[0].value == first_ref
    assert service.requests[-1] == DocumentReference("AAPL", "mat_0001", first_ref)

    # 只运行点击本身产生的重跑，不额外刷新来掩盖旧控件 key 的事件丢失。
    app.selectbox[0].set_value(second_ref).run()
    assert not app.exception and not app.error
    assert app.query_params["source_ref"] == [second_ref]
    assert app.selectbox[0].value == second_ref
    assert service.requests[-1] == DocumentReference("AAPL", "mat_0001", second_ref)
    assert any(item.value == f"ref: {second_ref}" for item in app.caption)
    assert app.subheader[0].value == ("文档级 / 全文" if second_ref == "full" else "章节级原文")
    assert service.submit_calls == 0


def test_roles_evidence_and_question_disclaimer_are_visible() -> None:
    """分析师问句、管理层陈述及未知发言显示各自原始身份依据。

    Args:
        无。
    Returns:
        无。
    Raises:
        AssertionError: 身份或证据列混淆，缺少问句不等于指引的提示。
    """
    service = _RecordingReadService()
    app = _make_app(service, "qa").run()
    assert not app.exception and not app.error
    assert any("问句不等于管理层回答或公司指引" in item.value for item in app.caption)
    frame = app.dataframe[0].value
    columns = ("范围", "发言人", "身份", "发言形式", "原文身份依据")
    records = [{column: cast(str, frame.at[index, column]) for column in columns} for index in range(len(frame.index))]
    assert [row["身份"] for row in records] == ["分析师", "管理层", "未标注"]
    assert [row["发言形式"] for row in records] == ["问句", "陈述", "陈述"]
    assert [row["原文身份依据"] for row in records] == [
        "Alex Smith - Analyst",
        "Jane Doe - Chief Executive Officer",
        "未标注",
    ]
    assert records[0]["发言人"] == "Alex Smith"
    assert all(":" in row["范围"] for row in records)
    assert service.submit_calls == 0


def test_no_heading_full_text_has_document_scope_label() -> None:
    """无章节标题的全文回退明确标示文档范围且保留完整正文。

    Args:
        无。
    Returns:
        无。
    Raises:
        AssertionError: 全文被误标为章节或丢失正文。
    """
    service = _RecordingReadService(info=replace(_INFO, material_name=None, company_name=None))
    app = _make_app(service, "full").run()
    assert not app.exception and not app.error
    assert app.header[0].value == "文档标题未披露"
    assert app.subheader[0].value == "文档级 / 全文"
    assert app.text[0].value == _QA_BODY.strip()
    assert "标题未披露 [full]" in app.selectbox[0].options
    assert any("未披露（AAPL）" in item.value for item in app.markdown)
    assert service.submit_calls == 0


@pytest.mark.parametrize("message", ["原文已删除", "原文尚未完整入库", "无效章节 ref", "原文定位参数非法"])
def test_read_error_is_explicit_without_fake_body(message: str) -> None:
    """读取错误明确展示且不伪造正文、不触发命令补救。

    Args:
        message: 服务返回的可解释错误。
    Returns:
        无。
    Raises:
        AssertionError: 错误被吞掉或出现伪造正文。
    """
    service = _RecordingReadService(failure=message)
    app = _make_app(service, "qa").run()
    assert not app.exception
    assert len(app.error) == 1
    assert app.error[0].value == f"原文读取失败：{message}"
    assert not app.text and not app.dataframe
    assert service.submit_calls == 0


def test_partial_link_stops_before_service_read() -> None:
    """残缺 URL 在实际页面入口失败，不借用侧栏公司或提交命令。

    Args:
        无。
    Returns:
        无。
    Raises:
        AssertionError: 不完整引用仍进入业务读取。
    """
    service = _RecordingReadService()
    app = _make_app(service)
    del app.query_params["source_document"]
    app.run()
    assert not app.exception
    assert "缺少公司或文档" in app.error[0].value
    assert not app.text
    assert service.requests == []
    assert service.submit_calls == 0


def test_invalid_section_link_is_not_replaced_by_first_section() -> None:
    """非法章节参数交给服务拒绝，不静默显示第一章节或全文。

    Args:
        无。
    Returns:
        无。
    Raises:
        AssertionError: 非法引用被当成有效章节展示。
    """
    service = _RecordingReadService()
    app = _make_app(service, "../etc/passwd").run()
    assert not app.exception
    assert service.requests == [DocumentReference("AAPL", "mat_0001", "../etc/passwd")]
    assert "无效章节 ref" in app.error[0].value
    assert not app.text
    assert service.submit_calls == 0
