"""内置 Astra 模型选择、真实聊天链路与缺失凭据阻断测试。"""

from pathlib import Path
from queue import Queue
from threading import Event

import pytest
from streamlit.testing.v1 import AppTest

from dayu.services.chat_service import ChatService
from dayu.services.startup_preparation import (
    PreparedHostRuntimeDependencies,
    prepare_host_runtime_dependencies,
)
from dayu.web.streamlit.pages.chat import stream_runtime
from dayu.web.streamlit.pages.chat.stream_runtime import StreamQueueItem, run_stream_worker
from dayu.web.streamlit.pages.chat.utils import build_chat_session_id
from tests.engine.test_async_openai_runner_call_paths import _build_runner_with_steps
from tests.engine.test_responses_protocol import response, step, text_item


_MODEL_NAME = "gpt-6-astra"
_SCENE_NAME = "interactive"
_API_KEY_ENV = "OPENAI_API_KEY"
_MODEL_ENV = "OPENAI_RESPONSES_MODEL"
_API_KEY = "offline-astra-test-key"
_ENDPOINT = "https://api.openai.com/v1/responses"
_TICKER = "AAPL"
_SUCCESS_TEXT = "已使用所选模型完成分析。"
_USER_TEXT = "请总结当前信息。"
_WORKER_TIMEOUT_SECONDS = 10
_MODEL_SELECT_KEY = f"chat_tab_{_TICKER}_model"
_SEND_BUTTON_KEY = f"chat_tab_{_TICKER}_send_button"
_APP = '''
import streamlit as st
from dayu.web.streamlit.components.watchlist import WatchlistItem
from dayu.web.streamlit.pages.chat_tab import render_chat_tab

render_chat_tab(
    selected_stock=WatchlistItem("AAPL", "Apple", "", ""),
    chat_service=st.session_state["test_service"],
    model_names=st.session_state["test_models"],
)
'''


@pytest.fixture
def builtin_dependencies(tmp_path: Path) -> PreparedHostRuntimeDependencies:
    """使用隔离工作区及包内配置装配真实 Host 和场景准备器。

    Args:
        tmp_path: 测试专属工作区，不写入模型或 manifest 覆盖文件。
    Returns:
        真实启动流程生成的运行时依赖。
    Raises:
        OSError: 工作区资源准备失败时抛出。
    """
    return prepare_host_runtime_dependencies(
        workspace_root=tmp_path,
        config_root=tmp_path / "config",
        execution_options=None,
        runtime_label="model-picker-test",
        log_module="TEST",
    )


def _chat_service(dependencies: PreparedHostRuntimeDependencies) -> ChatService:
    """装配使用内置 interactive 场景的真实聊天服务。

    Args:
        dependencies: 隔离工作区内的真实运行时依赖。
    Returns:
        通过真实 Host 执行请求的聊天服务。
    Raises:
        无。
    """
    return ChatService(
        host=dependencies.host,
        scene_execution_acceptance_preparer=dependencies.scene_execution_acceptance_preparer,
        default_scene_name=_SCENE_NAME,
    )


@pytest.mark.parametrize("responses_model", [None, "different-model"], ids=["unset", "ignored"])
def test_builtin_astra_config_is_selectable_and_uses_fixed_model(
    builtin_dependencies: PreparedHostRuntimeDependencies,
    monkeypatch: pytest.MonkeyPatch,
    responses_model: str | None,
) -> None:
    """内置选项可加载，且固定模型不依赖通用 Responses 模型变量。

    Args:
        builtin_dependencies: 使用包内配置的运行时依赖。
        monkeypatch: 隔离环境变量。
        responses_model: 通用条目的模型变量值；空值表示未设置。
    Returns:
        无。
    Raises:
        AssertionError: 可选列表、固定模型或配置字段不符合约定时抛出。
    """
    monkeypatch.setenv(_API_KEY_ENV, _API_KEY)
    monkeypatch.delenv(_MODEL_ENV, raising=False)
    if responses_model is not None:
        monkeypatch.setenv(_MODEL_ENV, responses_model)
    preparer = builtin_dependencies.scene_execution_acceptance_preparer
    scene = preparer.read_scene_definition(_SCENE_NAME)
    assert _MODEL_NAME in scene.model.allowed_names

    loader = builtin_dependencies.workspace.config_loader
    model = loader.load_llm_model(_MODEL_NAME)
    assert model.get("runner_type") == "openai_responses"
    assert model.get("model") == _MODEL_NAME
    assert model.get("endpoint_url") == _ENDPOINT
    headers = model.get("headers")
    assert headers is not None
    assert headers.get("Authorization") == f"Bearer {_API_KEY}"
    assert loader.collect_model_referenced_env_vars((_MODEL_NAME,)) == (_API_KEY_ENV,)
    raw_models = loader.load_llm_models()
    # 除模型 ID 与说明外沿用通用 Responses 条目，防止配置能力发生偏移。
    assert {
        key: value for key, value in raw_models[_MODEL_NAME].items()
        if key not in {"model", "description"}
    } == {
        key: value for key, value in raw_models["openai-responses"].items()
        if key not in {"model", "description"}
    }


@pytest.mark.parametrize("responses_model", [None, "different-model"], ids=["unset", "ignored"])
def test_selected_astra_reaches_real_worker_service_and_responses_http(
    builtin_dependencies: PreparedHostRuntimeDependencies,
    monkeypatch: pytest.MonkeyPatch,
    responses_model: str | None,
) -> None:
    """真实下拉选择驱动 UI worker、Service、Host、Agent 和 Runner 的假 HTTP。

    Args:
        builtin_dependencies: 使用包内配置的运行时依赖。
        monkeypatch: 隔离 HTTP、环境变量及页面运行句柄。
        responses_model: 通用条目的模型变量值；空值表示未设置。
    Returns:
        无。
    Raises:
        AssertionError: 页面选择、请求协议、请求体或完成状态不正确时抛出。
    """
    monkeypatch.setenv(_API_KEY_ENV, _API_KEY)
    monkeypatch.delenv(_MODEL_ENV, raising=False)
    if responses_model is not None:
        monkeypatch.setenv(_MODEL_ENV, responses_model)
    monkeypatch.setattr(stream_runtime, "CHAT_STREAM_RUNTIME_HANDLES", {})
    _, posts = _build_runner_with_steps(
        monkeypatch, [step(response([text_item(_SUCCESS_TEXT)]), True)]
    )
    preparer = builtin_dependencies.scene_execution_acceptance_preparer
    app = AppTest.from_string(_APP, default_timeout=_WORKER_TIMEOUT_SECONDS)
    app.session_state["test_service"] = _chat_service(builtin_dependencies)
    app.session_state["test_models"] = preparer.read_scene_definition(_SCENE_NAME).model.allowed_names
    app.run()
    assert not app.exception
    assert _MODEL_NAME in app.selectbox(key=_MODEL_SELECT_KEY).options
    app.selectbox(key=_MODEL_SELECT_KEY).set_value(_MODEL_NAME).run()
    app.text_area[0].set_value(_USER_TEXT)
    app.button(key=_SEND_BUTTON_KEY).click().run()
    runtime = stream_runtime.CHAT_STREAM_RUNTIME_HANDLES.get(_TICKER)
    if runtime is not None:
        runtime.worker.join(timeout=_WORKER_TIMEOUT_SECONDS)
        assert not runtime.worker.is_alive()
        app.run()

    assert not app.exception and not app.error
    assert len(posts) == 1
    assert posts[0]["endpoint_url"] == _ENDPOINT
    assert posts[0]["headers"]["Authorization"] == f"Bearer {_API_KEY}"
    body = posts[0]["json"]
    assert body["model"] == _MODEL_NAME
    assert body["stream"] is True
    assert "input" in body and "messages" not in body
    assert any(_SUCCESS_TEXT in item.value for item in app.markdown)
    assert _TICKER not in stream_runtime.CHAT_STREAM_RUNTIME_HANDLES


def test_astra_missing_api_key_fails_fast_before_http(
    builtin_dependencies: PreparedHostRuntimeDependencies,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """缺少密钥时加载器报错，真实 worker 投递明确错误且不发出 HTTP。

    Args:
        builtin_dependencies: 使用包内配置的运行时依赖。
        monkeypatch: 删除密钥并隔离 HTTP。
    Returns:
        无。
    Raises:
        AssertionError: 错误缺少条目名或变量名，或错误请求被发送时抛出。
    """
    monkeypatch.delenv(_API_KEY_ENV, raising=False)
    monkeypatch.delenv(_MODEL_ENV, raising=False)
    _, posts = _build_runner_with_steps(monkeypatch, [])
    with pytest.raises(ValueError, match=f"{_MODEL_NAME}.*{_API_KEY_ENV}"):
        builtin_dependencies.workspace.config_loader.load_llm_model(_MODEL_NAME)

    event_queue: Queue[StreamQueueItem] = Queue()
    run_stream_worker(
        chat_service=_chat_service(builtin_dependencies),
        user_text=_USER_TEXT,
        session_id=build_chat_session_id(_TICKER),
        ticker=_TICKER,
        event_queue=event_queue,
        cancel_event=Event(),
        trace_id="model-picker-test",
        model_name=_MODEL_NAME,
    )
    items = [event_queue.get_nowait() for _ in range(event_queue.qsize())]
    assert posts == []
    assert [item.event_type for item in items] == ["error", "done"]
    assert _MODEL_NAME in items[0].chunk
    assert _API_KEY_ENV in items[0].chunk
    assert items[-1].done
