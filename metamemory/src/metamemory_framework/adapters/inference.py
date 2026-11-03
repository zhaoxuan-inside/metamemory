"""推理适配器（ADR-006 单一最佳路径）：唯一 GatewayProvider，无降级链。

OpenAI 兼容 HTTP 网关（stdlib urllib 实现）：vLLM / Ollama / Ark / OpenAI 皆可接。
未配置端点 = 显式配置错误；网络故障 = DependencyError（重试/死信处置），不静默降质。
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Protocol

from ..container.policy import DependencyError


class InferenceProvider(Protocol):
    def complete(self, request: dict) -> dict: ...


class GatewayProvider:
    """强模型网关：POST {base_url}/chat/completions（OpenAI 兼容协议）。"""

    def __init__(self, base_url: str = "", api_key: str = "", model: str = "",
                 timeout_seconds: int = 60) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout_seconds = timeout_seconds
        # 推理网关属内部直连：绕过环境代理，避免 localhost 被外部代理劫持
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def complete(self, request: dict) -> dict:
        if not self.base_url or not self.model:
            raise DependencyError(
                "gateway_unconfigured: 需配置推理网关 base_url 与 model（ADR-006 唯一最佳路径，无降级）")
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": request.get("prompt", "")}],
            "temperature": request.get("temperature", 0.2),
        }
        http_req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     **({"Authorization": f"Bearer {self.api_key}"} if self.api_key else {})},
            method="POST",
        )
        try:
            with self._opener.open(http_req, timeout=self.timeout_seconds) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as exc:
            raise DependencyError(f"gateway_error: {type(exc).__name__}: {exc}") from exc
        return self._parse(data)

    def _parse(self, data: dict) -> dict:
        try:
            text = data["choices"][0]["message"]["content"]
            usage = data.get("usage", {})
        except (KeyError, IndexError, TypeError) as exc:
            raise DependencyError(f"gateway_bad_response: {exc}") from exc
        return {"text": text, "tokens": int(usage.get("total_tokens", 0)),
                "provider": "gateway", "model": self.model}


def load_provider(config: dict) -> GatewayProvider:
    """从 MetaMemoryConfig.inference 段装配唯一 Provider（组合根用）。"""
    return GatewayProvider(base_url=config.get("base_url", ""), api_key=config.get("api_key", ""),
                           model=config.get("model", ""),
                           timeout_seconds=int(config.get("timeout_seconds", 60)))
