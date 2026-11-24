"""检索预算配置的默认值、严格校验及覆盖测试。"""

from pathlib import Path
from typing import cast

import pytest

from dayu.contracts.tool_configs import FinsToolLimits, build_fins_tool_limits
from dayu.contracts.toolset_config import ToolsetConfigSnapshot, ToolsetConfigScalar
from dayu.execution.options import (
    ExecutionOptions, _build_fins_tool_limits_from_override, build_base_execution_options,
    deserialize_execution_options_snapshot, merge_execution_options, serialize_execution_options_snapshot,
)
from dayu.contracts.tool_configs import resolve_fins_tool_limits_from_toolset_configs


@pytest.mark.parametrize("value", [0, -1, True, 1.5, "16", None])
def test_budget_rejects_invalid_values(value: ToolsetConfigScalar) -> None:
    """各配置入口均拒绝非正整数。

    参数:
        value: 非法 JSON 配置值。
    返回值:
        无。
    异常:
        AssertionError: 非法值被容忍。
    """
    with pytest.raises(ValueError, match="retrieval_budget_per_ticker"):
        FinsToolLimits(retrieval_budget_per_ticker=cast(int, value))
    with pytest.raises(ValueError, match="retrieval_budget_per_ticker"):
        build_fins_tool_limits(ToolsetConfigSnapshot("fins", payload={"retrieval_budget_per_ticker": value}))
    with pytest.raises(ValueError, match="retrieval_budget_per_ticker"):
        _build_fins_tool_limits_from_override({"retrieval_budget_per_ticker": value})


def test_budget_defaults_and_sparse_override(tmp_path: Path) -> None:
    """默认16且 run.json、快照及稀疏覆盖语义一致。

    参数:
        tmp_path: 工作区目录。
    返回值:
        无。
    异常:
        AssertionError: 配置链路丢失预算。
    """
    assert FinsToolLimits().retrieval_budget_per_ticker == 16
    assert build_fins_tool_limits(None).retrieval_budget_per_ticker == 16
    options = build_base_execution_options(run_config={"fins_tool_limits": {"retrieval_budget_per_ticker": 12}}, workspace_dir=tmp_path)
    configured = resolve_fins_tool_limits_from_toolset_configs(options.toolset_configs)
    assert configured is not None and configured.retrieval_budget_per_ticker == 12
    overridden = _build_fins_tool_limits_from_override({"search_document_max_items": 5}, base=configured)
    assert overridden is not None and overridden.retrieval_budget_per_ticker == 12
    changed = _build_fins_tool_limits_from_override({"retrieval_budget_per_ticker": 14}, base=configured)
    assert changed is not None and changed.retrieval_budget_per_ticker == 14


@pytest.mark.parametrize("use_sparse_override", [False, True])
def test_budget_survives_host_request_snapshot(tmp_path: Path, use_sparse_override: bool) -> None:
    """Host 请求快照恢复后仍保留预算配置与稀疏覆盖。

    参数:
        tmp_path: 隔离工作区。
        use_sparse_override: 是否使用请求级稀疏覆盖。
    返回值:
        无。
    异常:
        AssertionError: 恢复或合并过程丢失预算。
    """
    base = build_base_execution_options(workspace_dir=tmp_path, run_config={"fins_tool_limits": {
        "retrieval_budget_per_ticker": 16, "search_document_max_items": 7,
    }})
    if use_sparse_override:
        request = ExecutionOptions(toolset_config_overrides=(ToolsetConfigSnapshot(
            "fins", payload={"retrieval_budget_per_ticker": 12},
        ),))
    else:
        request = ExecutionOptions(fins_tool_limits=FinsToolLimits(retrieval_budget_per_ticker=12))
    restored = deserialize_execution_options_snapshot(serialize_execution_options_snapshot(request))
    assert restored is not None
    merged = merge_execution_options(base_options=base, workspace_dir=tmp_path, execution_options=restored)
    limits = resolve_fins_tool_limits_from_toolset_configs(merged.toolset_configs)
    assert limits is not None and limits.retrieval_budget_per_ticker == 12
    assert limits.search_document_max_items == (7 if use_sparse_override else 20)
