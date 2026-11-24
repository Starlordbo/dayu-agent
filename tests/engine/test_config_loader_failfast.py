"""模型配置环境变量在单模型加载边界的阻断测试。"""

import json
from pathlib import Path

import pytest

from dayu.contracts.model_config import ModelConfig
from dayu.startup.config_file_resolver import ConfigFileResolver
from dayu.startup.config_loader import ConfigLoader


_MODEL_NAME = "selected_model"
_ENV_NAME = "DAYU_TEST_MODEL_VALUE"
_PLACEHOLDER = "{{DAYU_TEST_MODEL_VALUE}}"
_ENDPOINT = "https://provider.invalid/v1/chat/completions"


def _write_model_loader(tmp_path: Path, model_config: ModelConfig) -> ConfigLoader:
    """创建包含已选模型和未配置供应商的真实配置加载器。

    Args:
        tmp_path: 测试隔离目录。
        model_config: 本次选择的模型模板。

    Returns:
        从临时配置文件读取数据的加载器。

    Raises:
        OSError: 配置文件写入失败时抛出。
    """

    (tmp_path / "llm_models.json").write_text(
        json.dumps(
            {
                _MODEL_NAME: model_config,
                "unused_provider": {"model": "{{DAYU_TEST_UNUSED_PROVIDER}}"},
            }
        ),
        encoding="utf-8",
    )
    return ConfigLoader(ConfigFileResolver(tmp_path))


@pytest.mark.unit
@pytest.mark.parametrize("env_value", [None, "", " \t\n"], ids=["missing", "empty", "whitespace"])
@pytest.mark.parametrize(
    "model_config",
    [
        {"model": _PLACEHOLDER},
        {"headers": {"Authorization": f"Bearer {_PLACEHOLDER}"}},
        {"endpoint_url": f"{_ENDPOINT}/{_PLACEHOLDER}"},
        {"extra_payloads": {"metadata": [{"tag": _PLACEHOLDER}]}},
    ],
    ids=["model", "authorization", "endpoint", "nested-payload"],
)
def test_invalid_model_env_vars_fail_at_selected_model_load(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    env_value: str | None,
    model_config: ModelConfig,
) -> None:
    """缺失、空串和空白变量均应在单模型加载时给出可操作错误。

    Args:
        tmp_path: 测试隔离目录。
        monkeypatch: 环境变量隔离工具。
        env_value: 待验证的无效环境变量值。
        model_config: 包含变量引用的模型模板。

    Returns:
        无。

    Raises:
        AssertionError: 未拒绝无效变量或错误信息不符合预期时抛出。
    """

    if env_value is None:
        monkeypatch.delenv(_ENV_NAME, raising=False)
    else:
        monkeypatch.setenv(_ENV_NAME, env_value)
    loader = _write_model_loader(tmp_path, model_config)
    with pytest.raises(ValueError) as caught:
        loader.load_llm_model(_MODEL_NAME)

    error = str(caught.value)
    assert "llm_models.json" in error
    assert _MODEL_NAME in error
    assert _ENV_NAME in error
    assert "设置" in error
    assert "重试" in error


@pytest.mark.unit
@pytest.mark.parametrize("residual", ["{{DAYU_TEST_SECOND_VALUE}}", "{{unrecognized-name}}"])
def test_substituted_placeholders_are_rejected_without_exposing_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    residual: str,
) -> None:
    """环境变量值再次引入模板时应拒绝发送，且错误不泄漏凭据值。

    Args:
        tmp_path: 测试隔离目录。
        monkeypatch: 环境变量隔离工具。
        residual: 一次替换后残留的占位符。

    Returns:
        无。

    Raises:
        AssertionError: 未拒绝残留模板或泄漏凭据时抛出。
    """

    secret_value = f"private-token-{residual}"
    monkeypatch.setenv(_ENV_NAME, secret_value)
    monkeypatch.setenv("DAYU_TEST_SECOND_VALUE", "valid-nested-value")
    loader = _write_model_loader(tmp_path, {"headers": {"Authorization": _PLACEHOLDER}})
    with pytest.raises(ValueError) as caught:
        loader.load_llm_model(_MODEL_NAME)

    error = str(caught.value)
    assert _MODEL_NAME in error
    assert _ENV_NAME in error
    assert residual[2:-2] in error
    assert "重试" in error
    assert secret_value not in error


@pytest.mark.unit
def test_catalog_preserves_templates_and_selected_model_reloads_current_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """枚举不要求配齐变量，加载所选模型才校验且不缓存变量解析结果。

    Args:
        tmp_path: 测试隔离目录。
        monkeypatch: 环境变量隔离工具。

    Returns:
        无。

    Raises:
        AssertionError: 模板、选择范围或重新加载语义不符合预期时抛出。
    """

    monkeypatch.delenv(_ENV_NAME, raising=False)
    monkeypatch.delenv("DAYU_TEST_UNUSED_PROVIDER", raising=False)
    loader = _write_model_loader(tmp_path, {"model": _PLACEHOLDER})
    assert loader.load_llm_models()[_MODEL_NAME].get("model") == _PLACEHOLDER
    with pytest.raises(ValueError, match=_ENV_NAME):
        loader.load_llm_model(_MODEL_NAME)

    monkeypatch.setenv(_ENV_NAME, "valid-model")
    assert loader.load_llm_model(_MODEL_NAME).get("model") == "valid-model"
    assert loader.load_llm_models()[_MODEL_NAME].get("model") == _PLACEHOLDER

    monkeypatch.setenv(_ENV_NAME, "updated-model")
    assert loader.load_llm_model(_MODEL_NAME).get("model") == "updated-model"
    monkeypatch.setenv(_ENV_NAME, " ")
    with pytest.raises(ValueError, match=_ENV_NAME):
        loader.load_llm_model(_MODEL_NAME)
