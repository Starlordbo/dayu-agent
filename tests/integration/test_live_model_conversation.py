"""显式启用的真实模型验证；通过 ChatService 调用隔离空仓储，不访问研究材料。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import time
from typing import cast

import pytest

from dayu.contracts.agent_types import JsonValue
from dayu.contracts.events import AppEventType
from dayu.execution.options import ExecutionOptions
from dayu.fins.domain.document_models import CompanyMeta
from dayu.fins.storage.fs_company_meta_repository import FsCompanyMetaRepository
from dayu.services.chat_service import ChatService
from dayu.services.contracts import ChatTurnRequest
from dayu.services.startup_preparation import prepare_host_runtime_dependencies

_ENABLE_VARIABLE = "DAYU_RUN_LIVE_API"
_SCENE = "interactive"
_MAX_ITERATIONS = 4
_MAX_OUTPUT_TOKENS = 1024
_TURN_TIMEOUT_SECONDS = 180
_HTTP_TIMEOUT_SECONDS = 60
_MEMORY_MARKER = "DAYU-验证-4827"
_CONFIG_NAMES = ("openai-responses", "deepseek-v4-flash-thinking")
_FIRST_PROMPT = (
    "这是隔离空库的工具协议验证，不是投资研究。请记住本会话校验标记：" + _MEMORY_MARKER + "。"
    "必须实际调用 list_documents，分别查询 AMZN 和 MSFT 的本地文档。"
    "只查询一次每家公司，不下载、不联网、不推断业绩。"
    "收到两个工具结果后，用一句话说明本地是否有材料，并原样复述校验标记。"
)
_FOLLOWUP_PROMPT = (
    "继续上轮空库验证：实际调用 list_documents 查询 GOOGL 的本地文档，不下载、不联网。"
    "收到工具结果后，用一句话报告结果，并原样复述我在上一轮给你的校验标记。"
)
pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(os.environ.get(_ENABLE_VARIABLE) != "1", reason="真实 API 验证默认关闭"),
]


def _read_mapping(path: Path) -> dict[str, JsonValue]:
    """读取测试配置对象。

    Args:
        path: JSON 文件路径。
    Returns:
        JSON 对象。
    Raises:
        ValueError: 文件不是 JSON 对象。
        OSError: 读取失败。
    """
    value = cast(JsonValue, json.loads(path.read_text(encoding="utf-8")))
    if not isinstance(value, dict):
        raise ValueError(f"配置不是对象：{path.name}")
    return value


def _configure_workspace(workspace: Path, config_name: str) -> None:
    """复制现有配置到隔离工作区并限定预算和只读财报工具。

    Args:
        workspace: pytest 创建的隔离目录。
        config_name: 本次测试模型的现有配置名称。
    Returns:
        无。
    Raises:
        OSError: 文件读写失败。
        ValueError: 配置形状错误。
    """
    project = Path(__file__).resolve().parents[2]
    package_config = project / "dayu" / "config"
    local_models = project / "workspace" / "config" / "llm_models.json"
    catalog = _read_mapping(local_models if local_models.exists() else package_config / "llm_models.json")
    model = catalog[config_name]
    if not isinstance(model, dict):
        raise ValueError("模型配置不是对象")
    model["max_retries"] = 0
    model["timeout"] = _HTTP_TIMEOUT_SECONDS
    model["supports_stream"] = True
    if config_name == "openai-responses":
        model["model"] = "gpt-6-astra"
        model["extra_payloads"] = {"reasoning": {"effort": "low"}, "max_output_tokens": _MAX_OUTPUT_TOKENS}
    else:
        model["model"] = "deepseek-flash"
        model["extra_payloads"] = {
            "thinking": {"type": "enabled"}, "reasoning_effort": "high", "max_tokens": _MAX_OUTPUT_TOKENS,
        }
    config = workspace / "config"
    config.mkdir()
    (config / "llm_models.json").write_text(json.dumps(catalog, ensure_ascii=False), encoding="utf-8")
    manifest = _read_mapping(package_config / "prompts" / "manifests" / "interactive.json")
    manifest["tool_selection"] = {"mode": "select", "tool_tags_any": ["fins"]}
    manifest_dir = config / "prompts" / "manifests"
    manifest_dir.mkdir(parents=True)
    (manifest_dir / "interactive.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    run = _read_mapping(package_config / "run.json")
    agent = run["agent_running_config"]
    if not isinstance(agent, dict):
        raise ValueError("Agent 配置不是对象")
    agent.update({"max_continuations": 0, "max_compactions": 0, "fallback_mode": "raise_error"})
    (config / "run.json").write_text(json.dumps(run, ensure_ascii=False), encoding="utf-8")
    # 只登记测试公司身份，不创建文档；通过正式仓储获得成功的空文档列表。
    repository = FsCompanyMetaRepository(workspace)
    for ticker in ("AMZN", "MSFT", "GOOGL"):
        repository.upsert_company_meta(CompanyMeta(
            company_id=ticker, company_name=f"Protocol fixture {ticker}", ticker=ticker,
            market="US", resolver_version="live_protocol_fixture",
            updated_at=datetime.now(timezone.utc).isoformat(),
        ))


def _build_service(workspace: Path) -> ChatService:
    """按生产启动入口创建新的 Host 和聊天服务。

    Args:
        workspace: 当前测试独立工作区。
    Returns:
        使用真实 Runner 和原生工具的服务。
    Raises:
        ValueError: 配置无效。
        OSError: 工作区无法打开。
    """
    deps = prepare_host_runtime_dependencies(
        workspace_root=workspace, config_root=workspace / "config", execution_options=None,
        runtime_label="live-protocol-validation", log_module="TEST.LIVE",
    )
    return ChatService(host=deps.host, scene_execution_acceptance_preparer=deps.scene_execution_acceptance_preparer,
                       default_scene_name=_SCENE)


async def _consume_turn(
    service: ChatService, request: ChatTurnRequest, destination: Path
) -> tuple[str, list[dict[str, JsonValue]]]:
    """消费真实流式事件并即时落盘，排除推理文本和原生加密状态。

    Args:
        service: 实际聊天服务。
        request: 当前用户请求。
        destination: 本轮事件日志路径。
    Returns:
        会话编号和筛选后的事件。
    Raises:
        TimeoutError: 超过整轮时限。
        OSError: 写入失败。
        ValueError: 服务拒绝请求。
    """
    rows: list[dict[str, JsonValue]] = []
    started = time.monotonic()
    async with asyncio.timeout(_TURN_TIMEOUT_SECONDS):
        submission = await service.submit_turn(request)
        with destination.open("w", encoding="utf-8") as output:
            async for event in submission.event_stream:
                if event.type == AppEventType.REASONING_DELTA:
                    continue
                row: dict[str, JsonValue] = {
                    "type": event.type.value, "payload": cast(JsonValue, event.payload),
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                    "meta": {key: cast(JsonValue, event.meta[key]) for key in
                             ("run_id", "request_id", "attempt", "model") if key in event.meta},
                }
                rows.append(row)
                output.write(json.dumps(row, ensure_ascii=False) + "\n")
                output.flush()
    return submission.session_id, rows


def _check_turn(rows: list[dict[str, JsonValue]], expected_tickers: set[str], expected_model: str) -> None:
    """核对流式增量、实际工具结果、模型续答、用量和跨轮标记。

    Args:
        rows: 本轮真实事件。
        expected_tickers: 必须经过原生查询的公司代码。
        expected_model: 实际请求及用量事件必须对应的模型 ID。
    Returns:
        无。
    Raises:
        AssertionError: 任一验证未通过。
    """
    assert not [row for row in rows if row["type"] == AppEventType.ERROR.value], "服务返回错误，详见事件日志"
    assert any(row["type"] == AppEventType.CONTENT_DELTA.value for row in rows), "未收到流式正文增量"
    queried: set[str] = set()
    summaries: list[dict[str, JsonValue]] = []
    final: dict[str, JsonValue] | None = None
    for row in rows:
        payload = row["payload"]
        if not isinstance(payload, dict):
            continue
        if row["type"] == AppEventType.TOOL_EVENT.value and payload.get("engine_event_type") == "tool_call_result":
            data = payload.get("data")
            if isinstance(data, dict) and data.get("name") == "list_documents":
                arguments, result = data.get("arguments"), data.get("result")
                assert isinstance(result, dict) and result.get("ok") is True, "原生财报工具调用失败"
                if isinstance(arguments, dict) and isinstance(arguments.get("ticker"), str):
                    queried.add(cast(str, arguments["ticker"]).upper())
        if row["type"] == AppEventType.METADATA.value:
            if "request_attempt" in payload or "token_usage_summary" in payload:
                meta = row["meta"]
                assert isinstance(meta, dict) and meta.get("model") == expected_model, "模型路由发生变化"
            summary = payload.get("run_usage_summary")
            if isinstance(summary, dict):
                summaries.append(summary)
        if row["type"] == AppEventType.FINAL_ANSWER.value:
            final = payload
    assert expected_tickers <= queried, f"工具查询缺失：{expected_tickers - queried}"
    assert final is not None and not final.get("degraded") and not final.get("filtered"), "无正常最终回答"
    content = final.get("content")
    assert isinstance(content, str) and _MEMORY_MARKER in content, "会话校验标记未保留"
    assert summaries, "缺少整轮用量"
    count = summaries[-1].get("request_count")
    assert isinstance(count, int) and 2 <= count <= _MAX_ITERATIONS, "未发生工具结果回传后的模型续答"
    assert summaries[-1].get("requests_with_usage") == count, "请求用量记录不完整"


@pytest.mark.asyncio
@pytest.mark.parametrize("config_name", _CONFIG_NAMES)
async def test_live_service_stream_tools_and_followup(tmp_path: Path, config_name: str) -> None:
    """真实验证两种协议的多工具流式闭环和重建 Host 后的同会话追问。

    Args:
        tmp_path: 独立测试工作区，不含真实研究数据。
        config_name: 明确选择的供应商模型配置。
    Returns:
        无。
    Raises:
        AssertionError: 链路验证失败。
        TimeoutError: 单轮超过时间预算。
        OSError: 工作区操作失败。
    """
    key = "OPENAI_API_KEY" if config_name == "openai-responses" else "DEEPSEEK_API_KEY"
    assert os.environ.get(key), f"缺少 {key}，尚未发起请求"
    _configure_workspace(tmp_path, config_name)
    options = ExecutionOptions(model_name=config_name, max_iterations=_MAX_ITERATIONS,
                               fallback_mode="raise_error", trace_enabled=False)
    service = _build_service(tmp_path)
    session_id, first = await _consume_turn(
        service, ChatTurnRequest(user_text=_FIRST_PROMPT, execution_options=options), tmp_path / "turn-1.jsonl"
    )
    expected_model = "gpt-6-astra" if config_name == "openai-responses" else "deepseek-flash"
    _check_turn(first, {"AMZN", "MSFT"}, expected_model)
    restored = _build_service(tmp_path)
    # 追问不指定模型，由服务从已持久化的会话中恢复选择。
    resumed_id, second = await _consume_turn(
        restored, ChatTurnRequest(user_text=_FOLLOWUP_PROMPT, session_id=session_id,
                                  execution_options=ExecutionOptions(max_iterations=_MAX_ITERATIONS,
                                                                     fallback_mode="raise_error", trace_enabled=False)),
        tmp_path / "turn-2.jsonl",
    )
    assert resumed_id == session_id
    _check_turn(second, {"GOOGL"}, expected_model)
    (tmp_path / "passed.json").write_text(json.dumps({"model_config": config_name, "session_id": session_id,
                                                     "status": "passed"}), encoding="utf-8")
