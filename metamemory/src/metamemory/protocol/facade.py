"""Memory API 门面（I-027/I-028）：add/search 两动词——业务层唯一 import 面（ADR 16）。

add：计量→幂等→快通道→同事务入队（202 受理语义）；
search：五路召回→RRF 融合→VisionArbiter 裁决→预算组装（key 即查询者）。
"""
from __future__ import annotations

from metamemory_framework.container.signature import unified_method

from ..ingest.extract import route_score  # noqa: F401  （注册管道步骤）
from ..ingest.fastpath import fastpath_store  # noqa: F401
from ..ingest.organize import organize_links  # noqa: F401
from ..ingest.steps import (  # noqa: F401
    dedupe_route,
    normalize_event,
    pre_gate,
    segment_units,
    select_worthiness,
)
from ..query.express import budget_pack
from ..query.fusion import rrf_fuse
from ..query.recall import query_recall
from .arbiter import arbiter_decide
from .authorization import AuthzRegistry


@unified_method("protocol.add", 1, "orchestration")
def protocol_add(input: dict, output: dict, context, tenant, metrics, evolution) -> bool:
    """I-027：接收信息——受理即落袋（快通道）+ ingest 流程任务式受理。"""
    quota_cfg = context.config.get("quota") or {}
    quota = int(quota_cfg.get("max_pending_events", context.config.get("max_pending_events", 10_000)))
    if metrics.counters.get("add.accepted", 0) >= quota:
        evolution.fail("QUOTA_EXCEEDED", "写入配额封顶（配额即 COGS 上界）", retryable=True)
        metrics.counter("add.quota_blocked")
        return False
    fastpath: dict = {}
    if not fastpath_store({"event": input["event"]}, fastpath, context, tenant, metrics, evolution):
        evolution.push_cause("fastpath_store", evolution)
        return False
    engine = context.deps["engine"]
    pipeline = context.deps["flows"]["ingest"]
    task_id = engine.submit(pipeline, params={
        "episode_id": fastpath["episode_id"],
        "normalized": {k: input["event"][k] for k in ("source_uri", "content", "actor", "occurred_at")},
        "event": input["event"],
    })
    metrics.counter("add.accepted")
    output.update({"task_id": task_id, "sse_url": f"/v1/tasks/{task_id}/events",
                   "fastpath": {"stored": True, "searchable_in": "<100ms"},
                   "idempotency": {"replayed": bool(fastpath.get("replayed"))}})
    return True


@unified_method("protocol.search", 1, "orchestration")
def protocol_search(input: dict, output: dict, context, tenant, metrics, evolution) -> bool:
    """I-028：查询信息——召回→融合→裁决→组装；结果集由视野铁律决定（ADR 15）。"""
    recall_out: dict = {}
    if not query_recall({"query": input["query"], "episode_ref": input.get("episode_ref")},
                        recall_out, context, tenant, metrics, evolution):
        evolution.push_cause("query_recall", evolution)
        return False
    fused: dict = {}
    ranked_lists = recall_out.get("paths") or _to_ranked(recall_out["candidates"])
    rrf_fuse({"ranked_lists": ranked_lists}, fused, context, tenant, metrics, evolution)
    order = [item["memory_id"] for item in fused.get("fused", [])]
    by_id = {c["memory_id"]: c for c in recall_out["candidates"]}
    ranked = [_decorate(by_id[i], fused) for i in order if i in by_id]
    arb_out: dict = {}
    arbiter_decide({"candidates": ranked, "querier": _querier(tenant),
                    "authz": _authz(context), "declared_purpose": input.get("purpose")},
                   arb_out, context, tenant, metrics, evolution)
    top_k = max(1, min(int(input.get("top_k", 10)), 50))
    packed: dict = {}
    budget_pack({"items": arb_out.get("allowed", [])[:top_k],
                 "token_budget": input.get("token_budget", 2048)},
                packed, context, tenant, metrics, evolution)
    _touch_recalled(context, packed.get("packed") or [])  # 检索命中重置再巩固窗口锚点（数据流不变式⑤）
    output["items"] = packed.get("packed", [])
    output["denials"] = arb_out.get("denials", [])
    output["unanswered"] = {"reason": "NO_MATCH"} if not packed.get("packed") else None
    return True


def _touch_recalled(context, packed: list) -> None:
    """检索命中写回：last_recall_at=now（再巩固窗口 72h 起算），经 store 写穿。"""
    import time

    store = context.deps.get("store")
    touch = getattr(store, "touch_entry", None)
    if touch is None:
        return
    for item in packed:
        entry = store.entries.get(item.get("memory_id", ""))
        if entry is not None:
            entry.last_recall_at = time.time()
            touch(entry)


def _to_ranked(candidates: list) -> dict:
    ranked: dict[str, list] = {}
    for candidate in candidates:
        for path in candidate.get("paths", ["fulltext"]):
            ranked.setdefault(path, []).append(candidate["memory_id"])
    return ranked


def _decorate(candidate: dict, fused: dict) -> dict:
    scores = {f["memory_id"]: f["fusion_score"] for f in fused.get("fused", [])}
    return {**candidate, "fusion_score": scores.get(candidate["memory_id"], 0.0)}


def _querier(tenant) -> dict:
    return {"tenant_id": tenant.tenant_id, "domain_id": tenant.domain_id, "actor": tenant.actor}


def _authz(context) -> AuthzRegistry:
    return context.deps["authz"]
