"""通过真实仓储、处理器与正文验证纪要四个读取入口的双向公司隔离。"""

from __future__ import annotations

# pyright: strict

from dataclasses import dataclass
from io import BytesIO
import json
from pathlib import Path

from docling_core.types.doc.document import DoclingDocument
from docling_core.types.doc.labels import DocItemLabel
import pytest

from dayu.engine.tool_errors import ToolBusinessError
from dayu.engine.tools.error_contract import ErrorCode
from dayu.fins.domain.document_models import CompanyMeta, SourceDocumentUpsertRequest, SourceHandle
from dayu.fins.domain.enums import SourceKind
from dayu.fins.processors.registry import build_fins_processor_registry
from dayu.fins.tools.service import FinsToolService
from tests.fins.storage_testkit import FsStorageTestContext, build_fs_storage_test_context

pytestmark = pytest.mark.integration

_SHARED_DOCUMENT_ID = "2026Q3_earnings_call"
_MATERIAL_NAME = "2026 Q3 Earnings Call"
_FILENAME = "transcript_docling.json"
_FORM_TYPE = "EARNINGS_CALL"
_DOCUMENT_TYPE = "earnings_call"
_FISCAL_YEAR = 2026
_FISCAL_PERIOD = "Q3"
_UPDATED_AT = "2026-11-24T00:00:00Z"


@dataclass(frozen=True)
class _Transcript:
    """一家公司独有的测试身份与正文标记，不含真实业绩数据。"""

    ticker: str
    company_name: str
    document_id: str
    keyword: str
    speaker: str


_GOOGL = _Transcript("GOOGL", "Alphabet", "googl_2026q3_call", "quartzcloudsignal prismsearchsignal", "Google Speaker")
_AMZN = _Transcript("AMZN", "Amazon", "amzn_2026q3_call", "copperretailsignal bronzeordersignal", "Amazon Speaker")
_DIRECTIONS = [(_GOOGL, _AMZN), (_AMZN, _GOOGL)]
_DIRECTION_IDS = ["GOOGL_context", "AMZN_context"]


def _store_transcript(storage: FsStorageTestContext, transcript: _Transcript, document_id: str) -> None:
    """经 blob 与 source 仓储写入带明确管理层身份的纪要正文。

    参数:
        storage: 隔离的真实文件系统仓储集合。
        transcript: 公司身份及唯一检索标记。
        document_id: 当前公司下的文档编号，允许与另一家公司相同。
    返回值:
        无。
    异常:
        OSError: 仓储写入失败。
    """
    document = DoclingDocument(name=_MATERIAL_NAME)
    document.add_heading(text=f"{transcript.company_name} Management Remarks", level=1)
    document.add_text(label=DocItemLabel.TEXT, text=f"{transcript.speaker} - Chief Executive Officer")
    document.add_text(
        label=DocItemLabel.TEXT,
        text=f"{transcript.company_name} reported {transcript.keyword} during the quarter.",
    )
    file_meta = storage.blob_repository.store_file(
        SourceHandle(transcript.ticker, document_id, SourceKind.MATERIAL.value),
        _FILENAME,
        BytesIO(json.dumps(document.export_to_dict()).encode("utf-8")),
        content_type="application/json",
    )
    storage.source_repository.create_source_document(
        SourceDocumentUpsertRequest(
            ticker=transcript.ticker,
            document_id=document_id,
            internal_document_id=document_id,
            form_type=_FORM_TYPE,
            primary_document=_FILENAME,
            meta={
                "material_name": _MATERIAL_NAME,
                "fiscal_year": _FISCAL_YEAR,
                "fiscal_period": _FISCAL_PERIOD,
            },
            files=[file_meta],
        ),
        SourceKind.MATERIAL,
    )


@pytest.fixture
def transcript_service(tmp_path: Path) -> FinsToolService:
    """装配两家公司各有独有 ID 与相同 ID 纪要的真实读取服务。

    参数:
        tmp_path: pytest 提供的独立工作区。
    返回值:
        使用生产仓储和默认生产处理器注册表的财报工具服务。
    异常:
        OSError: 公司元数据或纪要写入失败。
        RuntimeError: 处理器注册失败。
    """
    storage = build_fs_storage_test_context(tmp_path)
    for transcript in (_GOOGL, _AMZN):
        storage.company_repository.upsert_company_meta(
            CompanyMeta(
                company_id=transcript.ticker,
                company_name=transcript.company_name,
                ticker=transcript.ticker,
                market="US",
                resolver_version="test",
                updated_at=_UPDATED_AT,
            )
        )
        for document_id in (transcript.document_id, _SHARED_DOCUMENT_ID):
            _store_transcript(storage, transcript, document_id)
    return FinsToolService(
        company_repository=storage.company_repository,
        source_repository=storage.source_repository,
        processed_repository=storage.processed_repository,
        processor_registry=build_fins_processor_registry(),
    )


@pytest.mark.parametrize(("current", "other"), _DIRECTIONS, ids=_DIRECTION_IDS)
def test_list_documents_only_lists_current_company_materials(
    transcript_service: FinsToolService, current: _Transcript, other: _Transcript,
) -> None:
    """无过滤和纪要过滤都只能列出指定公司的文档及推荐项。

    参数:
        transcript_service: 两家公司共用的真实读取服务。
        current: 当前公司。
        other: 不应出现在目录中的另一家公司。
    返回值:
        无。
    异常:
        AssertionError: 目录、公司身份或推荐项跨公司。
    """
    expected_ids = {current.document_id, _SHARED_DOCUMENT_ID}
    for document_types in (None, [_DOCUMENT_TYPE]):
        result = transcript_service.list_documents(
            ticker=current.ticker, document_types=document_types,
            fiscal_years=[_FISCAL_YEAR], fiscal_periods=[_FISCAL_PERIOD],
        )
        assert result["company"]["ticker"] == current.ticker
        assert result["company"]["name"] == current.company_name
        assert {item["document_id"] for item in result["documents"]} == expected_ids
        assert all(item["source_kind"] == SourceKind.MATERIAL.value for item in result["documents"])
        assert all(item["document_type"] == _DOCUMENT_TYPE for item in result["documents"])
        assert all(value is None or value in expected_ids for value in result["recommended_documents"].values())
        assert other.document_id not in str(result)


@pytest.mark.parametrize(("current", "other"), _DIRECTIONS, ids=_DIRECTION_IDS)
@pytest.mark.parametrize("shared_id", [False, True], ids=["distinct_ids", "same_id"])
@pytest.mark.parametrize("mode", ["auto", "exact", "keyword", "semantic"])
@pytest.mark.parametrize("batch", [False, True], ids=["single_query", "batch_queries"])
def test_search_document_never_returns_other_company_keyword(
    transcript_service: FinsToolService, current: _Transcript, other: _Transcript,
    shared_id: bool, mode: str, batch: bool,
) -> None:
    """本公司关键词能命中，对方关键词不能命中，包括同 ID 和热缓存。

    参数:
        transcript_service: 两家公司共用的真实读取服务。
        current: 首先读取并最终回读的公司。
        other: 交替读取的另一家公司。
        shared_id: 是否使用两家公司相同的文档编号。
        mode: 生产检索模式。
        batch: 是否同时验证批量查询分支。
    返回值:
        无。
    异常:
        AssertionError: 正向对照未命中或反向检索泄漏其他公司正文。
    """
    # 先后读取 A、B、A；相同编号时仍须各自读取正文，不能复用另一公司的缓存。
    for owner, foreign in ((current, other), (other, current), (current, other)):
        document_id = _SHARED_DOCUMENT_ID if shared_id else owner.document_id
        positive = transcript_service.search_document(
            ticker=owner.ticker, document_id=document_id, mode=mode,
            query=None if batch else owner.keyword,
            queries=[owner.keyword, " ".join(reversed(owner.keyword.split()))] if batch else None,
        )
        assert positive["total_matches"] > 0
        assert owner.keyword in str(positive["matches"])
        assert foreign.keyword not in str(positive["matches"])
        assert positive["ticker"] == owner.ticker
        assert positive["citation"]["ticker"] == owner.ticker
        assert positive["citation"]["document_id"] == document_id

        negative = transcript_service.search_document(
            ticker=owner.ticker, document_id=document_id, mode=mode,
            query=None if batch else foreign.keyword,
            queries=[foreign.keyword, " ".join(reversed(foreign.keyword.split()))] if batch else None,
        )
        assert negative["total_matches"] == 0
        assert negative["matches"] == []
        assert negative["ticker"] == owner.ticker
        assert negative["document_id"] == document_id
        assert negative["citation"]["ticker"] == owner.ticker
        if batch:
            assert positive.get("queries") == [owner.keyword, " ".join(reversed(owner.keyword.split()))]
            assert negative.get("queries") == [foreign.keyword, " ".join(reversed(foreign.keyword.split()))]


@pytest.mark.parametrize(("current", "other"), _DIRECTIONS, ids=_DIRECTION_IDS)
@pytest.mark.parametrize("shared_id", [False, True], ids=["distinct_ids", "same_id"])
def test_sections_and_content_remain_company_scoped_after_cache_warmup(
    transcript_service: FinsToolService, current: _Transcript, other: _Transcript, shared_id: bool,
) -> None:
    """章节、正文、管理层片段与引用在交替读取后仍属于指定公司。

    参数:
        transcript_service: 两家公司共用的真实读取服务。
        current: 首先读取并最终回读的公司。
        other: 用于预热另一个缓存项的公司。
        shared_id: 是否使用两家公司相同的文档编号。
    返回值:
        无。
    异常:
        AssertionError: 章节、正文、片段或引用跨公司。
    """
    for owner, foreign in ((current, other), (other, current), (current, other)):
        document_id = _SHARED_DOCUMENT_ID if shared_id else owner.document_id
        sections = transcript_service.get_document_sections(ticker=owner.ticker, document_id=document_id)
        assert sections["ticker"] == owner.ticker
        assert sections["document_id"] == document_id
        assert sections["citation"]["ticker"] == owner.ticker
        assert sections["sections"]
        assert owner.company_name in str(sections["sections"])
        assert foreign.company_name not in str(sections["sections"])
        for section in sections["sections"]:
            content = transcript_service.read_section(
                ticker=owner.ticker, document_id=document_id, ref=section["ref"],
            )
            assert owner.keyword in content["content"]
            assert foreign.keyword not in content["content"]
            assert content["ticker"] == owner.ticker
            assert content["document_id"] == document_id
            assert content["citation"]["ticker"] == owner.ticker
            assert content["citation"]["document_id"] == document_id
            passages = content["passages"]
            assert "".join(passage["text"] for passage in passages) == content["content"]
            assert any(passage["speaker"] == owner.speaker and passage["role"] == "management" for passage in passages)
            assert all(passage["speaker"] != foreign.speaker for passage in passages)


@pytest.mark.parametrize(("current", "other"), _DIRECTIONS, ids=_DIRECTION_IDS)
@pytest.mark.parametrize("warm_cache", [False, True], ids=["cold_cache", "warm_cache"])
def test_foreign_document_id_is_rejected_by_all_document_read_entries(
    transcript_service: FinsToolService, current: _Transcript, other: _Transcript, warm_cache: bool,
) -> None:
    """直接传入对方独有文档 ID 时，三个文档读取入口都拒绝访问。

    参数:
        transcript_service: 两家公司共用的真实读取服务。
        current: 发起错误文档读取的公司。
        other: 错误文档 ID 的真实归属公司。
        warm_cache: 是否先缓存文档所属公司的真实处理器。
    返回值:
        无。
    异常:
        AssertionError: 读取未拒绝或未报告确定的归属错误。
    """
    if warm_cache:
        transcript_service.search_document(ticker=other.ticker, document_id=other.document_id, query=other.keyword)
    with pytest.raises(ToolBusinessError) as search_error:
        transcript_service.search_document(ticker=current.ticker, document_id=other.document_id, query=other.keyword)
    with pytest.raises(ToolBusinessError) as sections_error:
        transcript_service.get_document_sections(ticker=current.ticker, document_id=other.document_id)
    with pytest.raises(ToolBusinessError) as read_error:
        transcript_service.read_section(ticker=current.ticker, document_id=other.document_id, ref="s_0001")
    for error in (search_error.value, sections_error.value, read_error.value):
        assert error.code == ErrorCode.INVALID_ARGUMENT
        assert f"属于 {other.ticker}" in error.message
        assert f"ticker={current.ticker}" in error.message
        assert other.keyword not in error.message
