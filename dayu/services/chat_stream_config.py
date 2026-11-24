"""聊天首输出等待策略；由 Service 解析后提供本次提交的生效阈值。"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field

from dayu.contracts.infrastructure import StructuredConfigValue

_CONFIG_SECTION = "chat_stream_config"
_TIMEOUT_FIELD = "first_chunk_timeout_seconds"
_RUNNER_TIMEOUTS_FIELD = "first_chunk_timeout_seconds_by_runner"
_DEFAULT_FIRST_CHUNK_TIMEOUT_SECONDS = 90.0


@dataclass(frozen=True)
class ChatStreamConfig:
    """聊天首输出等待配置，runner 未命中覆盖时使用通用阈值。"""

    first_chunk_timeout_seconds: float = _DEFAULT_FIRST_CHUNK_TIMEOUT_SECONDS
    first_chunk_timeout_seconds_by_runner: Mapping[str, float] = field(default_factory=dict)

    def resolve_first_chunk_timeout_seconds(self, runner: str) -> float:
        """解析指定 runner 的首输出等待秒数。

        Args:
            runner: 实际接受模型的 runner 类型。
        Returns:
            runner 覆盖值或通用默认值。
        Raises:
            无。
        """
        return self.first_chunk_timeout_seconds_by_runner.get(runner, self.first_chunk_timeout_seconds)


def _parse_timeout(value: StructuredConfigValue, *, field_path: str) -> float:
    """校验秒数并规范化成有限正浮点数。

    Args:
        value: 原始 JSON 配置值。
        field_path: 用于诊断的完整字段路径。
    Returns:
        有限且大于零的秒数。
    Raises:
        TypeError: 配置值不是数值或是布尔值。
        ValueError: 配置值非有限、溢出或非正。
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{field_path} 必须为有限正数（秒）")
    try:
        seconds = float(value)
    except OverflowError as exc:
        raise ValueError(f"{field_path} 必须为有限正数（秒）") from exc
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError(f"{field_path} 必须为有限正数（秒）")
    return seconds


def parse_chat_stream_config(run_config: Mapping[str, StructuredConfigValue]) -> ChatStreamConfig:
    """从运行配置解析聊天首输出等待策略。

    Args:
        run_config: 已加载的 run.json 对象。
    Returns:
        校验后的聊天配置；缺失字段使用默认值。
    Raises:
        TypeError: 配置对象、runner 映射或秒数类型非法。
        ValueError: 秒数非有限、非正或 runner 名称为空。
    """
    section = run_config.get(_CONFIG_SECTION, {})
    if not isinstance(section, dict):
        raise TypeError(f"{_CONFIG_SECTION} 必须为对象")
    default_timeout = _parse_timeout(
        section.get(_TIMEOUT_FIELD, _DEFAULT_FIRST_CHUNK_TIMEOUT_SECONDS),
        field_path=f"{_CONFIG_SECTION}.{_TIMEOUT_FIELD}",
    )
    overrides = section.get(_RUNNER_TIMEOUTS_FIELD, {})
    overrides_path = f"{_CONFIG_SECTION}.{_RUNNER_TIMEOUTS_FIELD}"
    if not isinstance(overrides, dict):
        raise TypeError(f"{overrides_path} 必须为 runner 到秒数的对象映射")
    parsed_overrides: dict[str, float] = {}
    for runner, value in overrides.items():
        if not runner.strip() or runner != runner.strip():
            raise ValueError(f"{overrides_path} 的 runner 名称不能为空或包含首尾空白")
        parsed_overrides[runner] = _parse_timeout(value, field_path=f"{overrides_path}.{runner}")
    return ChatStreamConfig(
        first_chunk_timeout_seconds=default_timeout,
        first_chunk_timeout_seconds_by_runner=parsed_overrides,
    )
