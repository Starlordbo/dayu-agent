"""共享原文章节查看器：只经 Service 读取，不直接接触仓储或本地路径。"""

from __future__ import annotations

from collections.abc import Mapping
from urllib.parse import urlencode

import streamlit as st

from dayu.fins.domain.document_reference import DocumentReference, DocumentReferenceView
from dayu.services.protocols import FinsServiceProtocol

_ROLE_LABELS = {"analyst": "分析师", "management": "管理层", "operator": "主持人", "unknown": "未标注"}
_KIND_LABELS = {"question": "问句", "statement": "陈述", "unknown": "未标注"}


def reference_from_query(query: Mapping[str, str]) -> DocumentReference | None:
    """读取可刷新恢复的引用身份；残缺链接明确失败，不回退当前侧栏公司。

    Args:
        query: 当前 URL 参数。
    Returns:
        原文请求；没有来源参数时为 None。
    Raises:
        ValueError: 来源参数残缺。
    """
    if not any(key.startswith("source_") for key in query):
        return None
    ticker, document = query.get("source_ticker"), query.get("source_document")
    if not ticker or not document:
        raise ValueError("来源链接缺少公司或文档标识")
    return DocumentReference(ticker, document, query.get("source_ref"))


def _render_source_body(view: DocumentReferenceView) -> None:
    """展示完整正文、原文范围和身份依据，不用问题内容冒充公司指引。

    Args:
        view: Service 已解析的原文视图。
    Returns:
        无。
    Raises:
        无。
    """
    section = view.selected_section
    if section is None:
        st.info("文档级引用：尚未定位到具体章节，请从目录选择；不得据此声称已有章节级证据。")
        return
    st.subheader("文档级 / 全文" if section.contains_full_text else "章节级原文")
    if section.title and not section.contains_full_text:
        st.write(section.title)
    st.caption(f"ref: {section.ref}")
    if section.page_range:
        st.caption("页码：" + "–".join(str(page) for page in section.page_range))
    st.text(section.content)
    if section.passages:
        st.caption(
            "发言标注来自明确原文身份行；问句不等于管理层回答或公司指引，无法确认时显示未标注。范围为正文字符偏移。"
        )
        st.dataframe(
            [
                {
                    "范围": f"{passage.start}:{passage.end}",
                    "发言人": passage.speaker or "未标注",
                    "身份": _ROLE_LABELS[passage.role],
                    "发言形式": _KIND_LABELS[passage.kind],
                    "原文身份依据": passage.evidence or "未标注",
                }
                for passage in section.passages
            ],
            hide_index=True,
            width="stretch",
        )


def render_source_viewer(
    *, service: FinsServiceProtocol, request: DocumentReference, query_params: Mapping[str, str]
) -> None:
    """从指定身份显示文档头、可选择目录及上下文，刷新不触发上传或聊天。

    Args:
        service: 财报只读业务入口。
        request: 来源链接中的公司、文档和可选章节 ref。
        query_params: 当前 URL 参数，保留非来源参数以便回到聊天。
    Returns:
        无。
    Raises:
        无；读取失败显示明确错误，不伪造原文。
    """
    back_params = {key: value for key, value in query_params.items() if not key.startswith("source_")}
    st.link_button("返回分析", "?" + urlencode(back_params))
    try:
        view = service.resolve_document_reference(request)
        info = view.info
        st.header(info.material_name or "文档标题未披露")
        st.write(f"公司：{info.company_name or '未披露'}（{info.ticker}）")
        st.write(
            f"材料类型：{info.form_type or '未披露'} · 财季：{info.fiscal_year or '未披露'} {info.fiscal_period or ''}"
        )
        st.caption(f"文档：{info.document_id} · 日期：{info.filing_date or '未披露'}")
        refs = [section.ref for section in view.sections]
        titles = {section.ref: f"{section.title or '标题未披露'} [{section.ref}]" for section in view.sections}
        selected = st.selectbox(
            "原文目录",
            refs,
            index=refs.index(request.section_ref) if request.section_ref in refs else None,
            format_func=titles.__getitem__,
            key=f"source_section_{info.ticker}_{info.document_id}_{request.section_ref}",
        )
        if selected is not None and selected != request.section_ref:
            st.query_params["source_ref"] = selected
            # URL 与 widget 身份必须在下一次交互前同步，否则新 key 会吞掉第二次选择。
            st.rerun()
        _render_source_body(view)
    except Exception as exc:
        st.error(f"原文读取失败：{exc}")
