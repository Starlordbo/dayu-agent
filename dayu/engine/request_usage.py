"""整题请求及 token 聚合；未知用量不当作零费用。"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from dayu.engine.events import EventType, StreamEvent
from dayu.contracts.agent_types import JsonValue


@dataclass
class RunRequestUsage:
    """累计每个 HTTP 尝试及其已知用量，不包含任何凭据或模型输入。"""

    started_at: float = field(default_factory=time.monotonic)
    requests: set[str] = field(default_factory=set)
    usage_requests: set[str] = field(default_factory=set)
    input_tokens: int = 0
    cached_tokens: int = 0
    output_tokens: int = 0
    priced_requests: int = 0
    cost_estimate_usd: float = 0.0

    def observe(self, event: StreamEvent) -> None:
        """消费请求开始与用量事件，重复事件只计一次。

        Args:
            event: Runner 事件。
        Returns:
            无。
        Raises:
            无。
        """
        if event.type != EventType.METADATA or not isinstance(event.data, dict):
            return
        value = event.data.get("token_usage_summary")
        request_id = str(event.metadata.get("request_id", ""))
        if "request_attempt" in event.data:
            self.requests.add(f"{request_id}:{event.metadata.get('attempt', 1)}")
        if isinstance(value, dict) and request_id not in self.usage_requests:
            self.usage_requests.add(request_id)
            self.input_tokens += int(value.get("prompt_tokens") or 0)
            self.output_tokens += int(value.get("completion_tokens") or 0)
            self.cached_tokens += int(value.get("cached_tokens") or 0)
            estimate = value.get("cost_estimate_usd")
            if isinstance(estimate, (int, float)):
                self.priced_requests += 1
                self.cost_estimate_usd += estimate

    def summary(self) -> dict[str, JsonValue]:
        """返回整题耗时及请求计数，不宣称供应商账单费用。

        Args:
            无。
        Returns:
            可序列化统计；缺少价格或失败请求用量时费用不可用。
        Raises:
            无。
        """
        complete = self.priced_requests == len(self.requests) and bool(self.requests)
        return {
            "elapsed_seconds": time.monotonic() - self.started_at,
            "request_count": len(self.requests),
            "requests_with_usage": len(self.usage_requests),
            "input_tokens": self.input_tokens,
            "cached_tokens": self.cached_tokens,
            "output_tokens": self.output_tokens,
            "cost_estimate_usd": self.cost_estimate_usd if complete else None,
            "known_cost_estimate_usd": self.cost_estimate_usd if self.priced_requests else None,
            "cost_status": "estimate_only" if complete else "price_table_or_complete_usage_unavailable",
        }
