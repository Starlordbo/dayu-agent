"""Responses 的离线协议、工具闭环与故障测试；HTTP 全部使用桩。"""

from __future__ import annotations

import asyncio
import json
from typing import AsyncIterator, cast

import pytest

from dayu.contracts.cancellation import CancellationToken, CancelledError
from dayu.contracts.agent_types import AgentMessage, JsonValue
from dayu.contracts.model_config import RunnerType, TokenPrices
from dayu.engine.async_agent import AsyncAgent
from dayu.engine.events import EventType
from dayu.engine.responses_protocol import build_responses_payload, normalize_response, is_quota_error
from tests.engine.test_async_openai_runner_call_paths import (
    _build_runner_with_steps,
    _PostStep,
    _FakeResponse,
    _DummyExecutor,
    _FakeContent,
)


def response(output: list[JsonValue], status: str = "completed") -> dict[str, JsonValue]:
    """构造固定模型回执。

    Args:
        output: 原生输出。
        status: 完成状态。
    Returns:
        无网络响应。
    Raises:
        无。
    """
    return {
        "id": "resp_test",
        "status": status,
        "model": "actual-model",
        "output": output,
        "usage": {
            "input_tokens": 20,
            "output_tokens": 5,
            "total_tokens": 25,
            "input_tokens_details": {"cached_tokens": 10},
        },
    }


def text_item(text: str) -> dict[str, JsonValue]:
    """构造文本项。

    Args:
        text: 答案。
    Returns:
        原生消息。
    Raises:
        无。
    """
    return {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("reason", [None, "max_output_tokens", "content_filter"])
async def test_empty_response_preserves_terminal_and_usage(
    monkeypatch: pytest.MonkeyPatch, stream: bool, reason: str | None
) -> None:
    """正文为空时仍保留终态、原生推理项和已发生的 token 用量。

    Args:
        monkeypatch: 替换模型 HTTP。
        stream: 是否采用流式响应。
        reason: 未完成原因；None 表示正常完成。
    Returns:
        无。
    Raises:
        AssertionError: 终态、原生状态或用量丢失。
    """
    reasoning: dict[str, JsonValue] = {
        "type": "reasoning", "id": "rs_empty", "encrypted_content": "opaque", "summary": []
    }
    body = response([reasoning], "incomplete" if reason else "completed")
    if reason:
        body["incomplete_details"] = {"reason": reason}
    runner, posts = _build_runner_with_steps(monkeypatch, [step(body, stream)])
    runner.api_protocol = RunnerType.OPENAI_RESPONSES
    events = [event async for event in runner.call([{"role": "user", "content": "x"}], stream=stream)]
    await runner.close()
    assert len(posts) == 1
    assert not [event for event in events if event.type == EventType.ERROR]
    complete = next(event for event in events if event.type == EventType.CONTENT_COMPLETE)
    assert complete.metadata["responses_output"] == [reasoning]
    done = next(event for event in events if event.type == EventType.DONE)
    assert done.data["truncated"] is (reason == "max_output_tokens")
    assert done.data["content_filtered"] is (reason == "content_filter")
    usage = next(
        event.data["token_usage_summary"]
        for event in events
        if event.type == EventType.METADATA and "token_usage_summary" in event.data
    )
    assert usage["prompt_tokens"] == 20
    assert usage["completion_tokens"] == 5


def call_item(call_id: str) -> dict[str, JsonValue]:
    """构造工具项。

    Args:
        call_id: 工具回传标识。
    Returns:
        原生调用，item id 与 call id 故意不同。
    Raises:
        无。
    """
    return {
        "id": "item_" + call_id,
        "type": "function_call",
        "call_id": call_id,
        "name": "lookup",
        "arguments": json.dumps({"query": call_id}),
        "status": "completed",
    }


def step(body: dict[str, JsonValue], stream: bool) -> _PostStep:
    """构造 JSON 或跨字节分片 SSE。

    Args:
        body: 终态。
        stream: 是否流式。
    Returns:
        HTTP 桩。
    Raises:
        无。
    """
    if not stream:
        return _PostStep(
            response=_FakeResponse(status=200, headers={"Content-Type": "application/json"}, json_data=body)
        )
    raw = (
        "data: "
        + json.dumps({"type": "response." + str(body["status"]), "response": body}, ensure_ascii=False)
        + "\n\n"
    ).encode()
    return _PostStep(
        response=_FakeResponse(
            status=200,
            headers={"Content-Type": "text/event-stream"},
            chunks=[raw[i : i + 7] for i in range(0, len(raw), 7)],
        )
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_agent_multiple_tool_roundtrips(monkeypatch: pytest.MonkeyPatch, stream: bool) -> None:
    """验证原 Agent 两轮工具及最终回答，原生状态和 call_id 完整回传。

    Args:
        monkeypatch: 替换 HTTP。
        stream: 两种协议交付方式。
    Returns:
        无。
    Raises:
        AssertionError: 闭环不完整。
    """
    reasoning: dict[str, JsonValue] = {
        "type": "reasoning",
        "id": "rs_1",
        "encrypted_content": "opaque-secret",
        "summary": [],
    }
    outputs = [[reasoning, call_item("a"), call_item("b")], [call_item("c")], [text_item("最终答案")]]
    runner, posts = _build_runner_with_steps(
        monkeypatch, [step(response(cast(list[JsonValue], items)), stream) for items in outputs]
    )
    runner.api_protocol = RunnerType.OPENAI_RESPONSES
    agent = AsyncAgent(
        runner=runner,
        tool_executor=_DummyExecutor(
            [
                {
                    "type": "function",
                    "function": {
                        "name": "lookup",
                        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
                    },
                }
            ]
        ),
    )
    events = [event async for event in agent.run("查询", stream=stream)]
    assert not [event for event in events if event.type == EventType.ERROR]
    assert len(posts) == 3
    assert posts[1]["json"]["input"][1] == reasoning
    items = posts[1]["json"]["input"]
    assert [item["call_id"] for item in items if item.get("type") == "function_call_output"] == ["a", "b"]
    assert len([item for item in posts[2]["json"]["input"] if item.get("type") == "function_call_output"]) == 3
    assert posts[0]["json"]["tools"][0]["strict"] is False
    assert "messages" not in posts[0]["json"]
    assert "temperature" not in posts[0]["json"]
    assert posts[0]["json"]["store"] is False
    assert any(event.type == EventType.FINAL_ANSWER and event.data["content"] == "最终答案" for event in events)
    assert "opaque-secret" not in "".join(
        str(event.data)
        for event in events
        if event.type in {EventType.CONTENT_DELTA, EventType.REASONING_DELTA, EventType.FINAL_ANSWER}
    )
    summary = next(
        event.data["run_usage_summary"]
        for event in events
        if event.type == EventType.METADATA and "run_usage_summary" in event.data
    )
    assert summary["request_count"] == 3
    assert summary["input_tokens"] == 60
    assert summary["cached_tokens"] == 30


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", [RunnerType.OPENAI_RESPONSES, RunnerType.OPENAI_COMPATIBLE])
async def test_quota_stops_without_retry(monkeypatch: pytest.MonkeyPatch, protocol: RunnerType) -> None:
    """两协议均不重试额度耗尽。

    Args:
        monkeypatch: HTTP 替换。
        protocol: 模型协议。
    Returns:
        无。
    Raises:
        AssertionError: 多发请求或分类错误。
    """
    runner, posts = _build_runner_with_steps(
        monkeypatch,
        [_PostStep(response=_FakeResponse(status=429, text_data='{"error":{"code":"insufficient_quota"}}'))],
    )
    runner.api_protocol = protocol
    events = [event async for event in runner.call([{"role": "user", "content": "x"}])]
    await runner.close()
    assert len(posts) == 1
    assert events[-1].metadata["error_type"] == "insufficient_quota"


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_incomplete_tool_never_runs(monkeypatch: pytest.MonkeyPatch, stream: bool) -> None:
    """输出截断时禁止执行看似完整的工具。

    Args:
        monkeypatch: HTTP 替换。
        stream: 传输模式。
    Returns:
        无。
    Raises:
        AssertionError: 工具被派发。
    """
    body = response([call_item("a")], "incomplete")
    body["incomplete_details"] = {"reason": "max_output_tokens"}
    runner, posts = _build_runner_with_steps(monkeypatch, [step(body, stream)])
    runner.api_protocol = RunnerType.OPENAI_RESPONSES
    runner.set_tools(_DummyExecutor())
    events = [event async for event in runner.call([{"role": "user", "content": "x"}], stream=stream)]
    await runner.close()
    assert len(posts) == 1
    assert any(event.type == EventType.ERROR for event in events)
    assert not any(event.type == EventType.TOOL_CALL_DISPATCHED for event in events)


@pytest.mark.asyncio
async def test_stream_missing_terminal_preserves_text(monkeypatch: pytest.MonkeyPatch) -> None:
    """中断流保留已发出的文本且不误报完成。

    Args:
        monkeypatch: HTTP 替换。
    Returns:
        无。
    Raises:
        AssertionError: 错误未识别。
    """
    raw = b'data: {"type":"response.output_text.delta","delta":"partial"}\n\n'
    runner, _ = _build_runner_with_steps(
        monkeypatch,
        [_PostStep(response=_FakeResponse(status=200, headers={"Content-Type": "text/event-stream"}, chunks=[raw]))],
    )
    runner.api_protocol = RunnerType.OPENAI_RESPONSES
    events = [event async for event in runner.call([{"role": "user", "content": "x"}])]
    await runner.close()
    assert any(event.type == EventType.CONTENT_DELTA and event.data == "partial" for event in events)
    assert any(event.type == EventType.ERROR for event in events)
    assert not any(event.type == EventType.DONE for event in events)


def test_conversion_and_validation() -> None:
    """覆盖消息转换、拒答、用量和非法回执。

    Args:
        无。
    Returns:
        无。
    Raises:
        AssertionError: 规范化错误。
    """
    messages: list[AgentMessage] = [
        {"role": "system", "content": "s"},
        {
            "role": "assistant",
            "content": "a",
            "tool_calls": [{"id": "c", "type": "function", "function": {"name": "lookup", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "c", "content": "ok"},
    ]
    payload = build_responses_payload({"model": "x"}, messages)
    assert len(cast(list[JsonValue], payload["input"])) == 4
    assert not is_quota_error("not-json")
    assert not is_quota_error('["insufficient_quota"]')
    assert not is_quota_error('{"error":{"code":"rate_limit_exceeded"}}')
    for invalid in (
        {},
        {"status": "completed", "output": [1]},
        {"status": "completed", "output": [{"type": "unknown"}]},
    ):
        with pytest.raises(ValueError):
            normalize_response(cast(dict[str, JsonValue], invalid))


@pytest.mark.asyncio
async def test_rate_limit_retry_and_cost_accounting(monkeypatch: pytest.MonkeyPatch) -> None:
    """限流重试计入整题请求数，未知失败用量不得标成完整费用。

    Args:
        monkeypatch: HTTP 桩。
    Returns:
        无。
    Raises:
        AssertionError: 计数或估价错误。
    """
    runner, posts = _build_runner_with_steps(
        monkeypatch,
        [
            _PostStep(
                response=_FakeResponse(
                    status=429, headers={"Retry-After": "0"}, text_data='{"error":{"code":"rate_limit_exceeded"}}'
                )
            ),
            step(response([text_item("done")]), False),
        ],
    )
    runner.api_protocol = RunnerType.OPENAI_RESPONSES
    prices: TokenPrices = {"input": 2.0, "cached_input": 1.0, "output": 4.0}
    runner.token_prices = prices
    events = [event async for event in AsyncAgent(runner=runner).run("x", stream=False)]
    assert len(posts) == 2
    usage = next(event for event in events if event.type == EventType.METADATA and "token_usage_summary" in event.data)
    assert usage.metadata["model"] == "actual-model"
    assert usage.data["token_usage_summary"]["cost_estimate_usd"] == pytest.approx(0.00005)
    summary = next(
        event.data["run_usage_summary"]
        for event in events
        if event.type == EventType.METADATA and "run_usage_summary" in event.data
    )
    assert summary["request_count"] == 2
    assert summary["requests_with_usage"] == 1
    assert summary["cost_estimate_usd"] is None
    assert summary["known_cost_estimate_usd"] == pytest.approx(0.00005)


@pytest.mark.asyncio
async def test_cancel_in_retry_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Responses 在退避期间立即响应取消，不发第二次请求。

    Args:
        monkeypatch: HTTP 桩。
    Returns:
        无。
    Raises:
        AssertionError: 取消失效。
    """
    token = CancellationToken()
    runner, posts = _build_runner_with_steps(
        monkeypatch,
        [_PostStep(response=_FakeResponse(status=429, headers={"Retry-After": "30"}, text_data="rate limited"))],
        cancellation_token=token,
    )
    runner.api_protocol = RunnerType.OPENAI_RESPONSES
    events = runner.call([{"role": "user", "content": "x"}])
    await anext(events)
    assert (await anext(events)).type == EventType.WARNING
    token.cancel()
    with pytest.raises(CancelledError):
        await anext(events)
    await runner.close()
    assert len(posts) == 1


@pytest.mark.asyncio
async def test_stream_deltas_and_completed_reasoning(monkeypatch: pytest.MonkeyPatch) -> None:
    """使用 output_item.done 的完整 reasoning，并避免重复文本。

    Args:
        monkeypatch: HTTP 桩。
    Returns:
        无。
    Raises:
        AssertionError: 丢失状态或重复输出。
    """
    complete: dict[str, JsonValue] = {"type": "reasoning", "id": "rs", "encrypted_content": "complete", "summary": []}
    frames = [
        {"type": "response.output_item.done", "output_index": 0, "item": complete},
        {"type": "response.output_text.delta", "delta": "你好"},
        {
            "type": "response.completed",
            "response": response([{"type": "reasoning", "id": "rs", "summary": []}, text_item("你好世界")]),
        },
    ]
    raw = "".join("data: " + json.dumps(frame, ensure_ascii=False) + "\n\n" for frame in frames).encode()
    runner, _ = _build_runner_with_steps(
        monkeypatch,
        [
            _PostStep(
                response=_FakeResponse(
                    status=200,
                    headers={"Content-Type": "text/event-stream"},
                    chunks=[raw[i : i + 1] for i in range(len(raw))],
                )
            )
        ],
    )
    runner.api_protocol = RunnerType.OPENAI_RESPONSES
    events = [event async for event in runner.call([{"role": "user", "content": "x"}])]
    await runner.close()
    assert "".join(event.data for event in events if event.type == EventType.CONTENT_DELTA) == "你好世界"
    completed = next(event for event in events if event.type == EventType.CONTENT_COMPLETE)
    assert completed.metadata["responses_output"][0] == complete


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        "not-json",
        "[]",
        '{"type":"response.output_text.delta","delta":42}',
        '{"type":"response.output_item.done"}',
        '{"type":"response.failed","response":{"error":{"code":"insufficient_quota"}}}',
    ],
)
async def test_invalid_and_failed_stream(monkeypatch: pytest.MonkeyPatch, body: str) -> None:
    """非法或失败流统一停止，不执行工具。

    Args:
        monkeypatch: HTTP 桩。
        body: 错误事件。
    Returns:
        无。
    Raises:
        AssertionError: 未识别失败。
    """
    raw = ("data: " + body + "\n\n").encode()
    runner, _ = _build_runner_with_steps(
        monkeypatch,
        [_PostStep(response=_FakeResponse(status=200, headers={"Content-Type": "text/event-stream"}, chunks=[raw]))],
    )
    runner.api_protocol = RunnerType.OPENAI_RESPONSES
    events = [event async for event in runner.call([{"role": "user", "content": "x"}])]
    await runner.close()
    assert any(event.type == EventType.ERROR for event in events)
    assert not any(event.type == EventType.TOOL_CALL_DISPATCHED for event in events)


@pytest.mark.parametrize("reason,finish", [("max_output_tokens", "length"), ("content_filter", "content_filter")])
def test_incomplete_text_and_refusal(reason: str, finish: str) -> None:
    """文本截断与拒答按内部终态映射。

    Args:
        reason: 未完成原因。
        finish: 预期内部原因。
    Returns:
        无。
    Raises:
        AssertionError: 转换错误。
    """
    body = response([{"type": "message", "content": [{"type": "refusal", "refusal": "拒答"}]}], "incomplete")
    body["incomplete_details"] = {"reason": reason}
    normalized = normalize_response(body)
    choice = cast(list[dict[str, JsonValue]], normalized["choices"])[0]
    assert choice["finish_reason"] == finish
    assert cast(dict[str, JsonValue], choice["message"])["content"] == "拒答"


async def blocked_chunks(content: _FakeContent, size: int) -> AsyncIterator[bytes]:
    """发出文本后阻塞，供测试取消等待中的 SSE 读取。

    Args:
        content: 测试内容对象。
        size: 分片参数。
    Returns:
        字节流。
    Raises:
        asyncio.CancelledError: 读取被取消。
    """
    del content, size
    yield b'data: {"type":"response.output_text.delta","delta":"partial"}\n\n'
    await asyncio.Event().wait()


@pytest.mark.asyncio
async def test_cancel_blocked_stream_preserves_content(monkeypatch: pytest.MonkeyPatch) -> None:
    """用户取消立即中断阻塞读取，保留已产生文本且不重试。

    Args:
        monkeypatch: HTTP 桩。
    Returns:
        无。
    Raises:
        AssertionError: 取消超时或内容丢失。
    """
    token = CancellationToken()
    monkeypatch.setattr(_FakeContent, "iter_chunked", blocked_chunks)
    runner, posts = _build_runner_with_steps(
        monkeypatch,
        [_PostStep(response=_FakeResponse(status=200, headers={"Content-Type": "text/event-stream"}))],
        cancellation_token=token,
    )
    runner.api_protocol = RunnerType.OPENAI_RESPONSES
    stream = runner.call([{"role": "user", "content": "x"}])
    await anext(stream)
    assert (await anext(stream)).data == "partial"
    token.cancel()
    with pytest.raises(CancelledError):
        await asyncio.wait_for(anext(stream), timeout=1)
    await runner.close()
    assert len(posts) == 1


@pytest.mark.asyncio
async def test_quota_after_tool_keeps_partial_and_stops(monkeypatch: pytest.MonkeyPatch) -> None:
    """工具往返后的额度错误不触发强制回答或换模型。

    Args:
        monkeypatch: HTTP 桩。
    Returns:
        无。
    Raises:
        AssertionError: 错误后继续请求。
    """
    runner, posts = _build_runner_with_steps(
        monkeypatch,
        [
            step(response([text_item("已有内容"), call_item("a")]), False),
            _PostStep(response=_FakeResponse(status=429, text_data='{"error":{"type":"insufficient_quota"}}')),
        ],
    )
    runner.api_protocol = RunnerType.OPENAI_RESPONSES
    events = [event async for event in AsyncAgent(runner=runner, tool_executor=_DummyExecutor()).run("x", stream=False)]
    assert len(posts) == 2
    assert events[-1].type == EventType.ERROR
    assert any(event.type == EventType.CONTENT_DELTA and event.data == "已有内容" for event in events)
