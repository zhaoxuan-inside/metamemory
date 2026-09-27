"""推理双通道（InferenceProvider，选型 #13）：嵌入式 + 网关，双向降级链。

分派键 = 任务档 × 资源档 × 隐私档；外部故障 → 嵌入式续跑；全程 llm_calls 打点。
"""
from __future__ import annotations

from typing import Any, Protocol

from ..container.policy import DependencyError


class InferenceProvider(Protocol):
    def complete(self, request: dict) -> dict: ...


class GatewayProvider:
    """外部强模型（LiteLLM 网关）：模型无关；骨架期未配置端点即视为外部故障。"""

    def __init__(self, endpoint: str | None = None, model: str = "default") -> None:
        self.endpoint = endpoint
        self.model = model

    def complete(self, request: dict) -> dict:
        if not self.endpoint:
            raise DependencyError("gateway_unavailable: 未配置外部模型端点")
        raise DependencyError("gateway_unavailable: 骨架期网关未实现（full 档接入 LiteLLM）")


class EmbeddedProvider:
    """进程内小模型（分类器/NLI/2B 生成）：零延迟零成本离线可用。

    骨架期为确定性回声实现：返回 prompt 原文供下游规则管道消费。
    """

    def complete(self, request: dict) -> dict:
        prompt = request.get("prompt", "")
        return {"text": prompt, "tokens": len(prompt) // 2, "provider": "embedded"}


class InferenceRouter:
    """双通道路由：优先嵌入任务集进程内终结，重任务外接；故障自动切换。"""

    def __init__(self, embedded: EmbeddedProvider | None = None, gateway: GatewayProvider | None = None) -> None:
        self.embedded = embedded or EmbeddedProvider()
        self.gateway = gateway or GatewayProvider()

    def complete(self, request: dict) -> dict:
        prefer = request.get("prefer", "gateway")
        order = [self.gateway, self.embedded] if prefer == "gateway" else [self.embedded, self.gateway]
        last_error: Exception | None = None
        for provider in order:
            try:
                response: dict[str, Any] = provider.complete(request)
                response["routed"] = type(provider).__name__
                return response
            except DependencyError as exc:
                last_error = exc
        raise last_error or DependencyError("inference_unavailable")
