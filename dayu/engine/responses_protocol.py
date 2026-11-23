"""Responses 协议转换；复用既有 HTTP 生命周期、SSE 分帧与工具执行器。"""

from __future__ import annotations

import json
from copy import deepcopy
from typing import AsyncIterator, cast

from dayu.contracts.agent_types import AgentMessage, JsonValue
from dayu.engine.events import StreamEvent
from dayu.engine.sse_parser import SSEParseResult, SSEStreamParser

NATIVE_OUTPUT_KEY = "responses_output"
_TERMINAL_EVENTS = frozenset({"response.completed", "response.incomplete", "response.failed"})
_QUOTA_CODES = frozenset(
    {
        "insufficient_quota",
        "billing_hard_limit_reached",
        "billing_not_active",
        "quota_exceeded",
        "credit_balance_too_low",
    }
)


def is_quota_error(body: str) -> bool:
    """识别供应商明确返回的硬配额错误，不把普通限流当作余额不足。

    Args:
        body: HTTP 错误 JSON。
    Returns:
        是否不可重试。
    Raises:
        无；非 JSON 返回 False。
    """
    try:
        value = cast(JsonValue, json.loads(body))
    except ValueError:
        return False
    if not isinstance(value, dict):
        return False
    error = value.get("error", value)
    return isinstance(error, dict) and any(
        error.get(key) in _QUOTA_CODES for key in ("code", "type") if isinstance(error.get(key), str)
    )


def build_responses_payload(payload: dict[str, JsonValue], messages: list[AgentMessage]) -> dict[str, JsonValue]:
    """把内部消息转成无服务端存储的 Responses 输入。

    Args:
        payload: Runner 已收敛的参数与工具定义。
        messages: 当前上下文，原生输出按原顺序回传。
    Returns:
        Responses 请求；不会改写输入。
    Raises:
        ValueError: 工具 schema 不合法时。
    """
    result = {
        key: value
        for key, value in payload.items()
        if key not in {"messages", "temperature", "stream_options", "tools"}
    }
    inputs: list[JsonValue] = []
    for message in messages:
        role = message.get("role")
        if role == "assistant" and message.get(NATIVE_OUTPUT_KEY):
            inputs.extend(deepcopy(cast(list[dict[str, JsonValue]], message.get(NATIVE_OUTPUT_KEY))))
        elif role == "tool":
            inputs.append(
                {
                    "type": "function_call_output",
                    "call_id": message.get("tool_call_id", ""),
                    "output": message.get("content", ""),
                }
            )
        else:
            content = message.get("content")
            if content:
                inputs.append({"role": role, "content": content})
            if role == "assistant":
                for call in message.get("tool_calls", []):
                    inputs.append(
                        {
                            "type": "function_call",
                            "call_id": call["id"],
                            "name": call["function"]["name"],
                            "arguments": call["function"]["arguments"],
                        }
                    )
    result["input"] = inputs
    result["store"] = False
    result["include"] = ["reasoning.encrypted_content"]
    tools = payload.get("tools", [])
    flattened: list[JsonValue] = []
    if not isinstance(tools, list):
        raise ValueError("tools 必须为列表")
    for tool in tools:
        if not isinstance(tool, dict) or not isinstance(tool.get("function"), dict):
            raise ValueError("Responses 仅支持已注册的 function 工具")
        function = cast(dict[str, JsonValue], tool["function"])
        # Dayu 的可选参数 schema 不满足 Responses 隐式 strict 要求。
        flattened.append({**function, "type": "function", "strict": False})
    if flattened:
        result["tools"] = flattened
    return result


def normalize_response(response: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """把完整 Responses 输出转换为 Runner 的内部完成结果。

    Args:
        response: 原生响应。
    Returns:
        文本、工具、用量和不透明原生项；reasoning 不进入文本。
    Raises:
        ValueError: 缺少终态、输出结构错误或工具未完成。
    """
    status = response.get("status")
    if status not in {"completed", "incomplete"}:
        raise ValueError("Responses 未成功终止")
    output = response.get("output")
    if not isinstance(output, list):
        raise ValueError("Responses 缺少 output 列表")
    texts: list[str] = []
    calls: list[JsonValue] = []
    for item in output:
        if not isinstance(item, dict):
            raise ValueError("Responses output 条目必须为对象")
        kind = item.get("type")
        if kind == "message":
            parts = item.get("content", [])
            if not isinstance(parts, list):
                raise ValueError("Responses message.content 必须为列表")
            for part in parts:
                if isinstance(part, dict):
                    text = part.get("text") if part.get("type") == "output_text" else part.get("refusal")
                    if isinstance(text, str):
                        texts.append(text)
        elif kind == "function_call":
            if status != "completed" or item.get("status", "completed") != "completed":
                raise ValueError("Responses 工具调用未完成，禁止执行")
            calls.append(
                {
                    "id": item.get("call_id"),
                    "type": "function",
                    "function": {"name": item.get("name"), "arguments": item.get("arguments")},
                }
            )
        elif kind != "reasoning":
            raise ValueError(f"不支持的 Responses output 类型: {kind}")
    details = response.get("incomplete_details")
    reason = details.get("reason") if isinstance(details, dict) else None
    if status == "incomplete" and reason not in {"max_output_tokens", "content_filter"}:
        raise ValueError(f"Responses 未完成: {reason}")
    finish = (
        "length"
        if reason == "max_output_tokens"
        else "content_filter" if reason == "content_filter" else "tool_calls" if calls else "stop"
    )
    usage = response.get("usage")
    normalized_usage: dict[str, JsonValue] = {}
    if isinstance(usage, dict):
        normalized_usage = {
            "prompt_tokens": usage.get("input_tokens", 0),
            "completion_tokens": usage.get("output_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0),
            "prompt_tokens_details": usage.get("input_tokens_details", {}),
            "completion_tokens_details": usage.get("output_tokens_details", {}),
        }
    return {
        "model": response.get("model"),
        "choices": [{"message": {"content": "".join(texts), "tool_calls": calls}, "finish_reason": finish}],
        "usage": normalized_usage,
        NATIVE_OUTPUT_KEY: deepcopy(output),
    }


class ResponsesSSEParser(SSEStreamParser):
    """复用 SSE 分帧与取消；只转换 Responses 语义事件。"""

    def get_result(self) -> SSEParseResult:
        """取得终态并拒绝缺失终止事件的流。

        Args:
            无。
        Returns:
            规范化结果与原生输出。
        Raises:
            无。
        """
        result = super().get_result()
        if not self._stream_state.get("responses_terminal"):
            result.protocol_errors.append(
                {"error_type": "response_error", "message": "Responses stream ended without terminal event"}
            )
        return result

    async def _handle_payload(self, payload: str) -> AsyncIterator[StreamEvent]:
        """转换一条 SSE 事件，终态前不执行工具。

        Args:
            payload: SSE data 文本。
        Returns:
            内部增量事件流。
        Raises:
            无；协议错误写入解析结果。
        """
        try:
            value = cast(JsonValue, json.loads(payload))
            if not isinstance(value, dict):
                raise ValueError("Responses SSE 必须为对象")
            kind = value.get("type")
            if kind in {"response.output_text.delta", "response.refusal.delta"}:
                delta = value.get("delta")
                if not isinstance(delta, str):
                    raise ValueError("Responses 文本增量必须为字符串")
                async for event in super()._handle_payload(json.dumps({"choices": [{"delta": {"content": delta}}]})):
                    yield event
            elif kind == "response.output_item.done":
                index = value.get("output_index")
                item = value.get("item")
                if not isinstance(index, int) or not isinstance(item, dict):
                    raise ValueError("Responses output_item.done 缺少索引或条目")
                self._stream_state.setdefault("responses_items", {})[index] = item
            elif kind in _TERMINAL_EVENTS or kind == "error":
                self._stream_state["responses_terminal"] = True
                self._done_received = True
                response = value.get("response", value)
                if not isinstance(response, dict):
                    raise ValueError("Responses 终态缺少 response")
                self._stream_state["actual_model"] = response.get("model")
                if kind in {"response.failed", "error"}:
                    error_type = "insufficient_quota" if is_quota_error(json.dumps(response)) else "response_error"
                    self._record_protocol_error(error_type, "Responses 服务端返回失败")
                    return
                # done 项是完整加密 reasoning 的真源；added 项绝不回传。
                terminal_output = response.get("output")
                completed_items = self._stream_state.get("responses_items", {})
                if isinstance(terminal_output, list):
                    response = {
                        **response,
                        "output": [completed_items.get(index, item) for index, item in enumerate(terminal_output)],
                    }
                normalized = normalize_response(response)
                self._stream_state[NATIVE_OUTPUT_KEY] = normalized[NATIVE_OUTPUT_KEY]
                choices = cast(list[dict[str, JsonValue]], normalized["choices"])
                choice = choices[0]
                message = cast(dict[str, JsonValue], choice["message"])
                full_text = cast(str, message["content"])
                emitted = "".join(self._content_buffer)
                if not full_text.startswith(emitted):
                    raise ValueError("Responses 终态文本与增量不一致")
                calls = cast(list[dict[str, JsonValue]], message["tool_calls"])
                chunk = {
                    "model": normalized.get("model"),
                    "choices": [
                        {
                            "delta": {
                                "content": full_text[len(emitted) :],
                                "tool_calls": [{**call, "index": index} for index, call in enumerate(calls)],
                            },
                            "finish_reason": choice["finish_reason"],
                        }
                    ],
                    "usage": normalized["usage"],
                }
                async for event in super()._handle_payload(json.dumps(chunk)):
                    yield event
        except (ValueError, TypeError) as exc:
            self._record_protocol_error("response_error", str(exc))
