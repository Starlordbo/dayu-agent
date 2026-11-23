"""电话会议纪要逐文件上传、处理与恢复页面。

页面只提交 Service 命令，不直接访问资料仓储；上传成功不是可问答，
只有 process_material 成功或确认同版本快照可复用才展示可问答。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, cast

import streamlit as st

from dayu.contracts.fins import (
    FinsCommand,
    FinsCommandName,
    FinsResult,
    ProcessMaterialCommandPayload,
    ProcessSingleResultData,
    UploadMaterialCommandPayload,
    UploadMaterialResultData,
)
from dayu.fins.domain.tool_models import Citation
from dayu.fins.pipelines.earnings_call_batch import EARNINGS_CALL_FORM_TYPE
from dayu.web.streamlit.pages.chat.citations import render_citation_links
from dayu.fins.upload_recognition import infer_earnings_call_fiscal
from dayu.services.contracts import FinsSubmitRequest
from dayu.services.protocols import FinsServiceProtocol
from dayu.web.streamlit.components.watchlist import WatchlistItem

STATUS_UPLOADING = "uploading"
STATUS_PROCESSING = "processing"
STATUS_READY = "ready"
STATUS_FAILED = "failed"
STATUS_PENDING_CONFIRMATION = "pending_confirmation"
_STATUS_LABELS = {
    STATUS_UPLOADING: "上传中",
    STATUS_PROCESSING: "处理中",
    STATUS_READY: "可问答",
    STATUS_FAILED: "失败",
    STATUS_PENDING_CONFIRMATION: "待确认",
}
_UPLOAD_STAGING_PARTS = (".dayu", "streamlit", "material-uploads")
_ROWS_STATE_KEY = "material_rows"
_PERIODS = ("", "Q1", "Q2", "Q3", "Q4", "FY", "H1")
_UPLOAD_OK = "ok"
_SKIPPED = "skipped"
_ALREADY_UPLOADED = "already_uploaded"
_PROCESSED = "processed"
_VERSION_MATCHED = "version_matched"


@dataclass(frozen=True)
class MaterialUploadEntry:
    """单文件的标题、财季及确认信息。"""

    file_name: str
    material_name: str
    fiscal_year: int | None
    fiscal_period: str | None
    needs_confirmation: bool
    reason: str


@dataclass(frozen=True)
class MaterialUploadRow:
    """逐文件状态及恢复检查点；document_id 仅在上传完成后写入。"""

    file_name: str
    material_name: str
    status: str
    message: str
    document_id: str | None = None


@dataclass(frozen=True)
class MaterialUploadInput:
    """浏览器文件内容和用户已核对的材料身份。"""

    entry: MaterialUploadEntry
    data: bytes


def build_material_entries(file_names: list[str]) -> list[MaterialUploadEntry]:
    """从文件名建立初始条目，未确认信息由页面允许补全。

    Args:
        file_names: 原始文件名。
    Returns:
        与输入一一对应的初始条目。
    Raises:
        无。
    """
    entries: list[MaterialUploadEntry] = []
    for file_name in file_names:
        guess = infer_earnings_call_fiscal(filename=file_name)
        entries.append(
            MaterialUploadEntry(
                file_name=file_name,
                material_name=Path(file_name).stem.strip() or file_name,
                fiscal_year=guess.fiscal_year,
                fiscal_period=guess.fiscal_period,
                needs_confirmation=guess.needs_confirmation,
                reason=guess.reason,
            )
        )
    return entries


def material_input_key(*, ticker: str, item: MaterialUploadInput) -> str:
    """按公司、材料身份与真实内容生成页面检查点键。

    Args:
        ticker: 公司代码。
        item: 已确认的条目及文件内容。
    Returns:
        SHA-256 键；同名等长不同内容不会误复用，跨公司也不会串用。
    Raises:
        无。
    """
    entry = item.entry
    identity = json.dumps(
        [
            ticker.strip().upper(),
            entry.file_name,
            entry.material_name,
            entry.fiscal_year,
            entry.fiscal_period,
            hashlib.sha256(item.data).hexdigest(),
        ],
        ensure_ascii=False,
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def build_upload_request(
    *,
    ticker: str,
    company_name: str,
    entry: MaterialUploadEntry,
    file_path: Path,
    fiscal_year: int,
    fiscal_period: str,
) -> FinsSubmitRequest:
    """构造非覆盖的同步单材料上传命令。

    Args:
        ticker: 公司代码。
        company_name: 自选股中的公司名称，用于建立公司元数据。
        entry: 用户核对的材料条目。
        file_path: 暂存路径。
        fiscal_year: 已确认财年。
        fiscal_period: 已确认财季。
    Returns:
        交给 FinsService 的上传请求。
    Raises:
        ValueError: 公司、标题或财季非法。
    """
    normalized_ticker = ticker.strip().upper()
    normalized_period = fiscal_period.strip().upper()
    if not normalized_ticker:
        raise ValueError("ticker 不能为空")
    if normalized_period not in _PERIODS[1:]:
        raise ValueError("fiscal_period 非法")
    if not entry.material_name.strip():
        raise ValueError("材料标题不能为空")
    return FinsSubmitRequest(
        command=FinsCommand(
            name=FinsCommandName.UPLOAD_MATERIAL,
            payload=UploadMaterialCommandPayload(
                ticker=normalized_ticker,
                company_name=company_name,
                files=(file_path,),
                form_type=EARNINGS_CALL_FORM_TYPE,
                material_name=entry.material_name.strip(),
                fiscal_year=fiscal_year,
                fiscal_period=normalized_period,
                overwrite=False,
            ),
            stream=False,
        )
    )


def apply_result(*, entry: MaterialUploadEntry, data: UploadMaterialResultData) -> MaterialUploadRow:
    """将真实上传契约映射到待处理检查点，不提前标记可问答。

    Args:
        entry: 原条目。
        data: 上传命令结果。
    Returns:
        上传失败或待处理行；仅同指纹已上传允许 skipped。
    Raises:
        无。
    """
    accepted = data.status == _UPLOAD_OK or (data.status == _SKIPPED and data.skip_reason == _ALREADY_UPLOADED)
    if not accepted or not data.document_id:
        return MaterialUploadRow(
            entry.file_name, entry.material_name, STATUS_FAILED, data.message or f"上传未完成: {data.status}"
        )
    return MaterialUploadRow(
        entry.file_name, entry.material_name, STATUS_PROCESSING, "原件与转换结果已入库，等待处理", data.document_id
    )


def _stage_file(*, workspace_root: Path, file_name: str, data: bytes) -> Path:
    """将文件安全暂存到内容隔离目录，保留原始 basename 供仓储计算指纹。

    Args:
        workspace_root: 工作区根目录。
        file_name: 原始文件名，不允许路径分隔符。
        data: 文件字节。
    Returns:
        暂存路径。
    Raises:
        ValueError: 文件名包含目录穿越或绝对路径。
        OSError: 文件写入失败。
    """
    if not file_name or file_name in {".", ".."} or "/" in file_name or "\\" in file_name:
        raise ValueError("上传文件名不得包含路径")
    directory = workspace_root.joinpath(*_UPLOAD_STAGING_PARTS, hashlib.sha256(data).hexdigest())
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / file_name
    target.write_bytes(data)
    return target


def execute_material_input(
    *,
    ticker: str,
    company_name: str,
    item: MaterialUploadInput,
    workspace_root: Path,
    fins_service: FinsServiceProtocol,
    previous: MaterialUploadRow | None = None,
    on_progress: Callable[[MaterialUploadRow], None] | None = None,
) -> MaterialUploadRow:
    """执行单文件上传及处理，已上传失败项只恢复处理。

    Args:
        ticker: 所属公司代码。
        company_name: 公司名称，传给上传命令初始化公司元数据。
        item: 已确认条目和内容。
        workspace_root: 暂存工作区。
        fins_service: 宿主托管的财报服务。
        previous: 同一 material_input_key 对应的先前检查点。
        on_progress: 可选页面状态持久化及渲染回调。
    Returns:
        独立终态；失败保留 document_id 以便仅恢复处理。
    Raises:
        无：文件级异常转为失败行，不阻断其余文件。
    """
    entry = item.entry
    if previous is not None and previous.status == STATUS_READY:
        return previous
    if entry.needs_confirmation or entry.fiscal_year is None or entry.fiscal_period is None:
        return MaterialUploadRow(
            entry.file_name, entry.material_name, STATUS_PENDING_CONFIRMATION, "请逐文件补全并确认财年、财季与标题"
        )
    row = previous or MaterialUploadRow(entry.file_name, entry.material_name, STATUS_UPLOADING, "正在上传及转换")
    try:
        if row.document_id is None:
            row = replace(row, status=STATUS_UPLOADING, message="正在上传及转换")
            if on_progress is not None:
                on_progress(row)
            staged = _stage_file(workspace_root=workspace_root, file_name=entry.file_name, data=item.data)
            submission = fins_service.submit(
                build_upload_request(
                    ticker=ticker,
                    company_name=company_name,
                    entry=entry,
                    file_path=staged,
                    fiscal_year=entry.fiscal_year,
                    fiscal_period=entry.fiscal_period,
                )
            )
            execution = submission.execution
            if not isinstance(execution, FinsResult) or not isinstance(execution.data, UploadMaterialResultData):
                raise TypeError("上传命令必须返回同步 UploadMaterialResultData")
            row = apply_result(entry=entry, data=execution.data)
            if row.status == STATUS_FAILED:
                return row
        document_id = row.document_id
        if document_id is None:
            raise RuntimeError("上传结果缺少 document_id")
        row = replace(row, status=STATUS_PROCESSING, message="正在生成可查询快照")
        # 在处理前持久化上传检查点，失败后重试无需再次提交上传命令。
        if on_progress is not None:
            on_progress(row)
        submission = fins_service.submit(
            FinsSubmitRequest(
                command=FinsCommand(
                    name=FinsCommandName.PROCESS_MATERIAL,
                    payload=ProcessMaterialCommandPayload(ticker=ticker, document_id=document_id),
                    stream=False,
                )
            )
        )
        execution = submission.execution
        if not isinstance(execution, FinsResult) or not isinstance(execution.data, ProcessSingleResultData):
            raise TypeError("处理命令必须返回同步 ProcessSingleResultData")
        result = execution.data
        if result.document_id != document_id:
            raise RuntimeError("处理结果的 document_id 与上传检查点不一致")
        if result.status != _PROCESSED and not (result.status == _SKIPPED and result.reason == _VERSION_MATCHED):
            raise RuntimeError(result.message or result.reason or f"处理未完成: {result.status}")
        return replace(row, status=STATUS_READY, message="处理完成，可通过原生工具搜索与读取")
    except Exception as error:  # 单文件隔离；检查点保留上传成功事实。
        return replace(row, status=STATUS_FAILED, message=f"{type(error).__name__}: {error}")


@dataclass
class _RowProgress:
    """把处理前检查点写回页面会话并即时展示。"""

    state_key: str

    def __call__(self, row: MaterialUploadRow) -> None:
        """保存并展示中间状态。

        Args:
            row: 当前文件状态。
        Returns:
            无。
        Raises:
            无。
        """
        st.session_state[self.state_key] = row
        st.caption(f"{row.file_name}：{_STATUS_LABELS[row.status]}")


def _edit_entry(entry: MaterialUploadEntry, widget_key: str) -> MaterialUploadEntry:
    """为单文件提供标题、财季及确认控件。

    Args:
        entry: 文件名推断的条目。
        widget_key: 公司及内容隔离的控件键。
    Returns:
        用户编辑后的条目。
    Raises:
        无。
    """
    with st.expander(entry.file_name):
        title = st.text_input(
            "材料标题（冲突时修改后另存，原件不会覆盖）", value=entry.material_name, key=f"title_{widget_key}"
        )
        year_text = st.text_input("财年", value=str(entry.fiscal_year or ""), key=f"year_{widget_key}")
        period = st.selectbox(
            "财季", options=_PERIODS, index=_PERIODS.index(entry.fiscal_period or ""), key=f"period_{widget_key}"
        )
        confirmed = not entry.needs_confirmation
        if entry.needs_confirmation:
            confirmed = st.checkbox("已按原文核对标题与财季", key=f"confirm_{widget_key}")
        year = int(year_text) if year_text.isdigit() and len(year_text) == 4 else None
    return replace(
        entry,
        material_name=title.strip(),
        fiscal_year=year,
        fiscal_period=period or None,
        needs_confirmation=not confirmed or year is None or not period or not title.strip(),
    )


def _render_material_catalog(ticker: str, service: FinsServiceProtocol) -> None:
    """显示仓储中的持久材料目录及原文入口，不把入库目录标为可问答。

    Args:
        ticker: 当前公司代码。
        service: 财报查询入口。
    Returns:
        无。
    Raises:
        无；读取异常展示提示，不阻止上传。
    """
    st.markdown("#### 已入库电话会议材料")
    try:
        materials = service.list_materials(ticker)
    except Exception as exc:
        st.caption(f"材料目录暂不可用：{exc}")
        return
    if not materials:
        st.caption("暂无已入库纪要。")
        return
    st.caption("此处列出已完整入库原文；不代表已生成处理快照。")
    st.dataframe(
        [
            {
                "公司": material.company_name or material.ticker,
                "财年": material.fiscal_year,
                "财季": material.fiscal_period,
                "材料标题": material.material_name or "未披露",
                "文档编号": material.document_id,
            }
            for material in materials
        ],
        hide_index=True,
        width="stretch",
    )
    for material in materials:
        marker = Citation(
            source_type="SUPPLEMENTARY", ticker=material.ticker, document_id=material.document_id
        ).to_dict()["source_marker"]
        st.write(material.material_name or material.document_id)
        st.markdown(render_citation_links(str(marker), st.query_params.to_dict()))


def render_material_tab(
    selected_stock: WatchlistItem,
    workspace_root: Path,
    fins_service: FinsServiceProtocol,
) -> None:
    """渲染逐文件上传、失败重试与可问答状态。

    Args:
        selected_stock: 当前公司。
        workspace_root: 暂存工作区。
        fins_service: 财报服务。
    Returns:
        无。
    Raises:
        无：执行失败由各文件结果独立展示。
    """
    st.markdown("#### 电话会议纪要上传")
    st.caption("支持 Markdown / TXT / DOCX / 文字型 PDF；一个文件一份材料；转换完成后继续预处理。")
    uploaded_files = st.file_uploader(
        "选择纪要文件（可多选）",
        accept_multiple_files=True,
        type=["md", "txt", "docx", "pdf"],
        key=f"material_uploader_{selected_stock.ticker}",
    )
    if not uploaded_files:
        _render_material_catalog(selected_stock.ticker, fins_service)
        return
    inputs: list[MaterialUploadInput] = []
    seen_keys: set[str] = set()
    for uploaded, entry in zip(uploaded_files, build_material_entries([item.name for item in uploaded_files])):
        item = MaterialUploadInput(entry, uploaded.getvalue())
        widget_key = material_input_key(ticker=selected_stock.ticker, item=item)
        if widget_key in seen_keys:
            st.caption(f"已忽略重复选择：{entry.file_name}")
            continue
        seen_keys.add(widget_key)
        inputs.append(replace(item, entry=_edit_entry(entry, widget_key)))
    start = st.button("开始上传", key=f"material_start_{selected_stock.ticker}")
    rows: list[MaterialUploadRow] = []
    for item in inputs:
        key = material_input_key(ticker=selected_stock.ticker, item=item)
        state_key = f"{_ROWS_STATE_KEY}_{key}"
        previous = cast(MaterialUploadRow | None, st.session_state.get(state_key))
        retry = False
        if previous is not None and previous.status == STATUS_FAILED:
            retry = st.button(f"重试 {item.entry.file_name}", key=f"retry_{key}")
        if start or retry:
            row = execute_material_input(
                ticker=selected_stock.ticker,
                company_name=selected_stock.company_name,
                item=item,
                workspace_root=workspace_root,
                fins_service=fins_service,
                previous=previous,
                on_progress=_RowProgress(state_key),
            )
            st.session_state[state_key] = row
            rows.append(row)
        elif previous is not None:
            rows.append(previous)
    if rows:
        st.dataframe(
            [
                {
                    "文件": row.file_name,
                    "材料": row.material_name,
                    "状态": _STATUS_LABELS[row.status],
                    "说明": row.message,
                    "文档编号": row.document_id or "",
                }
                for row in rows
            ],
            width="stretch",
            hide_index=True,
        )
    _render_material_catalog(selected_stock.ticker, fins_service)
