"""电话会议纪要批量上传的调度与冲突决策。

职责边界：
- 本模块只做“一个文件 = 一个材料”的批量调度决策，不解析文档、不访问仓储；
- 真正的落盘与处理复用既有上传命令（由调用方注入 `BatchUploaderProtocol` 实现）；
- 破坏性覆盖不在首版范围内：`overwrite=True` 直接拒绝，冲突一律保留旧件并另存独立材料。

对应验收项见 `docs/p2-verification.md` 的 S1–S4、S8。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional, Protocol

from dayu.fins.pipelines.docling_upload_service import build_material_ids

# 材料 form_type：电话会议纪要固定登记为 EARNINGS_CALL。
EARNINGS_CALL_FORM_TYPE: str = "EARNINGS_CALL"

# 批量决策：新建、复用、恢复处理、冲突另存、拒绝覆盖。
DECISION_CREATE: str = "create"
DECISION_REUSE: str = "reuse"
DECISION_RESUME_PROCESSING: str = "resume_processing"
DECISION_CONFLICT_NEW_MATERIAL: str = "conflict_new_material"

# 单项执行状态：已上传、已复用、失败。
ITEM_STATUS_UPLOADED: str = "uploaded"
ITEM_STATUS_REUSED: str = "reused"
ITEM_STATUS_FAILED: str = "failed"

# 冲突另存时的材料名后缀模板；后缀序号从 2 开始，避免与首份材料同名。
_CONFLICT_NAME_SUFFIX_TEMPLATE: str = "{name} ({index})"
_CONFLICT_SUFFIX_START_INDEX: int = 2

# 冲突另存允许的最大尝试次数，避免无限探测。
_CONFLICT_MAX_ATTEMPTS: int = 50


@dataclass(frozen=True)
class BatchFileRequest:
    """一个待上传的电话会议纪要文件。

    Attributes:
        path: 本地文件路径。
        material_name: 材料名（通常取文档标题），参与稳定 ID 生成。
        fiscal_year: 已确认的财年。
        fiscal_period: 已确认的财季（`Q1`–`Q4`/`FY`）。
        content_fingerprint: 源指纹，必须由 `build_upload_source_fingerprint` 产出，
            与上传服务写入的 `source_fingerprint` 同源。
    """

    path: Path
    material_name: str
    fiscal_year: int
    fiscal_period: str
    content_fingerprint: str


@dataclass(frozen=True)
class ExistingMaterial:
    """仓储中已存在的同名材料快照。

    Attributes:
        document_id: 已存在的 document_id。
        source_fingerprint: 已存在材料的源指纹。
        ingest_complete: 是否已完成入库处理。
    """

    document_id: str
    source_fingerprint: str
    ingest_complete: bool


@dataclass(frozen=True)
class BatchPlanItem:
    """单个文件的批量决策结果。

    Attributes:
        request: 原始请求。
        decision: 决策码，取值见本模块 `DECISION_*` 常量。
        document_id: 本次应写入的 document_id（冲突另存时为新的 ID）。
        material_name: 本次应写入的材料名（冲突另存时带序号后缀）。
    """

    request: BatchFileRequest
    decision: str
    document_id: str
    material_name: str


@dataclass(frozen=True)
class BatchItemResult:
    """单个文件的执行结果。

    Attributes:
        plan: 对应决策。
        status: 执行状态，取值见本模块 `ITEM_STATUS_*` 常量。
        error: 失败时的错误摘要；成功时为 `None`。
    """

    plan: BatchPlanItem
    status: str
    error: Optional[str]


@dataclass(frozen=True)
class BatchResult:
    """整批执行结果。

    Attributes:
        items: 逐项结果，顺序与输入一致。
    """

    items: tuple[BatchItemResult, ...]


class BatchUploaderProtocol(Protocol):
    """批量上传所需的最小上传能力。

    由调用方用既有上传服务适配实现，批量层不直接依赖仓储或转换细节。
    """

    def upload_one(self, item: BatchPlanItem) -> None:
        """上传并处理一个材料。

        Args:
            item: 本次决策结果。

        Returns:
            无。

        Raises:
            Exception: 上传或处理失败时由实现抛出，批量层按项隔离。
        """


def _stable_material_id(*, material_name: str, fiscal_year: int, fiscal_period: str) -> str:
    """按既有稳定规则生成材料 document_id。

    Args:
        material_name: 材料名。
        fiscal_year: 财年。
        fiscal_period: 财季。

    Returns:
        稳定 document_id。

    Raises:
        ValueError: 材料名为空时抛出。
    """

    document_id, _ = build_material_ids(
        form_type=EARNINGS_CALL_FORM_TYPE,
        material_name=material_name,
        fiscal_year=fiscal_year,
        fiscal_period=fiscal_period,
    )
    return document_id


def _allocate_conflict_identity(
    *,
    request: BatchFileRequest,
    existing_by_id: Mapping[str, ExistingMaterial],
) -> tuple[str, str]:
    """为冲突文件分配独立材料名与 ID，保留旧件。

    Args:
        request: 冲突文件请求。
        existing_by_id: 已存在材料快照。

    Returns:
        `(document_id, material_name)`；正常应为未被占用的新身份。

    Raises:
        RuntimeError: 连续尝试仍无法分配未占用身份时抛出。
    """

    for index in range(_CONFLICT_SUFFIX_START_INDEX, _CONFLICT_SUFFIX_START_INDEX + _CONFLICT_MAX_ATTEMPTS):
        candidate_name = _CONFLICT_NAME_SUFFIX_TEMPLATE.format(name=request.material_name, index=index)
        candidate_id = _stable_material_id(
            material_name=candidate_name,
            fiscal_year=request.fiscal_year,
            fiscal_period=request.fiscal_period,
        )
        if candidate_id not in existing_by_id:
            return candidate_id, candidate_name
    raise RuntimeError(f"冲突材料名分配失败: {request.material_name}")


def build_batch_plan(
    *,
    requests: list[BatchFileRequest],
    existing_by_id: Mapping[str, ExistingMaterial],
    overwrite: bool = False,
) -> list[BatchPlanItem]:
    """为整批文件逐项生成上传决策。

    决策规则：不存在则新建；指纹一致且已入库则复用；指纹一致但未入库则恢复处理；
    指纹不同则冲突另存（旧件保留，新件使用带序号后缀的独立材料名）。

    Args:
        requests: 整批文件请求。
        existing_by_id: 已存在材料快照，按 document_id 索引。
        overwrite: 是否请求破坏性覆盖；首版仅支持 `False`。

    Returns:
        与输入顺序一致的决策列表。

    Raises:
        ValueError: 请求中包含空白材料名或 `overwrite=True` 时抛出。
    """

    if overwrite:
        raise ValueError("首版不支持破坏性覆盖：冲突请另存独立材料")
    plans: list[BatchPlanItem] = []
    reserved_ids: dict[str, ExistingMaterial] = dict(existing_by_id)
    for request in requests:
        if not request.material_name.strip():
            raise ValueError("材料名不能为空")
        document_id = _stable_material_id(
            material_name=request.material_name,
            fiscal_year=request.fiscal_year,
            fiscal_period=request.fiscal_period,
        )
        existing = reserved_ids.get(document_id)
        if existing is None:
            plans.append(
                BatchPlanItem(
                    request=request,
                    decision=DECISION_CREATE,
                    document_id=document_id,
                    material_name=request.material_name,
                )
            )
            reserved_ids[document_id] = ExistingMaterial(
                document_id=document_id,
                source_fingerprint=request.content_fingerprint,
                ingest_complete=True,
            )
            continue
        if existing.source_fingerprint == request.content_fingerprint:
            decision = DECISION_REUSE if existing.ingest_complete else DECISION_RESUME_PROCESSING
            plans.append(
                BatchPlanItem(
                    request=request,
                    decision=decision,
                    document_id=document_id,
                    material_name=request.material_name,
                )
            )
            continue
        conflict_id, conflict_name = _allocate_conflict_identity(
            request=request,
            existing_by_id=reserved_ids,
        )
        plans.append(
            BatchPlanItem(
                request=request,
                decision=DECISION_CONFLICT_NEW_MATERIAL,
                document_id=conflict_id,
                material_name=conflict_name,
            )
        )
        reserved_ids[conflict_id] = ExistingMaterial(
            document_id=conflict_id,
            source_fingerprint=request.content_fingerprint,
            ingest_complete=True,
        )
    return plans


def execute_batch(
    *,
    plans: list[BatchPlanItem],
    uploader: BatchUploaderProtocol,
) -> BatchResult:
    """按决策逐项执行上传，失败项隔离，不中断整批。

    Args:
        plans: `build_batch_plan` 生成的决策列表。
        uploader: 既有上传能力的适配实现。

    Returns:
        逐项执行结果。

    Raises:
        无：单项失败被记录为 `ITEM_STATUS_FAILED`，不向上抛出。
    """

    results: list[BatchItemResult] = []
    for plan in plans:
        if plan.decision == DECISION_REUSE:
            results.append(BatchItemResult(plan=plan, status=ITEM_STATUS_REUSED, error=None))
            continue
        try:
            uploader.upload_one(plan)
        except Exception as error:  # noqa: BLE001 - 单项失败必须隔离并记录
            results.append(
                BatchItemResult(plan=plan, status=ITEM_STATUS_FAILED, error=f"{type(error).__name__}: {error}")
            )
            continue
        results.append(BatchItemResult(plan=plan, status=ITEM_STATUS_UPLOADED, error=None))
    return BatchResult(items=tuple(results))
