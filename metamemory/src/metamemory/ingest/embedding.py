"""语义向量落库（M2 理解闭环·写侧）：网关 embeddings → 向量槽位 → 语义召回路。

未配置 inference.embedding_model = 语义路关闭（显式配置选择，非降级——语义缺席时
其余四路照常工作，启动/受理侧均有显式计数）。向量真源 = 向量槽位（SQLite BLOB，
库重启不丢），条目冗余存一份（store.set_entry_embedding，full 档随条目行走 pgvector）。
"""
from __future__ import annotations

from metamemory_framework.container.signature import unified_method


@unified_method("ingest.embed_entries", 1, "data")
def embed_entries(input: dict, output: dict, context, tenant, metrics, evolution) -> bool:
    """对本次提取路由入库的原子生成语义向量（批量一次调用；幂等经 llm_calls 缓存）。"""
    store = context.deps["store"]
    embedder = context.deps.get("inference")
    vectors = context.deps.get("vector_store")
    if embedder is None or vectors is None or not getattr(embedder, "embedding_model", ""):
        metrics.counter("recall.semantic_unavailable")  # 显式计数：语义路未启用
        output.update({"embedded": 0, "reason": "embedding_model_unconfigured"})
        return True
    entry_ids = [eid for eid in (input.get("entry_ids") or []) if eid in store.entries]
    if not entry_ids:
        output.update({"embedded": 0})
        return True
    texts = [store.entries[eid].object_value for eid in entry_ids]
    cache = context.deps.get("call_cache")
    vecs: list[list[float]] | None = None
    if cache is not None:
        input_hash = cache.input_hash(texts)
        prompt_version = (context.config.get("inference") or {}).get("prompt_version", "v1")
        cached = cache.lookup("ingest.embed_entries", input_hash, prompt_version)
        if cached is not None and cached.get("vectors"):
            vecs = cached["vectors"]
            metrics.counter("llm_cache_hit")
    if vecs is None:
        vecs = embedder.embed(texts)  # 未配置/网络故障 = DependencyError（显式失败，无降级）
        if cache is not None:
            cache.store("ingest.embed_entries", cache.input_hash(texts), prompt_version,
                        {"vectors": vecs})
        ledger = context.deps.get("metering")
        if ledger is not None:
            ledger.record(tenant.tenant_id, "llm.tokens", len(texts) * 32,
                          source="ingest.embed_entries")  # 嵌入 token 量按条目近似计量
    for entry_id, vec in zip(entry_ids, vecs):
        entry = store.entries[entry_id]
        entry.embedding = vec
        entry.embedding_model = embedder.embedding_model
        persist = getattr(store, "set_entry_embedding", None)
        if persist is not None:
            persist(entry)
        vectors.upsert("entries", [(entry_id, vec)])
    output.update({"embedded": len(entry_ids)})
    metrics.counter("embed.entries", len(entry_ids))
    return True
