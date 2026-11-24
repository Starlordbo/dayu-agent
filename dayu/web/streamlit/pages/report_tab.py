"""分析报告表单与磁盘状态轮询，不在 Streamlit 线程执行写作服务。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import streamlit as st

from dayu.services.contracts import WriteRequest, WriteRunConfig
from dayu.services.protocols import WriteServiceProtocol
from dayu.startup.config_file_resolver import resolve_package_assets_path
from dayu.web.streamlit.components.watchlist import WatchlistItem
from dayu.web.streamlit.report_runs import ReportRun, ReportRunState
from dayu.web.streamlit.report_runtime import load_report_run, start_report_run

_POLL_SECONDS = 2.0
_DEFAULT_TEMPLATE = "定性分析模板.md"
_WRITE_MAX_RETRIES = 2
_REPORT_SUFFIX = "_qual_report.md"


def _template_choices(workspace_root: Path) -> dict[str, Path]:
    """列出工作区 Markdown 模板及始终可选的内置默认模板。

    参数:
        workspace_root: 工作区根目录。
    返回值:
        按工作区默认模板优先排序的标签与绝对路径。
    异常:
        OSError: 模板目录无法读取。
    """
    assets = workspace_root / "assets"
    paths = sorted(assets.glob("*.md"), key=lambda path: (path.name != _DEFAULT_TEMPLATE, path.name))
    choices = {f"工作区 · {path.name}": path.resolve() for path in paths if path.is_file()}
    choices[f"内置 · {_DEFAULT_TEMPLATE}"] = resolve_package_assets_path() / _DEFAULT_TEMPLATE
    return choices


def _format_model(name: str) -> str:
    """把空模型覆盖显示为使用场景配置。

    参数:
        name: 可选模型名。
    返回值:
        控件中的可读标签。
    异常:
        无。
    """
    return name or "使用场景默认模型"


def _render_result(run: ReportRun, ticker: str, key_prefix: str) -> None:
    """从成功运行的输出目录读取最终报告，显示正文与 Markdown 下载。

    参数:
        run: 磁盘持久化终态。
        ticker: 当前公司股票代码。
        key_prefix: 公司隔离的 UI 键前缀。
    返回值:
        无。
    异常:
        无；读取错误转成页面提示。
    """
    report_path = Path(run.output_dir) / f"{ticker}{_REPORT_SUFFIX}"
    try:
        content = report_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        st.warning(f"任务已结束，但未找到报告文件：{report_path}")
        return
    except (OSError, UnicodeError) as exc:
        st.error(f"报告读取失败：{exc}")
        return
    st.markdown(content)
    st.download_button("下载 Markdown", data=content, file_name=report_path.name,
                       mime="text/markdown", key=f"{key_prefix}_download", on_click="ignore")


def _render_run_status(run: ReportRun | None, ticker: str, key_prefix: str) -> None:
    """渲染开始时间、时长及终态，阶段不可获得时明确显示运行中。

    参数:
        run: 当前磁盘记录；尚未生成时为空。
        ticker: 当前公司股票代码。
        key_prefix: 公司隔离的 UI 键前缀。
    返回值:
        无。
    异常:
        ValueError: 已校验记录中的时间格式失效。
    """
    if run is None:
        return
    started = datetime.fromisoformat(run.started_at)
    end = datetime.now(timezone.utc) if run.state == ReportRunState.RUNNING else datetime.fromisoformat(run.updated_at)
    elapsed = timedelta(seconds=max(0, int((end - started).total_seconds())))
    st.caption(f"开始时间：{started.astimezone().isoformat(timespec='seconds')} · 已运行：{elapsed}")
    if run.state == ReportRunState.RUNNING:
        st.info("运行中：已有报告任务正在后台生成，请等待完成。")
    elif run.state == ReportRunState.STALE:
        st.warning("上次运行已中断，可重试。")
    elif run.state == ReportRunState.FAILED:
        st.error(f"报告生成失败：{run.error}")
    else:
        st.success("报告生成完成。")
        _render_result(run, ticker, key_prefix)
    if run.log_path:
        st.caption(f"任务日志：{run.log_path}")


@st.fragment(run_every=_POLL_SECONDS)
def _render_report_panel(
    selected_stock: WatchlistItem,
    workspace_root: Path,
    write_service: WriteServiceProtocol | None,
    model_names: tuple[str, ...],
) -> None:
    """独立轮询报告状态与表单，使自动片段刷新同步更新提交按钮。

    参数:
        selected_stock: 当前自选股。
        workspace_root: 工作区根目录。
        write_service: 写作服务；为空时仅展示既有结果。
        model_names: 入口传入的可选模型名。
    返回值:
        无。
    异常:
        无；持久化或启动错误转成页面提示。
    """
    ticker = selected_stock.ticker.strip().upper()
    key_prefix = f"report_tab_{ticker}"
    output_dir = (workspace_root / "draft" / ticker).resolve()
    try:
        run = load_report_run(workspace_root, ticker)
        templates = _template_choices(workspace_root)
    except (OSError, ValueError) as exc:
        st.error(f"报告任务状态或模板读取失败：{exc}")
        return
    running = run is not None and run.state == ReportRunState.RUNNING
    if write_service is None:
        st.warning("分析报告功能不可用（写作服务未初始化）。")
    st.caption(f"目标输出目录：{output_dir}")
    with st.form(f"{key_prefix}_form"):
        template_label = st.selectbox("报告模板", tuple(templates), key=f"{key_prefix}_template", disabled=running)
        model = st.selectbox("报告模型覆盖", ("", *model_names), format_func=_format_model,
                             key=f"{key_prefix}_model", disabled=running)
        resume = st.checkbox("从已有进度继续（resume）", value=True, key=f"{key_prefix}_resume", disabled=running)
        submitted = st.form_submit_button("生成报告", disabled=running or write_service is None)
    if submitted and write_service is not None:
        template_path = templates[template_label]
        if not template_path.is_file():
            st.error(f"模板文件不存在：{template_path}")
        else:
            request = WriteRequest(write_config=WriteRunConfig(
                ticker=ticker, company=selected_stock.company_name, template_path=str(template_path),
                output_dir=str(output_dir), write_max_retries=_WRITE_MAX_RETRIES,
                # 空覆盖沿用 Service 解析的工作区 provider，不在 UI 重复读取配置。
                web_provider="", resume=resume, write_model_override_name=model,
            ))
            try:
                if start_report_run(workspace_root=workspace_root, write_service=write_service, request=request):
                    st.rerun(scope="app")
                else:
                    st.info("该公司已有报告任务正在启动或运行，请勿重复提交。")
            except (OSError, ValueError, RuntimeError) as exc:
                st.error(f"报告任务启动失败：{exc}")
    _render_run_status(run, ticker, key_prefix)


def render_report_tab(
    selected_stock: WatchlistItem,
    workspace_root: Path,
    write_service: WriteServiceProtocol | None,
    model_names: tuple[str, ...] = (),
) -> None:
    """渲染分析报告 Tab。

    参数:
        selected_stock: 当前选中的自选股。
        workspace_root: 工作区根目录。
        write_service: 写作服务协议实例；为 None 时分析报告功能不可用。
        model_names: 沿用入口已配置的可选模型清单；空选择保留场景默认值。

    返回值:
        无。

    异常:
        无。
    """

    _render_report_panel(selected_stock, workspace_root, write_service, model_names)
