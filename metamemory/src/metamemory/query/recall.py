"""五路并行召回骨架（lite 档）与候选合并。

五路：语义（VectorStore）/ 全文（FullTextStore）/ 图谱（GraphStore）/ 时间线 / 情境
（episode 直取——编码特异性）。lite 档语义与图谱两路降级为词面重叠；接口不变。
候选携带裁决所需字段（tenant/visibility/confidence…），过滤前不做业务加工。
"""
from __future__ import annotations

import re
import time

from metamemory_framework.container.signature import unified_method

from ..domain.models import MemoryEntry
from ..domain.store import MemoryStore


CJK_RUN = re.compile(r"([\u4e00-\u9fa5]+)|([a-z0-9_]+)")


def _tokenize(text: str) -> set[str]:
    """lite 切词：ASCII 词元 + CJK 字符 bigram（zhparser/FTS5 的降级等价物，约束 13 演化位）。"""
    tokens: set[str] = set()
    for word in text.lower().split():
        for cjk, ascii_ in CJK_RUN.findall(word):
            if cjk:
                if len(cjk) == 1:
                    tokens.add(cjk)
                tokens.update(a + b for a, b in zip(cjk, cjk[1:]))
            else:
                tokens.add(ascii_)
    return tokens


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
    tokens = _tokenize(input["query"].lower())
    paths: dict[str, list[dict]] = {
        "fulltext": _fulltext_path(store, tokens),
        "semantic": _semantic_path(store, tokens),
        "graph": _graph_path(store, tokens),
        "timeline": _timeline_path(store, tokens, input.get("since")),
        "context": _context_path(store, input.get("episode_ref")),
    }
    merged: dict = {}
    merge_candidates({"paths": paths}, merged, context, tenant, metrics, evolution)
    output["candidates"] = merged.get("candidates", [])
    output["paths"] = {name: [item["memory_id"] for item in items]  # 各路真排序（RRF 输入）
                       for name, items in paths.items()}
    return True


def _fulltext_path(store: MemoryStore, tokens: set[str]) -> list[dict]:
    """lite 全文路：内容词面重叠打分（FullTextStore 接口位）。

    只按 object_value（内容）算重叠——subject/predicate 是身份词不是内容信号，
    计入会让全体条目基线同分、RRF 排序退化（评测黄金集暴露的真实缺陷）。
    """
    hits = []
    for entry in store.entries.values():
        overlap = len(tokens & _tokenize(entry.object_value.lower()))
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


def _timeline_path(store: MemoryStore, tokens: set[str], since: float | None) -> list[dict]:
    """时间线路：近期记忆 + 最小词面相关（拒答语义：无关查询不得凑数返回）。"""
    cutoff = since if since is not None else time.time() - 86400 * 30
    recent = [e for e in store.entries.values() if e.learned_at >= cutoff]
    recent.sort(key=lambda e: e.learned_at, reverse=True)
    hits = []
    for e in recent:
        if tokens & _tokenize(e.object_value.lower()):
            hits.append(_candidate(e, 0.3))
    return hits[:10]


def _context_path(store: MemoryStore, episode_ref: str | None) -> list[dict]:
    """情境触发：episode 成员整体复活（编码特异性）。"""
    if not episode_ref:
        return []
    return [_candidate(e, 0.8) for e in store.entries_of(episode_ref)]
