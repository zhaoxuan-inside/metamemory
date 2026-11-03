"""巩固管道（consolidate 流程，互补学习系统 CLS 的慢速归纳侧）。

- strength_refresh：强度衰减重算 f(重复,检索,情感)×e^(-λ·Δt)（Δt 锚点=last_recall_at，检索命中已重置）；
- compress_fold：折叠/留原文积压盘点（grep 捞回通道的存储完备率信号，R3 双指标）；
- pattern_induce：S4 模式原子归纳——巩固收益门（R1）：复用频次 ≥ 阈值才固化；
- graph_cleanup：孤悬边清理（硬遗忘/血缘重算后的图完整性）。
定时驱动（full 档 schedules 表 cron；lite 由 API/控制台手动触发）。
"""
from __future__ import annotations

import math
import time

from metamemory_framework.container.signature import unified_method

from ..domain.models import AssociationEdge, MemoryEntry

PATTERN_MIN_SOURCES = 3      # 巩固收益门·复用频次下限（R1 提案值，演化闭环）
STRENGTH_DECAY_PER_DAY = 0.01  # λ 提案值（演化参数，约束 13）


def _find_or_create_pattern(store, subject: str, predicate: str) -> MemoryEntry | None:
    for entry in store.entries.values():
        if entry.entry_type == "pattern" and entry.subject == subject and entry.predicate == predicate:
            return entry
    return None


@unified_method("consolidate.strength_refresh", 1, "data")
def strength_refresh(input: dict, output: dict, context, tenant, metrics, evolution) -> bool:
    """强度衰减：按日指数衰减，检索命中（last_recall_at）即再巩固锚点。"""
    store = context.deps["store"]
    now = time.time()
    refreshed = 0
    for entry in store.entries.values():
        anchor = entry.last_recall_at or entry.learned_at
        days = max(0.0, (now - anchor) / 86400.0)
        new_strength = round(math.exp(-STRENGTH_DECAY_PER_DAY * days), 4)
        if entry.gray_forgotten:
            new_strength = 0.0  # 灰色遗忘恒零（可恢复）
        if abs(new_strength - entry.strength) > 1e-4:
            entry.strength = new_strength
            persist = getattr(store, "set_entry_strength", None)
            if persist is not None:
                persist(entry)
            refreshed += 1
    output["refreshed"] = refreshed
    metrics.counter("consolidate.strength_refreshed", refreshed)
    return True


@unified_method("consolidate.compress_fold", 1, "data")
def compress_fold(input: dict, output: dict, context, tenant, metrics, evolution) -> bool:
    """折叠积压盘点：fold/keep_raw 路由的内容只在原文层——grep 捞回通道的完备率信号。"""
    store = context.deps["store"]
    routed_texts = {route.get("text") for task in
                    (context.deps.get("engine").tasks.values() if context.deps.get("engine") else [])
                    for node_out in task.get("outputs", {}).values() if isinstance(node_out, dict)
                    for route in node_out.get("routes", []) if route.get("route") in ("fold", "keep_raw")}
    stored_raw = {segment.text for segment in store.segments.values()}
    folded_covered = len(routed_texts & stored_raw)
    output.update({"fold_routed": len(routed_texts), "covered_in_raw": folded_covered,
                   "storage_completeness": round(folded_covered / len(routed_texts), 4)
                   if routed_texts else 1.0})
    metrics.gauge("consolidate.fold_completeness", output["storage_completeness"])
    return True


@unified_method("consolidate.pattern_induce", 1, "orchestration")
def pattern_induce(input: dict, output: dict, context, tenant, metrics, evolution) -> bool:
    """S4 模式原子归纳（巩固收益门 R1：复用频次≥3 才固化；血缘=derived 边回连源原子）。"""
    store = context.deps["store"]
    groups: dict[tuple[str, str], list[MemoryEntry]] = {}
    for entry in store.entries.values():
        if entry.entry_type in ("pattern", "intent") or entry.gray_forgotten:
            continue
        if entry.valid_to:
            continue  # 已被 supersedes 关闭的旧原子不参与归纳
        groups.setdefault((entry.subject, entry.predicate), []).append(entry)
    induced, refreshed = 0, 0
    for (subject, predicate), sources in groups.items():
        if len(sources) < PATTERN_MIN_SOURCES:
            continue
        pattern = _find_or_create_pattern(store, subject, predicate)
        confidence = round(sum(s.confidence for s in sources) / len(sources), 3)
        object_value = f"{subject}反复{predicate}（{len(sources)}次）：跨源一致的模式陈述"
        if pattern is None:
            pattern = MemoryEntry(
                entry_id=store.next_id("mem"), tenant_id=tenant.tenant_id,
                domain_id=tenant.domain_id, subject=subject, predicate=predicate,
                object_value=object_value, entry_type="pattern",
                valid_from=time.strftime("%Y-%m-%dT%H:%M:%S%z"), anchor_episode="",
                anchor_span=(0, 0), source_level="S4", confidence=confidence,
                visibility="L1",  # 派生模式默认私有（抽象过严格门，R9）
            )
            store.add_entry(pattern)
            induced += 1
        else:
            pattern.object_value = object_value
            pattern.confidence = confidence
            store.add_entry(pattern)
            refreshed += 1
        existing_sources = {edge.dst_id for edge in store.edges
                            if edge.src_id == pattern.entry_id and edge.edge_type == "derived"}
        for source in sources:  # 血缘回连（纠错沿 derived 下游重算的依据）
            if source.entry_id in existing_sources:
                continue
            store.add_edge(AssociationEdge(
                edge_id=store.next_id("edge"), edge_type="derived",
                src_id=pattern.entry_id, dst_id=source.entry_id,
                evidence={"rule": "pattern_induce", "gate": "reuse_frequency",
                          "frequency": len(sources)}))
    output.update({"induced": induced, "refreshed": refreshed,
                   "gate": {"min_sources": PATTERN_MIN_SOURCES}})
    metrics.counter("consolidate.pattern_induced", induced)
    return True


@unified_method("consolidate.graph_cleanup", 1, "data")
def graph_cleanup(input: dict, output: dict, context, tenant, metrics, evolution) -> bool:
    """图完整性：孤悬边清理（端点已被硬遗忘的边；无依据不建边的反面——依据已亡则边不存）。"""
    store = context.deps["store"]
    orphan_ids = [edge.edge_id for edge in store.edges
                  if edge.src_id not in store.entries or edge.dst_id not in store.entries]
    if orphan_ids and hasattr(store, "db"):
        store.db.execute(
            f"DELETE FROM association_edges WHERE edge_id IN ({','.join('?' for _ in orphan_ids)})",
            orphan_ids)
    store.edges = [edge for edge in store.edges if edge.edge_id not in set(orphan_ids)]
    output["removed_orphan_edges"] = len(orphan_ids)
    metrics.counter("consolidate.orphan_edges_removed", len(orphan_ids))
    return True
