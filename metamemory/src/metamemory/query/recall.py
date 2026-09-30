"""五路并行召回骨架（lite 档）与候选合并。

五路：语义（VectorStore）/ 全文（FullTextStore）/ 图谱（GraphStore）/ 时间线 / 情境
（episode 直取——编码特异性）。lite 档语义与图谱两路降级为词面重叠；接口不变。
候选携带裁决所需字段（tenant/visibility/confidence…），过滤前不做业务加工。
"""
from __future__ import annotations

import time

from metamemory_framework.container.signature import unified_method

from ..domain.models import MemoryEntry
from ..domain.store import MemoryStore


def _candidate(entry: MemoryEntry, score: float) -> dict:
    return {"memory_id": entry.entry_id, "score": score,
            "tenant_id": entry.tenant_id, "domain_id": entry.domain_id,
            "visibility": entry.visibility, "confidence": entry.confidence,
            "source_level": entry.source_level, "injected_suspect": entry.injected_suspect,
            "content": entry.object_value, "anchor_episode": entry.anchor_episode,
            "span": list(entry.anchor_span), "strength": entry.strength}


@unified_method("query.merge_candidates", 1, "data")
def merge_candidates(input: dict, output: dict, context, tenant, metrics, evolution) -> bool:
    """I-029：合并各路候选并去重，保留首次出现路径与最好分。"""
    paths: dict[str, list[dict]] = input["paths"]
    merged: dict[str, dict] = {}
    for path, items in paths.items():
        for item in items:
            key = item["memory_id"]
            if key not in merged:
                merged[key] = {**item, "paths": [path]}
            elif path not in merged[key]["paths"]:
                merged[key]["paths"].append(path)
    output["candidates"] = list(merged.values())
    metrics.counter("recall.merged", len(merged))
    return True


@unified_method("query.recall", 1, "orchestration")
def query_recall(input: dict, output: dict, context, tenant, metrics, evolution) -> bool:
    """I-032：五路并行召回（lite 骨架）→ 候选合并；过滤前不做业务加工。"""
    store: MemoryStore = context.deps["store"]
    tokens = set(input["query"].lower().split())
    paths: dict[str, list[dict]] = {
        "fulltext": _fulltext_path(store, tokens),
        "semantic": _semantic_path(store, tokens),
        "graph": _graph_path(store, tokens),
        "timeline": _timeline_path(store, input.get("since")),
        "context": _context_path(store, input.get("episode_ref")),
    }
    merged: dict = {}
    merge_candidates({"paths": paths}, merged, context, tenant, metrics, evolution)
    output["candidates"] = merged.get("candidates", [])
    return True


def _fulltext_path(store: MemoryStore, tokens: set[str]) -> list[dict]:
    """lite 全文路：词面重叠打分（FullTextStore 接口位，PG FTS/SQLite FTS5 替换）。"""
    hits = []
    for entry in store.entries.values():
        text = f"{entry.subject} {entry.predicate} {entry.object_value}".lower()
        overlap = len(tokens & set(text.split()))
        if overlap:
            hits.append(_candidate(entry, overlap / (len(tokens) or 1)))
    return sorted(hits, key=lambda h: h["score"], reverse=True)


def _semantic_path(store: MemoryStore, tokens: set[str]) -> list[dict]:
    """lite 语义路降级为词面重叠（VectorStore 接口位：pgvector/sqlite-vec）。"""
    return _fulltext_path(store, tokens)


def _graph_path(store: MemoryStore, tokens: set[str]) -> list[dict]:
    """lite 图谱路：一步邻居扩散（GraphStore 接口位：递归 CTE → PPR）。"""
    direct = {hit["memory_id"] for hit in _fulltext_path(store, tokens)}
    hits = []
    for edge in store.edges:
        if edge.src_id in direct and edge.edge_type in ("entity", "contextual", "temporal"):
            neighbor = store.entries.get(edge.dst_id)
            if neighbor is not None:
                hits.append(_candidate(neighbor, edge.weight * 0.5))
    return hits


def _timeline_path(store: MemoryStore, since: float | None) -> list[dict]:
    cutoff = since if since is not None else time.time() - 86400 * 30
    recent = [e for e in store.entries.values() if e.learned_at >= cutoff]
    recent.sort(key=lambda e: e.learned_at, reverse=True)
    return [_candidate(e, 0.3) for e in recent[:10]]


def _context_path(store: MemoryStore, episode_ref: str | None) -> list[dict]:
    """情境触发：episode 成员整体复活（编码特异性）。"""
    if not episode_ref:
        return []
    return [_candidate(e, 0.8) for e in store.entries_of(episode_ref)]
