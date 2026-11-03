"""精排（M2 理解闭环·最佳路径组成件，C-G 裁决落地）。

rerank_model 配置即启用（网关 LLM 点式打分，llm_calls 缓存幂等）；未配置 = RRF 直出
——这是显式配置选择而非质量降级（ADR-006：响应不再携带 degraded 语义，精排是可
配置的最佳路径组件，缺席时系统按融合序诚实输出）。
"""
from __future__ import annotations

import json

from metamemory_framework.container.policy import ContractViolation
from metamemory_framework.container.signature import unified_method

RERANK_NODE = "query.rerank"
RERANK_TOP_N = 20  # 精排窗口（融合序前 N；全量精排是 S 后续容量议题）


@unified_method("query.rerank", 1, "orchestration")
def rerank(input: dict, output: dict, context, tenant, metrics, evolution) -> bool:
    """融合序 → 精排重排：rerank_model 未配置 = RRF 直出（reranked=false 显式标记）。"""
    fused: list[dict] = input.get("fused") or []
    provider = context.deps.get("inference") if context is not None else None
    if provider is None or not getattr(provider, "rerank_model", "") or not fused:
        output["fused"] = fused
        output["reranked"] = False
        return True
    store = context.deps["store"]
    window = fused[:RERANK_TOP_N]
    contents = {item["memory_id"]: getattr(store.entries.get(item["memory_id"]), "object_value", "")
                for item in window}
    prompt = _build_rerank_prompt(input["query"], contents)
    scores = _score_cached(context, tenant, metrics, provider, prompt)
    if scores is None:
        raise ContractViolation("rerank_parse_failed: 精排输出非约定 JSON（转复核，禁静默直出）")
    def sort_key(item: dict) -> tuple:
        score = scores.get(item["memory_id"], 0.0)
        return (score, item["fusion_score"])
    ordered = sorted(window, key=sort_key, reverse=True) + fused[RERANK_TOP_N:]
    output["fused"] = ordered
    output["reranked"] = True
    metrics.counter("rerank.applied")
    return True


def _score_cached(context, tenant, metrics, provider, prompt: str) -> dict[str, float] | None:
    """精排打分（缓存幂等）：返回 {memory_id: 0-10}。"""
    cache = context.deps.get("call_cache")
    input_hash = None
    prompt_version = "v1"
    if cache is not None:
        input_hash = cache.input_hash(prompt)
        prompt_version = (context.config.get("inference") or {}).get("prompt_version", "v1")
        cached = cache.lookup(RERANK_NODE, input_hash, prompt_version)
        if cached is not None:
            metrics.counter("llm_cache_hit")
            return cached.get("scores")
    response = provider.complete({"task": "rerank", "prompt": prompt, "prefer": "gateway"})
    ledger = context.deps.get("metering")
    if ledger is not None and response.get("tokens"):
        ledger.record(getattr(tenant, "tenant_id", ""), "llm.tokens", response["tokens"],
                      source=RERANK_NODE)
    try:
        parsed = json.loads(response.get("text") or "null")
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, list):
        return None
    scores = {}
    for row in parsed:
        if isinstance(row, dict) and "id" in row:
            try:
                scores[str(row["id"])] = max(0.0, min(10.0, float(row.get("score", 0))))
            except (TypeError, ValueError):
                continue
    if cache is not None and input_hash is not None:
        cache.store(RERANK_NODE, input_hash, prompt_version, {"scores": scores})
    return scores


def _build_rerank_prompt(query: str, contents: dict[str, str]) -> str:
    """精排 prompt（v1）：按相关性 0-10 打分，只输出 JSON 数组。"""
    lines = [f'{doc_id}: {text}' for doc_id, text in contents.items() if text]
    return (
        "你是检索精排器。对每个候选按与【查询】的相关性打 0-10 分（整数）。\n"
        '只输出 JSON 数组：[{"id": "候选id", "score": 数字}, ...]，勿输出其他内容。\n'
        f"【查询】{query}\n【候选】\n" + "\n".join(lines)
    )
