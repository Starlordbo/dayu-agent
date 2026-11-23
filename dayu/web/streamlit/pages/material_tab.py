"""Streamlit 电话会议纪要上传 Tab。

职责边界：
- 一个文件 = 一个材料：逐文件推断财季、逐文件提交既有 `upload_material` 命令；
- 单文件失败不中断整批，结果逐行展示；
- 页面重绘时按批次指纹去重，不重复提交。

纯函数部分（条目推断、请求构造、批次指纹、结果映射）与 Streamlit 解耦，便于无头测试。
对应验收项见 `docs/p2-verification.md` 的 W1、W2、W3、W4。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import streamlit as st

from dayu.contracts.fins import (
    FinsCommand,
    FinsCommandName,
    FinsResult,
    UploadMaterialCommandPayload,
    UploadMaterialResultData,
)
from dayu.fins.pipelines.earnings_call_batch import EARNINGS_CALL_FORM_TYPE
from dayu.fins.upload_recognition import infer_earnings_call_fiscal
from dayu.services.contracts import FinsSubmitRequest
from dayu.services.protocols import FinsServiceProtocol
from dayu.web.streamlit.components.watchlist import WatchlistItem

# 逐文件展示状态。
STATUS_SUBMITTED: str = "submitted"
STATUS_FAILED: str = "failed"
STATUS_PENDING_CONFIRMATION: str = "pending_confirmation"

# 上传暂存目录（相对工作区根目录）。
_UPLOAD_STAGING_PARTS: tuple[str, ...] = (".dayu", "streamlit", "material-uploads")
# 页面会话状态键前缀。
_BATCH_STATE_KEY: str = "material_batch"
_ROWS_STATE_KEY: str = "material_rows"


@dataclass(frozen=True)
class MaterialUploadEntry:
    """一个待上传文件的推断结果。

    Attributes:
        file_name: 原始文件名。
        material_name: 材料名（默认取文件名主干）。
        fiscal_year: 推断财年；None 表示需用户补全。
        fiscal_period: 推断财季；None 表示需用户补全。
        needs_confirmation: 是否必须由用户确认后才能提交。
        reason: 推断原因码，沿用 `upload_recognition` 的 `REASON_*`。
    """

    file_name: str
    material_name: str
    fiscal_year: Optional[int]
    fiscal_period: Optional[str]
    needs_confirmation: bool
    reason: str


@dataclass(frozen=True)
class MaterialUploadRow:
    """逐文件上传结果行。

    Attributes:
        file_name: 原始文件名。
        material_name: 材料名。
        status: 展示状态，取值见本模块 `STATUS_*`。
        message: 失败或待确认时的说明。
    """

    file_name: str
    material_name: str
    status: str
    message: str


def build_material_entries(file_names: list[str]) -> list[MaterialUploadEntry]:
    """按文件名推断每个文件的财季。

    Args:
        file_names: 上传文件原始文件名列表。

    Returns:
        与输入顺序一致的条目列表；每个文件独立推断，互不影响。

    Raises:
        无。
    """

    entries: list[MaterialUploadEntry] = []
    for file_name in file_names:
        stem = Path(file_name).stem.strip()
        guess = infer_earnings_call_fiscal(filename=file_name)
        entries.append(
            MaterialUploadEntry(
                file_name=file_name,
                material_name=stem or file_name,
                fiscal_year=guess.fiscal_year,
                fiscal_period=guess.fiscal_period,
                needs_confirmation=guess.needs_confirmation,
                reason=guess.reason,
            )
        )
    return entries


def batch_key(*, file_names: list[str], sizes: list[int]) -> str:
    """计算批次指纹，用于页面重绘时判断是否重复提交。

    Args:
        file_names: 文件名列表。
        sizes: 与文件名一一对应的字节数列表。

    Returns:
        十六进制 SHA-256 批次指纹。

    Raises:
        ValueError: 文件名与大小数量不一致时抛出。
    """

    if len(file_names) != len(sizes):
        raise ValueError("文件名与大小数量不一致")
    pairs = sorted(zip(file_names, sizes))
    digest = hashlib.sha256()
    for name, size in pairs:
        digest.update(f"{name}:{size};".encode("utf-8"))
    return digest.hexdigest()


def build_upload_request(
    *,
    ticker: str,
    entry: MaterialUploadEntry,
    file_path: Path,
    fiscal_year: int,
    fiscal_period: str,
) -> FinsSubmitRequest:
    """构造单文件上传请求。

    Args:
        ticker: 公司 ticker。
        entry: 文件条目。
        file_path: 暂存到工作区后的文件路径。
        fiscal_year: 用户确认后的财年。
        fiscal_period: 用户确认后的财季。

    Returns:
        可直接提交给 `FinsServiceProtocol.submit` 的请求。

    Raises:
        ValueError: ticker 或财季为空时抛出。
    """

    normalized_ticker = ticker.strip().upper()
    if not normalized_ticker:
        raise ValueError("ticker 不能为空")
    normalized_period = fiscal_period.strip().upper()
    if not normalized_period:
        raise ValueError("fiscal_period 不能为空")
    return FinsSubmitRequest(
        command=FinsCommand(
            name=FinsCommandName.UPLOAD_MATERIAL,
            payload=UploadMaterialCommandPayload(
                ticker=normalized_ticker,
                files=(file_path,),
                form_type=EARNINGS_CALL_FORM_TYPE,
                material_name=entry.material_name,
                fiscal_year=fiscal_year,
                fiscal_period=normalized_period,
                overwrite=False,
            ),
            stream=False,
        ),
    )


def apply_result(*, entry: MaterialUploadEntry, data: UploadMaterialResultData) -> MaterialUploadRow:
    """把单文件上传结果映射为展示行。

    Args:
        entry: 对应文件条目。
        data: `upload_material` 命令结果数据。

    Returns:
        展示行；非 uploaded 状态标记为失败并带原因。

    Raises:
        无。
    """

    if data.status != "uploaded":
        return MaterialUploadRow(
            file_name=entry.file_name,
            material_name=entry.material_name,
            status=STATUS_FAILED,
            message=f"上传状态: {data.status}",
        )
    return MaterialUploadRow(
        file_name=entry.file_name,
        material_name=entry.material_name,
        status=STATUS_SUBMITTED,
        message="已提交并完成转换",
    )


def _stage_file(*, workspace_root: Path, ticker: str, file_name: str, data: bytes) -> Path:
    """把上传文件写入工作区暂存目录。

    Args:
        workspace_root: 工作区根目录。
        ticker: 公司 ticker。
        file_name: 原始文件名。
        data: 文件字节内容。

    Returns:
        暂存后的文件路径。

    Raises:
        OSError: 写入失败时抛出。
    """

    target_dir = workspace_root.joinpath(*_UPLOAD_STAGING_PARTS, ticker)
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / file_name
    target_path.write_bytes(data)
    return target_path


def render_material_tab(
    selected_stock: WatchlistItem,
    workspace_root: Path,
    fins_service: FinsServiceProtocol,
) -> None:
    """渲染电话会议纪要上传 Tab。

    Args:
        selected_stock: 当前选中的自选股。
        workspace_root: 工作区根目录。
        fins_service: 财报服务协议实例。

    Returns:
        无。

    Raises:
        无：单文件失败被记录为结果行，不向上抛出。
    """

    st.markdown("#### 电话会议纪要上传")
    st.caption("支持 Markdown / TXT / DOCX / 文字型 PDF；一个文件登记为一份材料，逐份显示状态。")

    uploaded_files = st.file_uploader(
        "选择纪要文件（可多选）",
        accept_multiple_files=True,
        type=["md", "txt", "docx", "pdf"],
        key=f"material_uploader_{selected_stock.ticker}",
    )
    if not uploaded_files:
        return

    file_names = [uploaded.name for uploaded in uploaded_files]
    sizes = [len(uploaded.getvalue()) for uploaded in uploaded_files]
    current_key = batch_key(file_names=file_names, sizes=sizes)
    entries = build_material_entries(file_names)

    st.dataframe(
        [
            {
                "文件": entry.file_name,
                "财年": entry.fiscal_year or "",
                "财季": entry.fiscal_period or "",
                "需确认": "是" if entry.needs_confirmation else "",
            }
            for entry in entries
        ],
        width="stretch",
        hide_index=True,
    )

    pending = [entry for entry in entries if entry.needs_confirmation]
    confirmed = True
    if pending:
        st.warning(f"{len(pending)} 个文件无法从文件名确认财季，请补全后再提交。")
        confirmed = st.checkbox("我已核对上述财年 / 财季", key=f"material_confirm_{current_key}")

    if st.session_state.get(_BATCH_STATE_KEY) == current_key:
        rows = st.session_state.get(_ROWS_STATE_KEY, [])
        if rows:
            st.dataframe(
                [
                    {
                        "文件": row.file_name,
                        "材料": row.material_name,
                        "状态": row.status,
                        "说明": row.message,
                    }
                    for row in rows
                ],
                width="stretch",
                hide_index=True,
            )
        return

    if st.button("开始上传", key=f"material_upload_{current_key}", disabled=not confirmed):
        rows: list[MaterialUploadRow] = []
        for uploaded, entry in zip(uploaded_files, entries):
            if entry.fiscal_year is None or entry.fiscal_period is None:
                rows.append(
                    MaterialUploadRow(
                        file_name=entry.file_name,
                        material_name=entry.material_name,
                        status=STATUS_PENDING_CONFIRMATION,
                        message="缺少财年或财季声明",
                    )
                )
                continue
            staged_path = _stage_file(
                workspace_root=workspace_root,
                ticker=selected_stock.ticker,
                file_name=uploaded.name,
                data=uploaded.getvalue(),
            )
            request = build_upload_request(
                ticker=selected_stock.ticker,
                entry=entry,
                file_path=staged_path,
                fiscal_year=entry.fiscal_year,
                fiscal_period=entry.fiscal_period,
            )
            try:
                submission = fins_service.submit(request)
                execution = submission.execution
                if not isinstance(execution, FinsResult):
                    raise RuntimeError("上传命令返回了流式句柄，当前页面只支持同步结果")
                data = execution.data
                if not isinstance(data, UploadMaterialResultData):
                    raise RuntimeError("上传命令返回的结果类型不匹配")
                rows.append(apply_result(entry=entry, data=data))
            except Exception as error:  # noqa: BLE001 - 单文件失败必须隔离并展示
                rows.append(
                    MaterialUploadRow(
                        file_name=entry.file_name,
                        material_name=entry.material_name,
                        status=STATUS_FAILED,
                        message=f"{type(error).__name__}: {error}",
                    )
                )
        st.session_state[_BATCH_STATE_KEY] = current_key
        st.session_state[_ROWS_STATE_KEY] = rows
        st.rerun()
