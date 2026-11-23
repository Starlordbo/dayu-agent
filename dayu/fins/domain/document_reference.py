"""只读原文定位视图：以公司、文档和机器 ref 定位，不暴露仓储路径。"""

from __future__ import annotations

from dataclasses import dataclass

from dayu.fins.earnings_call_passages import EarningsCallPassage


@dataclass(frozen=True)
class DocumentReference:
    """请求文档或章节；没有 ref 时只请求文档级目录。"""

    ticker: str
    document_id: str
    section_ref: str | None = None


@dataclass(frozen=True)
class DocumentSourceInfo:
    """从源仓储及公司仓储读取的真实文档头；未知字段不编造。"""

    ticker: str
    document_id: str
    company_name: str | None
    material_name: str | None
    form_type: str | None
    fiscal_year: int | None
    fiscal_period: str | None
    filing_date: str | None


@dataclass(frozen=True)
class SourceSectionSummary:
    """目录中的机器 ref 与原章节标题。"""

    ref: str
    title: str | None


@dataclass(frozen=True)
class SourceSectionView:
    """完整章节正文及可核验的纪要发言片段，不用角色代替原文。"""

    ref: str
    title: str | None
    content: str
    contains_full_text: bool
    page_range: tuple[int, ...]
    passages: tuple[EarningsCallPassage, ...]


@dataclass(frozen=True)
class DocumentReferenceView:
    """只读 Service 聚合的文档头、目录与选中章节。"""

    info: DocumentSourceInfo
    sections: tuple[SourceSectionSummary, ...]
    selected_section: SourceSectionView | None
