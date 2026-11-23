"""把电话会议纪要批量调度接到既有 Docling 上传服务。

分层约束：
- 批量决策留在 `earnings_call_batch`，本模块只负责“决策 → 既有上传命令”的适配；
- 仓储读取走 `SourceDocumentRepositoryProtocol`，写入仍由 `DoclingUploadService` 完成；
- 不复制转换、指纹、upsert 逻辑，全部复用既有实现。

对应验收项见 `docs/p2-verification.md` 的 S1–S4、U8。
"""

from __future__ import annotations

from typing import Iterable, Mapping, Optional

from dayu.fins.domain.enums import SourceKind
from dayu.fins.pipelines.docling_upload_service import DoclingUploadService
from dayu.fins.pipelines.earnings_call_batch import (
    DECISION_CONFLICT_NEW_MATERIAL,
    DECISION_CREATE,
    DECISION_RESUME_PROCESSING,
    EARNINGS_CALL_FORM_TYPE,
    BatchPlanItem,
    ExistingMaterial,
)
from dayu.fins.storage import SourceDocumentRepositoryProtocol

# 材料元数据中标记“上传来源”的固定值。
_INGEST_METHOD_UPLOAD: str = "upload"

# 决策到上传服务 action 的映射：冲突另存与新建都走 create，恢复处理走 update。
_ACTION_BY_DECISION: Mapping[str, str] = {
    DECISION_CREATE: "create",
    DECISION_RESUME_PROCESSING: "update",
    DECISION_CONFLICT_NEW_MATERIAL: "create",
}


class DoclingEarningsCallUploader:
    """用量化上传服务实现批量上传的单材料能力。

    Attributes:
        _service: 既有 Docling 上传服务。
        _repository: 源文档仓储，只用于读取既有材料快照。
        _ticker: 本批次所属公司 ticker。
    """

    def __init__(
        self,
        *,
        service: DoclingUploadService,
        repository: SourceDocumentRepositoryProtocol,
        ticker: str,
    ) -> None:
        """初始化适配器。

        Args:
            service: 既有 Docling 上传服务。
            repository: 源文档仓储协议实现。
            ticker: 本批次所属公司 ticker。

        Returns:
            无。

        Raises:
            ValueError: ticker 为空时抛出。
        """

        normalized_ticker = ticker.strip().upper()
        if not normalized_ticker:
            raise ValueError("ticker 不能为空")
        self._service = service
        self._repository = repository
        self._ticker = normalized_ticker

    def read_existing(self, document_ids: Iterable[str]) -> dict[str, ExistingMaterial]:
        """读取候选材料的既有快照。

        Args:
            document_ids: 待查询的 document_id 集合。

        Returns:
            已存在材料快照，按 document_id 索引；不存在的 ID 不出现在结果中。

        Raises:
            无：仓储读取异常按“不存在”处理，由调用方走新建分支。
        """

        existing: dict[str, ExistingMaterial] = {}
        for document_id in document_ids:
            meta = self._read_meta(document_id)
            if meta is None:
                continue
            existing[document_id] = ExistingMaterial(
                document_id=document_id,
                source_fingerprint=str(meta.get("source_fingerprint", "")).strip(),
                ingest_complete=bool(meta.get("ingest_complete", False)),
            )
        return existing

    def upload_one(self, item: BatchPlanItem) -> None:
        """上传并处理一个电话会议纪要材料。

        Args:
            item: 批量决策结果。

        Returns:
            无。

        Raises:
            KeyError: 决策码不在支持范围内时抛出。
            ValueError: 上传参数非法时由既有服务抛出。
            RuntimeError: 转换或落盘失败时由既有服务抛出。
        """

        action = _ACTION_BY_DECISION[item.decision]
        self._service.execute_upload(
            ticker=self._ticker,
            source_kind=SourceKind.MATERIAL,
            action=action,
            document_id=item.document_id,
            internal_document_id=item.document_id,
            form_type=EARNINGS_CALL_FORM_TYPE,
            files=[item.request.path],
            overwrite=False,
            meta={
                "material_name": item.material_name,
                "ingest_method": _INGEST_METHOD_UPLOAD,
                "fiscal_year": item.request.fiscal_year,
                "fiscal_period": item.request.fiscal_period,
            },
        )

    def _read_meta(self, document_id: str) -> Optional[Mapping[str, object]]:
        """读取单个材料元数据。

        Args:
            document_id: 材料 document_id。

        Returns:
            元数据映射；不存在时返回 `None`。

        Raises:
            无：`FileNotFoundError` 视为不存在。
        """

        try:
            return self._repository.get_source_meta(
                ticker=self._ticker,
                document_id=document_id,
                source_kind=SourceKind.MATERIAL,
            )
        except FileNotFoundError:
            return None
