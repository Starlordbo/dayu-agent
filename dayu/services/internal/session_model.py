"""会话模型选择：复用 Host 元数据，拒绝在续聊时变更模型配置。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace

from dayu.contracts.execution_metadata import ExecutionDeliveryContext
from dayu.execution.options import ExecutionOptions
from dayu.host.protocols import SessionOperationsProtocol
from dayu.services.scene_execution_acceptance import AcceptedSceneExecution


def restore_session_model(
    host: SessionOperationsProtocol, session_id: str | None, options: ExecutionOptions | None
) -> ExecutionOptions | None:
    """从会话恢复模型选择；显式更换必须新建会话。

    Args:
        host: 会话公共接口。
        session_id: 已有会话 ID。
        options: 本次请求选项。
    Returns:
        带持久化模型名的选项。
    Raises:
        ValueError: 试图切换已有会话模型。
    """
    session = host.get_session(session_id) if session_id else None
    selected = session.metadata.get("selected_model_name") if session else None
    if not selected:
        return options
    if options and options.model_name and options.model_name != selected:
        raise ValueError("切换模型必须新建会话")
    return replace(options or ExecutionOptions(), model_name=selected)


def session_model_metadata(scene: AcceptedSceneExecution) -> ExecutionDeliveryContext:
    """生成不含凭据的模型绑定信息。

    Args:
        scene: 已通过场景校验的配置。
    Returns:
        模型名称及协议、地址、实际模型 ID 的指纹。
    Raises:
        无。
    """
    config = scene.model_config
    identity = {key: config.get(key) for key in ("runner_type", "endpoint_url", "model")}
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    return {"selected_model_name": scene.accepted_execution_spec.model.model_name, "model_config_fingerprint": digest}
