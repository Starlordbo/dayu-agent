"""真实聊天入口的模型配置阻断、错误投递及修复重试验证。"""

import json
from pathlib import Path
from queue import Queue
from typing import Literal

from dayu.contracts.cancellation import CancellationToken

import pytest

from dayu.contracts.model_config import OpenAICompatibleModelConfig
from dayu.services.chat_service import ChatService
from dayu.services.startup_preparation import prepare_host_runtime_dependencies
from dayu.web.streamlit.pages.chat.stream_runtime import ChatStreamEvent, StreamQueueItem, run_stream_worker
from dayu.web.streamlit.pages.chat.utils import build_chat_session_id
from tests.engine.test_async_openai_runner_call_paths import _build_runner_with_steps
from tests.engine.test_responses_protocol import response, step, text_item


_MODEL_NAME = "openai-responses"
_ENV_NAME = "DAYU_TEST_SELECTED_MODEL_VALUE"
_PLACEHOLDER = "{{DAYU_TEST_SELECTED_MODEL_VALUE}}"
_RESIDUAL_PLACEHOLDER = "{{DAYU_TEST_NESTED_MODEL_VALUE}}"
_SUCCESS_TEXT = "配置修复后已完成请求。"


def _prepare_chat_service(tmp_path: Path, field: Literal["model", "authorization"]) -> ChatService:
    """通过真实启动装配创建使用环境变量模型的聊天服务。

    Args:
        tmp_path: 隔离工作区。
        field: 接受环境变量占位符的模型字段。

    Returns:
        已接入真实配置加载器、Host 与场景准备器的聊天服务。

    Raises:
        OSError: 临时配置写入或启动资源准备失败时抛出。
    """

    config_root = tmp_path / "config"
    config_root.mkdir()
    model: OpenAICompatibleModelConfig = {
        "runner_type": "openai_responses",
        "model": "offline-model",
        "endpoint_url": "https://provider.invalid/responses",
        "headers": {},
        "max_retries": 0,
        "max_context_tokens": 128000,
        "runtime_hints": {"temperature_profiles": {"interactive": {"temperature": 1.0}}},
    }
    if field == "model":
        model["model"] = _PLACEHOLDER
    else:
        model["headers"] = {"Authorization": f"Bearer {_PLACEHOLDER}"}
    (config_root / "llm_models.json").write_text(
        json.dumps({_MODEL_NAME: model}), encoding="utf-8"
    )
    deps = prepare_host_runtime_dependencies(
        workspace_root=tmp_path,
        config_root=config_root,
        execution_options=None,
        runtime_label="config-failfast-test",
        log_module="TEST",
    )
    return ChatService(
        host=deps.host,
        scene_execution_acceptance_preparer=deps.scene_execution_acceptance_preparer,
        default_scene_name="interactive",
    )


def _run_chat_worker(service: ChatService) -> list[StreamQueueItem]:
    """运行真实 UI worker，并读取交付给渲染端的全部队列事件。

    Args:
        service: 真实聊天服务。

    Returns:
        worker 结束后队列中的事件，保持投递顺序。

    Raises:
        无。
    """

    event_queue: Queue[ChatStreamEvent] = Queue()
    run_stream_worker(
        chat_service=service,
        user_text="请总结当前信息。",
        session_id=build_chat_session_id("AAPL"),
        ticker="AAPL",
        event_queue=event_queue,
        cancellation_token=CancellationToken(),
        trace_id="config-failfast-test",
        model_name=_MODEL_NAME,
    )
    items = [event_queue.get_nowait() for _ in range(event_queue.qsize())]
    return [item for item in items if isinstance(item, StreamQueueItem)]


@pytest.mark.parametrize("field", ["model", "authorization"])
@pytest.mark.parametrize(
    "env_value",
    [None, "", " \t\n", _RESIDUAL_PLACEHOLDER],
    ids=["missing", "empty", "whitespace", "residual"],
)
def test_real_chat_config_failure_reaches_worker_without_http_and_allows_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: Literal["model", "authorization"],
    env_value: str | None,
) -> None:
    """真实配置失败须零 HTTP 且向 UI 投递终态，修复后可完成同入口重试。

    Args:
        tmp_path: 隔离工作区。
        monkeypatch: 环境变量和模型 HTTP 隔离工具。
        field: 引用变量的模型字段。
        env_value: 缺失、空串、空白或含残留模板的变量值。

    Returns:
        无。

    Raises:
        AssertionError: 请求阻断、错误投递或修复重试结果不符合预期时抛出。
    """

    if env_value is None:
        monkeypatch.delenv(_ENV_NAME, raising=False)
    else:
        monkeypatch.setenv(_ENV_NAME, env_value)
    monkeypatch.setenv("DAYU_TEST_NESTED_MODEL_VALUE", "nested-value")
    _, posts = _build_runner_with_steps(
        monkeypatch, [step(response([text_item(_SUCCESS_TEXT)]), True)]
    )
    service = _prepare_chat_service(tmp_path, field)

    failed_items = _run_chat_worker(service)

    assert posts == []
    assert [item.event_type for item in failed_items] == ["error", "done"]
    error_message = failed_items[0].chunk
    assert "llm_models.json" in error_message
    assert _MODEL_NAME in error_message
    assert _ENV_NAME in error_message
    assert "设置" in error_message
    assert "重试" in error_message
    assert failed_items[-1].done

    # 同一服务重试走完整 Host/Agent/Runner，证明 HTTP 桩确实接入了生产调用。
    monkeypatch.setenv(_ENV_NAME, "fixed-value")
    retried_items = _run_chat_worker(service)

    assert len(posts) == 1
    assert not [item for item in retried_items if item.event_type == "error"]
    assert any(item.event_type == "chunk" and _SUCCESS_TEXT in item.chunk for item in retried_items)
    assert retried_items[-1].done
