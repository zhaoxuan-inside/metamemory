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
    """强模型网关（OpenAI 兼容协议）：chat/completions（提取/NLI 裁判/精排/摘要）
    + embeddings（语义向量）。未配置端点 = 显式配置错误；网络故障 = DependencyError。"""

    def __init__(self, base_url: str = "", api_key: str = "", model: str = "",
                 embedding_model: str = "", rerank_model: str = "",
                 timeout_seconds: int = 60) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.embedding_model = embedding_model
        self.rerank_model = rerank_model
        self.timeout_seconds = timeout_seconds
        # 推理网关属内部直连：绕过环境代理，避免 localhost 被外部代理劫持
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def complete(self, request: dict) -> dict:
        """chat 补全（task 标记仅用于缓存命名与替身分发，网关侧透明）。"""
        if not self.base_url or not self.model:
            raise DependencyError(
                "gateway_unconfigured: 需配置推理网关 base_url 与 model（ADR-006 唯一最佳路径，无降级）")
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": request.get("prompt", "")}],
            "temperature": request.get("temperature", 0.2),
        }
        return self._chat(payload)

    def embed(self, texts: list[str]) -> list[list[float]]:
        """批量向量：POST /embeddings（bge-m3 等任何 OpenAI 兼容嵌入端点）。"""
        if not self.base_url or not self.embedding_model:
            raise DependencyError(
                "embedding_unconfigured: 需配置 inference.embedding_model（语义路的显式配置前提）")
        payload = {"model": self.embedding_model, "input": texts}
        data = self._post(payload, "/embeddings")
        try:
            ordered = sorted(data["data"], key=lambda item: item["index"])
            return [item["embedding"] for item in ordered]
        except (KeyError, TypeError) as exc:
            raise DependencyError(f"embedding_bad_response: {exc}") from exc

    def _chat(self, payload: dict) -> dict:
        data = self._post(payload, "/chat/completions")
        try:
            text = data["choices"][0]["message"]["content"]
            usage = data.get("usage", {})
        except (KeyError, IndexError, TypeError) as exc:
            raise DependencyError(f"gateway_bad_response: {exc}") from exc
        return {"text": text, "tokens": int(usage.get("total_tokens", 0)),
                "provider": "gateway", "model": payload["model"]}

    def _post(self, payload: dict, path: str) -> dict:
        http_req = urllib.request.Request(
            f"{self.base_url}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     **({"Authorization": f"Bearer {self.api_key}"} if self.api_key else {})},
            method="POST",
        )
        try:
            with self._opener.open(http_req, timeout=self.timeout_seconds) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as exc:
            raise DependencyError(f"gateway_error: {type(exc).__name__}: {exc}") from exc


def load_provider(config: dict) -> GatewayProvider:
    """从 MetaMemoryConfig.inference 段装配唯一 Provider（组合根用）。"""
    return GatewayProvider(base_url=config.get("base_url", ""), api_key=config.get("api_key", ""),
                           model=config.get("model", ""),
                           embedding_model=config.get("embedding_model", ""),
                           rerank_model=config.get("rerank_model", ""),
                           timeout_seconds=int(config.get("timeout_seconds", 60)))
