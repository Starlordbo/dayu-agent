"""Service 启动准备公共 API 测试。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from dayu.execution.options import ExecutionOptions, ResolvedExecutionOptions
from dayu.contracts.infrastructure import StructuredConfigObject
from dayu.fins.service_runtime import DefaultFinsRuntime
from dayu.host import Host
from dayu.services.host_admin_service import HostAdminService
from dayu.services.scene_execution_acceptance import SceneExecutionAcceptancePreparer
from dayu.services.startup_preparation import prepare_host_runtime_dependencies
from dayu.startup.workspace import WorkspaceResources


@pytest.mark.unit
def test_prepare_host_runtime_dependencies_runs_unified_startup_recovery(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """共享 Host 启动准备复用同一管理服务执行恢复并供入口注入。

    参数:
        monkeypatch: 装配依赖替换工具。
        tmp_path: 独立工作区。
    返回值:
        无。
    异常:
        AssertionError: 恢复与返回依赖未复用相同 Host 管理服务。
    """

    fake_paths = SimpleNamespace(
        workspace_root=tmp_path,
        config_root=tmp_path / "config",
        output_dir=tmp_path / "output",
    )
    fake_workspace = cast(WorkspaceResources, object())
    fake_model_catalog = object()
    fake_default_execution_options = cast(ResolvedExecutionOptions, object())
    fake_scene_preparer = cast(SceneExecutionAcceptancePreparer, object())
    fake_fins_runtime = cast(DefaultFinsRuntime, object())
    fake_host = cast(Host, object())
    recover_calls: list[tuple[HostAdminService, str, str]] = []

    monkeypatch.setattr(
        "dayu.services.startup_preparation.resolve_startup_paths",
        lambda **_kwargs: fake_paths,
    )
    monkeypatch.setattr(
        "dayu.services.startup_preparation.ConfigFileResolver",
        lambda _config_root: object(),
    )
    monkeypatch.setattr(
        "dayu.services.startup_preparation.ConfigLoader",
        lambda _resolver: SimpleNamespace(load_run_config=lambda: {}),
    )
    monkeypatch.setattr(
        "dayu.services.startup_preparation.FilePromptAssetStore",
        lambda _resolver: object(),
    )
    monkeypatch.setattr(
        "dayu.services.startup_preparation.WorkspaceResources",
        lambda **_kwargs: fake_workspace,
    )
    monkeypatch.setattr(
        "dayu.services.startup_preparation.ConfigLoaderModelCatalog",
        lambda _config_loader: fake_model_catalog,
    )
    monkeypatch.setattr(
        "dayu.services.startup_preparation.build_base_execution_options",
        lambda **_kwargs: object(),
    )
    monkeypatch.setattr(
        "dayu.services.startup_preparation.merge_execution_options",
        lambda **_kwargs: fake_default_execution_options,
    )
    monkeypatch.setattr(
        "dayu.services.startup_preparation.prepare_scene_execution_acceptance_preparer",
        lambda **_kwargs: fake_scene_preparer,
    )
    monkeypatch.setattr(
        "dayu.services.startup_preparation.DefaultFinsRuntime.create",
        lambda **_kwargs: fake_fins_runtime,
    )
    monkeypatch.setattr(
        "dayu.services.startup_preparation.resolve_host_config",
        lambda **_kwargs: SimpleNamespace(
            store_path=tmp_path / "host.sqlite3",
            lane_config={"llm_api": 1},
            pending_turn_resume_max_attempts=3,
            pending_turn_retention_hours=168,
            cancellation_bridge_poll_interval_seconds=0.5,
            cancellation_bridge_failure_grace_period_seconds=5.0,
        ),
    )
    monkeypatch.setattr("dayu.services.startup_preparation.Host", lambda **_kwargs: fake_host)
    monkeypatch.setattr(
        "dayu.services.startup_preparation.recover_host_startup_state",
        lambda host_admin_service, *, runtime_label, log_module: recover_calls.append(
            (host_admin_service, runtime_label, log_module)
        ),
    )

    prepared = prepare_host_runtime_dependencies(
        workspace_root=tmp_path,
        config_root=tmp_path / "config",
        execution_options=ExecutionOptions(),
        runtime_label="Shared Host runtime",
        log_module="APP.TEST",
    )

    assert prepared.workspace is fake_workspace
    assert prepared.default_execution_options is fake_default_execution_options
    assert prepared.scene_execution_acceptance_preparer is fake_scene_preparer
    assert prepared.host is fake_host
    assert prepared.fins_runtime is fake_fins_runtime
    assert isinstance(prepared.host_admin_service, HostAdminService)
    assert prepared.host_admin_service.host is fake_host
    assert recover_calls == [(prepared.host_admin_service, "Shared Host runtime", "APP.TEST")]


@pytest.mark.unit
def test_prepare_host_runtime_dependencies_loads_run_config_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """共享 Host 启动准备应复用同一次加载得到的 run_config。"""

    fake_paths = SimpleNamespace(
        workspace_root=tmp_path,
        config_root=tmp_path / "config",
        output_dir=tmp_path / "output",
    )
    fake_workspace = cast(WorkspaceResources, object())
    fake_model_catalog = object()
    fake_default_execution_options = cast(ResolvedExecutionOptions, object())
    fake_scene_preparer = cast(SceneExecutionAcceptancePreparer, object())
    fake_fins_runtime = cast(DefaultFinsRuntime, object())
    fake_host = cast(Host, object())
    fake_run_config: StructuredConfigObject = {"chat_stream_config": {"first_chunk_timeout_seconds": 123.0}}
    load_run_config_calls: list[object] = []
    build_base_run_configs: list[object] = []
    resolve_host_run_configs: list[object] = []

    monkeypatch.setattr(
        "dayu.services.startup_preparation.resolve_startup_paths",
        lambda **_kwargs: fake_paths,
    )
    monkeypatch.setattr(
        "dayu.services.startup_preparation.ConfigFileResolver",
        lambda _config_root: object(),
    )

    class _FakeConfigLoader:
        """记录 `load_run_config()` 调用次数的测试桩。"""

        def load_run_config(self) -> StructuredConfigObject:
            """返回共享的测试配置。

            Args:
                无。
            Returns:
                同一运行配置对象。
            Raises:
                无。
            """

            load_run_config_calls.append(object())
            return fake_run_config

    monkeypatch.setattr(
        "dayu.services.startup_preparation.ConfigLoader",
        lambda _resolver: _FakeConfigLoader(),
    )
    monkeypatch.setattr(
        "dayu.services.startup_preparation.FilePromptAssetStore",
        lambda _resolver: object(),
    )
    monkeypatch.setattr(
        "dayu.services.startup_preparation.WorkspaceResources",
        lambda **_kwargs: fake_workspace,
    )
    monkeypatch.setattr(
        "dayu.services.startup_preparation.ConfigLoaderModelCatalog",
        lambda _config_loader: fake_model_catalog,
    )
    monkeypatch.setattr(
        "dayu.services.startup_preparation.build_base_execution_options",
        lambda **kwargs: build_base_run_configs.append(kwargs["run_config"]) or object(),
    )
    monkeypatch.setattr(
        "dayu.services.startup_preparation.merge_execution_options",
        lambda **_kwargs: fake_default_execution_options,
    )
    monkeypatch.setattr(
        "dayu.services.startup_preparation.prepare_scene_execution_acceptance_preparer",
        lambda **_kwargs: fake_scene_preparer,
    )
    monkeypatch.setattr(
        "dayu.services.startup_preparation.DefaultFinsRuntime.create",
        lambda **_kwargs: fake_fins_runtime,
    )
    monkeypatch.setattr(
        "dayu.services.startup_preparation.resolve_host_config",
        lambda **kwargs: resolve_host_run_configs.append(kwargs["run_config"]) or SimpleNamespace(
            store_path=tmp_path / "host.sqlite3",
            lane_config={"llm_api": 1},
            pending_turn_resume_max_attempts=3,
            pending_turn_retention_hours=168,
            cancellation_bridge_poll_interval_seconds=0.5,
            cancellation_bridge_failure_grace_period_seconds=5.0,
        ),
    )
    monkeypatch.setattr("dayu.services.startup_preparation.Host", lambda **_kwargs: fake_host)
    monkeypatch.setattr(
        "dayu.services.startup_preparation.recover_host_startup_state",
        lambda *_args, **_kwargs: None,
    )

    prepared = prepare_host_runtime_dependencies(
        workspace_root=tmp_path,
        config_root=tmp_path / "config",
        execution_options=ExecutionOptions(),
        runtime_label="Shared Host runtime",
        log_module="APP.TEST",
    )

    assert prepared.host is fake_host
    assert len(load_run_config_calls) == 1
    assert prepared.chat_stream_config.first_chunk_timeout_seconds == 123.0
    assert build_base_run_configs == [fake_run_config]
    assert resolve_host_run_configs == [fake_run_config]
