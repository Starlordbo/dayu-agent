"""真实 Service→Host→Agent→Runner 链路的离线 Responses 验证。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dayu.contracts.agent_types import JsonValue
from dayu.contracts.events import AppEventType
from dayu.execution.options import ExecutionOptions
from dayu.services.chat_service import ChatService
from dayu.services.contracts import ChatTurnRequest, ChatResumeRequest
from dayu.services.write_service import _resolve_write_scene_models
from dayu.services.internal.write_pipeline.execution_options import build_execution_options_with_model_override
from dayu.services.startup_preparation import prepare_host_runtime_dependencies
from tests.engine.test_async_openai_runner_call_paths import _build_runner_with_steps, _PostStep, _FakeResponse
from tests.engine.test_responses_protocol import response, step, text_item


def list_call(call_id: str, ticker: str) -> dict[str, JsonValue]:
    """构造原生财报列表工具调用。

    Args:
        call_id: 调用 ID。
        ticker: 公司。
    Returns:
        原生 function call。
    Raises:
        无。
    """
    return {
        "type": "function_call",
        "id": "item_" + call_id,
        "call_id": call_id,
        "name": "list_documents",
        "arguments": json.dumps({"ticker": ticker}),
        "status": "completed",
    }


@pytest.mark.asyncio
async def test_real_service_roundtrip_and_restart(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """实际服务完成多工具、多轮；重建 Host 后续聊保持模型并拒绝切换。

    Args:
        tmp_path: 隔离工作区。
        monkeypatch: 仅替换模型 HTTP。
    Returns:
        无。
    Raises:
        AssertionError: 任一链路或持久化验证失败。
    """
    config = tmp_path / "config"
    config.mkdir()
    models: dict[str, dict[str, JsonValue]] = {
        "openai-responses": {
            "runner_type": "openai_responses",
            "model": "offline-test",
            "endpoint_url": "https://example.invalid/responses",
            "headers": {},
            "max_context_tokens": 128000,
        }
    }
    models["openai-responses"]["runtime_hints"] = {"temperature_profiles": {"interactive": {"temperature": 1.0}}}
    (config / "llm_models.json").write_text(json.dumps(models))
    reasoning: dict[str, JsonValue] = {"type": "reasoning", "id": "rs", "encrypted_content": "opaque", "summary": []}
    _, posts = _build_runner_with_steps(
        monkeypatch,
        [
            step(response([text_item("先查资料"), reasoning, list_call("a", "AAA"), list_call("b", "BBB")]), True),
            step(response([list_call("c", "CCC")]), True),
            step(response([text_item("本地没有这些公司的材料。")]), True),
            step(response([text_item("沿用上一轮资料缺口。")]), True),
        ],
    )
    deps = prepare_host_runtime_dependencies(
        workspace_root=tmp_path,
        config_root=config,
        execution_options=None,
        runtime_label="offline-test",
        log_module="TEST",
    )
    service = ChatService(
        host=deps.host,
        scene_execution_acceptance_preparer=deps.scene_execution_acceptance_preparer,
        default_scene_name="interactive",
    )
    first = await service.submit_turn(
        ChatTurnRequest(user_text="比较三家公司", execution_options=ExecutionOptions(model_name="openai-responses"))
    )
    events = [event async for event in first.event_stream]
    assert not [event for event in events if event.type == AppEventType.ERROR]
    assert len(posts) == 3
    assert any(event.type == AppEventType.FINAL_ANSWER for event in events)
    assert any(item.get("type") == "function_call_output" for item in posts[1]["json"]["input"])
    assert reasoning in posts[1]["json"]["input"]
    restored = prepare_host_runtime_dependencies(
        workspace_root=tmp_path,
        config_root=config,
        execution_options=None,
        runtime_label="offline-restart",
        log_module="TEST",
    )
    continued = ChatService(
        host=restored.host,
        scene_execution_acceptance_preparer=restored.scene_execution_acceptance_preparer,
        default_scene_name="interactive",
    )
    second = await continued.submit_turn(ChatTurnRequest(user_text="继续说明缺口", session_id=first.session_id))
    followup = [event async for event in second.event_stream]
    assert not [event for event in followup if event.type == AppEventType.ERROR]
    assert len(posts) == 4
    assert posts[-1]["json"]["model"] == "offline-test"
    assert "本地没有这些公司的材料" in json.dumps(posts[-1]["json"], ensure_ascii=False)
    with pytest.raises(ValueError, match="新建会话"):
        await continued.submit_turn(
            ChatTurnRequest(
                user_text="换模型", session_id=first.session_id, execution_options=ExecutionOptions(model_name="other")
            )
        )
    models["openai-responses"]["model"] = "different-provider-model"
    (config / "llm_models.json").write_text(json.dumps(models))
    changed = prepare_host_runtime_dependencies(
        workspace_root=tmp_path,
        config_root=config,
        execution_options=None,
        runtime_label="offline-changed",
        log_module="TEST",
    )
    changed_service = ChatService(
        host=changed.host,
        scene_execution_acceptance_preparer=changed.scene_execution_acceptance_preparer,
        default_scene_name="interactive",
    )
    with pytest.raises(ValueError, match="新建会话"):
        await changed_service.submit_turn(ChatTurnRequest(user_text="继续", session_id=first.session_id))
    assert len(posts) == 4


def test_write_scene_default_and_both_overrides(tmp_path: Path) -> None:
    """验证报告默认值与主写作、核查两组显式覆盖，全部使用真实场景配置。

    Args:
        tmp_path: 隔离工作区。
    Returns:
        无。
    Raises:
        AssertionError: 场景模型路由不一致。
    """
    config = tmp_path / "config"
    config.mkdir()
    deps = prepare_host_runtime_dependencies(
        workspace_root=tmp_path,
        config_root=config,
        execution_options=None,
        runtime_label="report-offline",
        log_module="TEST",
    )
    preparer = deps.scene_execution_acceptance_preparer
    default = _resolve_write_scene_models(
        scene_execution_acceptance_preparer=preparer, main_execution_options=None, audit_execution_options=None
    )
    for scene, model in default.items():
        assert model.name == preparer.read_scene_definition(scene).model.default_name
    main = build_execution_options_with_model_override(execution_options=None, model_name="openai-responses")
    audit = build_execution_options_with_model_override(execution_options=None, model_name="openai-responses")
    explicit = _resolve_write_scene_models(
        scene_execution_acceptance_preparer=preparer, main_execution_options=main, audit_execution_options=audit
    )
    assert explicit
    assert all(model.name == "openai-responses" for model in explicit.values())


@pytest.mark.asyncio
async def test_pending_turn_resume_preserves_protocol(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """模型失败后重建 Host，从已有 pending 快照恢复，保持协议。

    Args:
        tmp_path: 临时工作区。
        monkeypatch: HTTP 桩。
    Returns:
        无。
    Raises:
        AssertionError: 恢复状态或路由错误。
    """
    config = tmp_path / "config"
    config.mkdir()
    model = {
        "runner_type": "openai_responses",
        "model": "offline-test",
        "endpoint_url": "https://example.invalid/responses",
        "headers": {},
        "max_retries": 0,
        "runtime_hints": {"temperature_profiles": {"interactive": {"temperature": 1.0}}},
    }
    (config / "llm_models.json").write_text(json.dumps({"openai-responses": model}))
    _, posts = _build_runner_with_steps(
        monkeypatch,
        [
            _PostStep(response=_FakeResponse(status=500, text_data="temporary")),
            step(response([text_item("恢复成功")]), True),
        ],
    )
    deps = prepare_host_runtime_dependencies(
        workspace_root=tmp_path,
        config_root=config,
        execution_options=None,
        runtime_label="resume-test",
        log_module="TEST",
    )
    service = ChatService(
        host=deps.host,
        scene_execution_acceptance_preparer=deps.scene_execution_acceptance_preparer,
        default_scene_name="interactive",
    )
    submission = await service.submit_turn(
        ChatTurnRequest(user_text="第一次请求", execution_options=ExecutionOptions(model_name="openai-responses"))
    )
    events = [event async for event in submission.event_stream]
    assert any(event.type == AppEventType.ERROR for event in events)
    pending = service.list_resumable_pending_turns(session_id=submission.session_id)
    assert len(pending) == 1
    restored = prepare_host_runtime_dependencies(
        workspace_root=tmp_path,
        config_root=config,
        execution_options=None,
        runtime_label="resume-restart",
        log_module="TEST",
    )
    restored_service = ChatService(
        host=restored.host,
        scene_execution_acceptance_preparer=restored.scene_execution_acceptance_preparer,
        default_scene_name="interactive",
    )
    resumed = await restored_service.resume_pending_turn(
        ChatResumeRequest(session_id=submission.session_id, pending_turn_id=pending[0].pending_turn_id)
    )
    result = [event async for event in resumed.event_stream]
    assert not [event for event in result if event.type == AppEventType.ERROR]
    assert len(posts) == 2
    assert posts[1]["json"]["model"] == "offline-test"
    assert "input" in posts[1]["json"] and "messages" not in posts[1]["json"]
