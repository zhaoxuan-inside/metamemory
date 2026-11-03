"""组织（I-022）：双时态落库 + 确定性建边（时序/情境/替代）——无依据不建边。"""
from __future__ import annotations

import math

from metamemory_framework.container.signature import unified_method

from ..domain.models import AssociationEdge, MemoryEntry
from ..domain.store import MemoryStore

RECONSOLIDATION_WINDOW_SECONDS = 72 * 3600  # 再巩固窗口（约束 11，默认 72h）


@unified_method("ingest.organize_links", 1, "orchestration")
def organize_links(input: dict, output: dict, context, tenant, metrics, evolution) -> bool:
    """I-022：提取路由原子落库；同实体同谓词走 supersedes（窗口内）；情境/时序建边。"""
    store: MemoryStore = context.deps["store"]
    occurred_at = (input.get("normalized") or {}).get("occurred_at", "")
    entry_ids: list[str] = []
    for route in input.get("routes") or []:
        if route.get("route") != "extract":
            continue
        entry = _to_entry(store, route, input, tenant, occurred_at)
        existing = store.find_entries(subject=entry.subject, predicate=entry.predicate)
        active = [e for e in existing if e.entry_id != entry.entry_id and _supersede_allowed(e)]
        if active:
            store.supersede(_newest(active), entry)
        else:
            store.add_entry(entry)
        entry_ids.append(entry.entry_id)
        fts = context.deps.get("fts") if context is not None else None
        if fts is not None:  # 全文槽位索引（bm25 召回面；硬遗忘时级联移除）
            fts.index("entries", entry.entry_id, entry.object_value)
    _link_episode(store, input.get("episode_id", ""))
    output["entry_ids"] = entry_ids
    metrics.counter("organize.entries", len(entry_ids))
    return True


def _to_entry(store: MemoryStore, route: dict, input: dict, tenant, occurred_at: str) -> MemoryEntry:
    suspect = bool(route.get("suspect"))
    confidence = min(route.get("confidence", 0.5), 0.3 if suspect else 1.0)
    return MemoryEntry(
        entry_id=store.next_id("mem"), tenant_id=tenant.tenant_id,
        domain_id=tenant.domain_id, subject=route.get("subject", "user"),
        predicate=route.get("predicate", "mentioned"), object_value=route.get("text", ""),
        entry_type=route.get("entry_type", "fact"), valid_from=occurred_at,
        anchor_episode=input.get("episode_id", ""), anchor_span=tuple(route.get("span") or (0, 0)),
        source_level="S3" if suspect else "S2", confidence=confidence,
        visibility="L1" if suspect else "L2", injected_suspect=suspect,
    )


def _supersede_allowed(old: MemoryEntry) -> bool:
    """再巩固窗口：supersedes 重写仅在窗口内行使；窗口外并存（走派生门禁）。"""
    if old.last_recall_at is None:
        return True  # 从未被唤起的旧原子：写侧直接关闭（时间演进型）
    import time

    return time.time() - old.last_recall_at <= RECONSOLIDATION_WINDOW_SECONDS


def _newest(entries: list[MemoryEntry]) -> MemoryEntry:
    return max(entries, key=lambda e: e.learned_at)


def _link_episode(store: MemoryStore, episode_id: str) -> None:
    """情境边（同 episode 成员互连）+ 时序边（权重 exp(-Δt/σ)，确定性）。"""
    entries = store.entries_of(episode_id)
    for index, entry in enumerate(entries):
        for peer in entries[index + 1:]:
            store.add_edge(AssociationEdge(
                edge_id=store.next_id("edge"), edge_type="contextual",
                src_id=entry.entry_id, dst_id=peer.entry_id, evidence={"episode": episode_id},
            ))
            gap = abs(entry.learned_at - peer.learned_at)
            store.add_edge(AssociationEdge(
                edge_id=store.next_id("edge"), edge_type="temporal",
                src_id=entry.entry_id, dst_id=peer.entry_id,
                weight=round(math.exp(-gap / 3600.0), 4), evidence={"sigma": "3600s"},
            ))
