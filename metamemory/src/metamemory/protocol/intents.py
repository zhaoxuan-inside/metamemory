"""意图状态机（S2 信任完整）：唯一"有时效、可交易、需确认"的记忆——核心层只有字段与状态机位。

双入口（意图三分之一，R5）：declared 声明直生效 / agent_candidate Agent 只产候选（默认
仅自己可见，经确认卡片人裁后才可能被发现）。三旋钮=有效期/可见性滑杆/期望回应，每档
可见性是一次视野变更。发现位配额（G3）：配额限"生效意图进入发现网络的位数"（免费 3），
意图本体永远免费不限量。到期自动转 expired → 退出发现索引（血缘级联=不参与 network 面）。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from metamemory_framework.container.signature import unified_method

from ..domain.models import IntentRecord, MemoryEntry

_NETWORK_LEVELS = ("L3", "L3D", "L4", "L5")  # 进入发现网络的可见性档位
_ISO_FMT = "%Y-%m-%dT%H:%M:%S%z"


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _parse_iso(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return None


def _active_network_slots(store) -> int:
    return sum(1 for intent in store.intents.values()
               if intent.state == "active" and intent.visibility in _NETWORK_LEVELS)


def _discovery_quota(context) -> int | None:
    quota = (context.config.get("quota") or {})
    plan = (context.config.get("profile") or "lite")
    pro_slots = quota.get("discovery_slots_pro")
    if pro_slots is None:
        return int(quota.get("discovery_slots_free", 3))
    return int(pro_slots)


def _audit(context, tenant, action: str, object_ref: str, detail: dict) -> None:
    audit = context.deps.get("audit")
    if audit is not None:
        audit.append(tenant.actor if tenant is not None else "system", action, object_ref,
                     tenant_id=getattr(tenant, "tenant_id", None), detail=detail)


def _meter(context, tenant, dimension: str, quantity: float) -> None:
    ledger = context.deps.get("metering")
    if ledger is not None and tenant is not None:
        ledger.record(tenant.tenant_id, dimension, quantity)


@unified_method("intent.declare", 1, "orchestration")
def intent_declare(input: dict, output: dict, context, tenant, metrics, evolution) -> bool:
    """API-013：意图声明——declared 直生效 / agent 只产候选；发现位配额 G3。"""
    store = context.deps["store"]
    content = (input.get("content") or "").strip()
    if not content:
        evolution.fail("VALID_FAILED", "intent content 不能为空")
        return False
    origin = input.get("origin", "declared")
    if origin not in ("declared", "agent_candidate"):
        evolution.fail("VALID_FAILED", "origin 仅支持 declared|agent_candidate")
        return False
    visibility = input.get("visibility", "L1")
    state = "active" if origin == "declared" else "candidate"
    if state == "active" and visibility in _NETWORK_LEVELS:  # 进入发现网络 → 占用发现位
        quota = _discovery_quota(context)
        if _active_network_slots(store) >= quota:
            evolution.fail("QUOTA_EXCEEDED",
                           f"发现位配额封顶（{quota}）：意图本体已保存为候选，可降档或清理生效意图",
                           retryable=True)
            metrics.counter("intent.quota_blocked")
            return False
    entry = MemoryEntry(
        entry_id=store.next_id("mem"), tenant_id=tenant.tenant_id, domain_id=tenant.domain_id,
        subject=tenant.actor, predicate="intends", object_value=content,
        entry_type="intent", valid_from=_now_iso(),  # 意图锚点=声明本身（无原文情境，schema 唯一例外）
        anchor_episode="", anchor_span=(0, 0), source_level="S1", confidence=1.0,
        visibility=visibility,
    )
    store.add_entry(entry)
    ttl_expires_at = input.get("ttl_expires_at") or (
        datetime.now(timezone.utc).astimezone()
        + timedelta(days=int(input.get("ttl_days", 30)))).isoformat(timespec="seconds")
    intent = IntentRecord(
        entry_id=entry.entry_id, state=state, ttl_expires_at=ttl_expires_at,
        visibility=visibility, expected_response=input.get("expected_response", "solution"),
        origin=origin,
    )
    store.save_intent(intent)
    _audit(context, tenant, "intent.declare", entry.entry_id,
           {"origin": origin, "state": state, "visibility": visibility})
    if state == "active" and visibility in _NETWORK_LEVELS:
        _meter(context, tenant, "intent.active_slots", 1)
    metrics.counter(f"intent.declare.{origin}")
    output.update({"intent_id": entry.entry_id, "state": state,
                   "ttl_expires_at": ttl_expires_at, "visibility": visibility})
    return True


@unified_method("intent.confirm", 1, "data")
def intent_confirm(input: dict, output: dict, context, tenant, metrics, evolution) -> bool:
    """API-014：确认卡片人裁——Agent 候选转生效（唯一确认链）；或拒绝（转 completed）。"""
    store = context.deps["store"]
    intent = store.get_intent(input.get("intent_id", ""))
    if intent is None:
        evolution.fail("NOT_FOUND", "意图不存在")
        return False
    if intent.state != "candidate":
        evolution.fail("VALID_FAILED", f"意图状态 {intent.state} 不可确认（仅候选可人裁）")
        return False
    decision = input.get("decision", "confirm")
    if decision == "reject":
        intent.state = "completed"
        intent.updated_at = datetime.now(timezone.utc).timestamp()
        store.save_intent(intent)
        _audit(context, tenant, "intent.confirm.reject", intent.entry_id, {})
        output.update({"intent_id": intent.entry_id, "state": "completed"})
        return True
    if intent.visibility in _NETWORK_LEVELS:  # 确认后进入发现网络 → 占位检查
        quota = _discovery_quota(context)
        if _active_network_slots(store) >= quota:
            evolution.fail("QUOTA_EXCEEDED", f"发现位配额封顶（{quota}）", retryable=True)
            return False
    intent.state = "active"
    intent.updated_at = datetime.now(timezone.utc).timestamp()
    store.save_intent(intent)
    _audit(context, tenant, "intent.confirm.accept", intent.entry_id,
           {"visibility": intent.visibility})
    if intent.visibility in _NETWORK_LEVELS:
        _meter(context, tenant, "intent.active_slots", 1)
    metrics.counter("intent.confirmed")
    output.update({"intent_id": intent.entry_id, "state": "active"})
    return True


@unified_method("intent.update", 1, "data")
def intent_update(input: dict, output: dict, context, tenant, metrics, evolution) -> bool:
    """三旋钮调整：有效期/可见性滑杆/期望回应（每档可见性是一次视野变更）。"""
    store = context.deps["store"]
    intent = store.get_intent(input.get("intent_id", ""))
    if intent is None:
        evolution.fail("NOT_FOUND", "意图不存在")
        return False
    old_visibility = intent.visibility
    if "ttl_expires_at" in input:
        intent.ttl_expires_at = input["ttl_expires_at"]
    if "expected_response" in input:
        intent.expected_response = input["expected_response"]
    if "visibility" in input:
        target = input["visibility"]
        if target not in ("L1", "L2", "L3", "L3D", "L4", "L5"):
            evolution.fail("VALID_FAILED", f"非法视野档位: {target}")
            return False
        if target in _NETWORK_LEVELS and old_visibility not in _NETWORK_LEVELS \
                and intent.state == "active":  # 升入网络 → 占位检查
            quota = _discovery_quota(context)
            if _active_network_slots(store) >= quota:
                evolution.fail("QUOTA_EXCEEDED", f"发现位配额封顶（{quota}）", retryable=True)
                return False
        intent.visibility = target
        store_intact = store.entries.get(intent.entry_id)
        if store_intact is not None:
            store_intact.visibility = target
            persist_vis = getattr(store, "set_entry_visibility", None)
            if persist_vis is not None:
                persist_vis(store_intact)
    intent.updated_at = datetime.now(timezone.utc).timestamp()
    store.save_intent(intent)
    _audit(context, tenant, "intent.update", intent.entry_id,
           {"visibility": f"{old_visibility}->{intent.visibility}"})
    output.update({"intent_id": intent.entry_id, "state": intent.state,
                   "visibility": intent.visibility, "ttl_expires_at": intent.ttl_expires_at})
    return True


@unified_method("intent.sweep_expiry", 1, "data")
def intent_sweep_expiry(input: dict, output: dict, context, tenant, metrics, evolution) -> bool:
    """到期扫描：active + ttl 过期 → expired，退出发现索引（定时任务驱动；lite 随检索惰性触发）。"""
    store = context.deps["store"]
    now = datetime.now(timezone.utc).timestamp()
    expired: list[str] = []
    for intent in store.intents.values():
        deadline = _parse_iso(intent.ttl_expires_at)
        if intent.state == "active" and deadline is not None and deadline < now:
            intent.state = "expired"
            intent.updated_at = now
            store.save_intent(intent)
            expired.append(intent.entry_id)
    if expired:
        _audit(context, tenant, "intent.expire", ",".join(expired), {"count": len(expired)})
    metrics.counter("intent.expired", len(expired))
    output.update({"expired": expired})
    return True
