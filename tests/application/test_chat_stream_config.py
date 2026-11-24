"""聊天首输出等待配置的默认值、覆盖和错误边界测试。"""

from pathlib import Path

import pytest

from dayu.contracts.infrastructure import StructuredConfigObject, StructuredConfigValue
from dayu.services.chat_stream_config import parse_chat_stream_config
from dayu.startup.config_file_resolver import ConfigFileResolver
from dayu.startup.config_loader import ConfigLoader


@pytest.mark.parametrize("run_config", [{}, {"chat_stream_config": {}}])
def test_missing_config_keeps_ninety_seconds(run_config: StructuredConfigObject) -> None:
    """缺失配置保留默认值。

    Args:
        run_config: 缺少全部或部分聊天配置的运行配置。
    Returns:
        无。
    Raises:
        AssertionError: 默认值或覆盖映射不符。
    """
    config = parse_chat_stream_config(run_config)
    assert config.first_chunk_timeout_seconds == 90.0
    # Responses 通道在真实使用中首输出可达 90 秒以上，包内为其提供更长的等待窗口。
    assert config.first_chunk_timeout_seconds_by_runner == {"openai_responses": 300.0}
    assert config.resolve_first_chunk_timeout_seconds("openai_responses") == 90.0


def test_runner_override_and_fallback() -> None:
    """runner 覆盖仅影响匹配类型，整数秒也可用。

    Args:
        无。
    Returns:
        无。
    Raises:
        AssertionError: 覆盖或回退错误。
    """
    config = parse_chat_stream_config({"chat_stream_config": {
        "first_chunk_timeout_seconds": 120,
        "first_chunk_timeout_seconds_by_runner": {"openai_responses": 300},
    }})
    assert config.resolve_first_chunk_timeout_seconds("openai_responses") == 300.0
    assert config.resolve_first_chunk_timeout_seconds("openai_compatible") == 120.0
    assert config.resolve_first_chunk_timeout_seconds("future_runner") == 120.0


@pytest.mark.parametrize("value", [None, True, "90", [], {}, 0, -1, float("nan"), float("inf"), -float("inf"), 10**400])
@pytest.mark.parametrize("override", [False, True])
def test_invalid_seconds_report_field(value: StructuredConfigValue, override: bool) -> None:
    """非法秒数必须明确指出字段。

    Args:
        value: 非法配置值。
        override: 是否测试 runner 覆盖项。
    Returns:
        无。
    Raises:
        AssertionError: 非法值未报错或诊断缺少字段路径。
    """
    field = "first_chunk_timeout_seconds"
    section: StructuredConfigObject = {field: value}
    if override:
        field = "first_chunk_timeout_seconds_by_runner.openai_responses"
        section = {"first_chunk_timeout_seconds_by_runner": {"openai_responses": value}}
    with pytest.raises((TypeError, ValueError), match=f"chat_stream_config.{field}"):
        parse_chat_stream_config({"chat_stream_config": section})


@pytest.mark.parametrize("value", [None, True, 90, "90", []])
@pytest.mark.parametrize("override", [False, True])
def test_invalid_container_reports_field(value: StructuredConfigValue, override: bool) -> None:
    """显式非法容器不能退化成默认配置。

    Args:
        value: 非对象容器。
        override: 是否测试映射容器。
    Returns:
        无。
    Raises:
        AssertionError: 容器错误未被拒绝。
    """
    section = {"first_chunk_timeout_seconds_by_runner": value} if override else value
    with pytest.raises(TypeError, match="chat_stream_config"):
        parse_chat_stream_config({"chat_stream_config": section})


def test_packaged_default_does_not_enable_responses_override(tmp_path: Path) -> None:
    """包内默认配置不能替用户启用 300 秒覆盖。

    Args:
        tmp_path: 无用户配置的隔离工作区。
    Returns:
        无。
    Raises:
        AssertionError: 包内配置改变默认行为。
    """
    config = parse_chat_stream_config(ConfigLoader(ConfigFileResolver(tmp_path)).load_run_config())
    assert config.first_chunk_timeout_seconds == 90.0
    assert config.first_chunk_timeout_seconds_by_runner == {}


@pytest.mark.parametrize("runner", ["", " ", " openai_responses", "openai_responses "])
def test_runner_names_reject_empty_or_surrounding_whitespace(runner: str) -> None:
    """拒绝无法与模型 runner 精确匹配的空白名称。

    Args:
        runner: 非法 runner 映射键。
    Returns:
        无。
    Raises:
        AssertionError: 名称错误未被拒绝。
    """
    with pytest.raises(ValueError, match="chat_stream_config.first_chunk_timeout_seconds_by_runner"):
        parse_chat_stream_config({"chat_stream_config": {
            "first_chunk_timeout_seconds_by_runner": {runner: 300.0},
        }})
