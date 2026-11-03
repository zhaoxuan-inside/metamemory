"""写侧治理（manage 四子动词，API-008~011，C-F 口径）+ 血缘回溯 recompute 步骤。

- correct：纠错=新原子 supersedes 旧原子（S1 来源、置信 1.0）+ 血缘 BFS 下游 stale → recompute 管道；
- visibility：升档需确认（confirm_token 二次提交）/降档即生效；L4/L5 生成发布投影（生成/发布分离）；
- authz：授权三元组 grant/revoke（撤回幂等，停止未来检索）；
- forget：遗忘双轨 gray（降权可恢复）/ hard（物理删除+血缘级联，唯一真删除）。
一切动作落审计链（US-7 全量不采样）。abstain 语义不适用本面（人裁就是本面）。
"""
from __future__ import annotations

import hashlib
import hmac
import os
import time
from datetime import datetime, timedelta, timezone

from metamemory_framework.container.signature import unified_method

from ..domain.models import AssociationEdge, MemoryEntry, PublishedItem
_LINEAGE_EDGES = ("derived", "semantic", "causal")  # 血缘传播边（错误沿派生/语义/因果下游扩散）

_LEVEL_ORDER = {"L1": 0, "L2": 1, "L3": 2, "L3D": 2, "L4": 3, "L5": 4}


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _audit(context, tenant, action: str, object_ref: str, detail: dict) -> None:
    audit = context.deps.get("audit")
    if audit is not None:
        audit.append(tenant.actor if tenant is not None else "system", action, object_ref,
                     tenant_id=getattr(tenant, "tenant_id", None), detail=detail)


# ---------------------------------------------------------------- correct

@unified_method("manage.correct", 1, "orchestration")
def manage_correct(input: dict, output: dict, context, tenant, metrics, evolution) -> bool:
    """API-008：纠错——supersedes 关闭旧原子 + 血缘下游 stale 重算（任务式返回 task_id）。"""
    store = context.deps["store"]
    old = store.entries.get(input.get("memory_id", ""))
    if old is None:
        evolution.fail("NOT_FOUND", "待纠错记忆不存在")
        return False
    correction = (input.get("correction") or "").strip()
    if not correction:
        evolution.fail("VALID_FAILED", "correction 不能为空")
        return False
    corrected = MemoryEntry(
        entry_id=store.next_id("mem"), tenant_id=old.tenant_id, domain_id=old.domain_id,
        subject=old.subject, predicate=old.predicate, object_value=correction,
        entry_type=old.entry_type, valid_from=input.get("effective_at") or _now_iso(),
        anchor_episode=old.anchor_episode, anchor_span=tuple(input.get("evidence_span") or old.anchor_span),
        source_level="S1", confidence=1.0, visibility=old.visibility,  # 用户明示纠错：最高来源级别
    )
    store.supersede(old, corrected)
    stale_ids = mark_lineage_stale(store, [old.entry_id])
    task_id = _submit_recompute(context, tenant, seed_entry_ids=[old.entry_id])
    _audit(context, tenant, "manage.correct", old.entry_id,
           {"corrected_to": corrected.entry_id, "stale_downstream": stale_ids})
    metrics.counter("manage.correct")
    output.update({"memory_id": corrected.entry_id, "superseded": old.entry_id,
                   "stale_downstream": stale_ids, "task_id": task_id})
    return True


def mark_lineage_stale(store, seeds: list[str]) -> list[str]:
    """血缘回溯：沿派生/语义/因果边 BFS 下游，标记 stale（错误不扩散，等重算）。

    边方向约定：derived 边 src=派生原子、dst=来源原子（血缘=type='derived'）；
    因此对任一节点，"下游"= 以它为 dst 的派生边之 src（反向兼容 src→dst 写法）。
    """
    stale: list[str] = []
    frontier = list(seeds)
    seen = set(seeds)
    while frontier:
        current = frontier.pop()
        for edge in store.edges:
            if edge.edge_type not in _LINEAGE_EDGES:
                continue
            if edge.dst_id == current:
                downstream_id = edge.src_id  # 派生边：下游是派生方
            elif edge.src_id == current:
                downstream_id = edge.dst_id  # 语义/因果边：正向下游
            else:
                continue
            if downstream_id in seen:
                continue
            seen.add(downstream_id)
            downstream = store.entries.get(downstream_id)
            if downstream is not None:
                downstream.stale = True
                persist = getattr(store, "set_entry_stale", None)
                if persist is not None:
                    persist(downstream)
                stale.append(downstream_id)
                frontier.append(downstream_id)
    return stale


def _submit_recompute(context, tenant, seed_entry_ids: list[str]) -> str | None:
    """recompute 流程任务式重算（lite 同步驱动；受理即任务，与 add 同语义）。"""
    engine = context.deps.get("engine")
    flows = context.deps.get("flows") or {}
    pipeline = flows.get("recompute")
    if engine is None or pipeline is None:
        return None
    task_id = engine.submit(pipeline, params={"seed_entry_ids": seed_entry_ids},
                            tenant_id=getattr(tenant, "tenant_id", ""))
    engine.run_flow({"task_id": task_id}, {}, context, tenant, _QuietMetrics(), _QuietEvo())
    return task_id


# ---------------------------------------------------------------- visibility

class ConfirmationSigner:
    """升档确认令牌：HMAC(secret, memory_id|target)——二次提交才生效（升档需确认）。"""

    def __init__(self, db) -> None:
        self.db = db
        rows = db.query("SELECT value FROM kv_counters WHERE name='manage.confirm_secret'")
        if rows:
            self._secret = rows[0]["value"].encode()
        else:
            self._secret = os.urandom(32)
            db.execute("INSERT OR REPLACE INTO kv_counters(name, value) VALUES('manage.confirm_secret',?)",
                       (self._secret.hex(),))

    def sign(self, memory_id: str, target_level: str) -> str:
        return hmac.new(self._secret, f"{memory_id}|{target_level}".encode(),
                        hashlib.sha256).hexdigest()[:32]

    def verify(self, memory_id: str, target_level: str, token: str) -> bool:
        return bool(token) and hmac.compare_digest(self.sign(memory_id, target_level), token)


@unified_method("manage.visibility", 1, "orchestration")
def manage_visibility(input: dict, output: dict, context, tenant, metrics, evolution) -> bool:
    """API-009：视野变更——升档需确认/降档即生效；L4/L5 发布=变更（生成/发布分离）。"""
    store = context.deps["store"]
    entry = store.entries.get(input.get("memory_id", ""))
    if entry is None:
        evolution.fail("NOT_FOUND", "记忆不存在")
        return False
    target = input.get("target_level", "")
    if target not in _LEVEL_ORDER:
        evolution.fail("VALID_FAILED", f"非法视野档位: {target}")
        return False
    current = entry.visibility
    signer = context.deps.get("confirm_signer")
    if _LEVEL_ORDER[target] > _LEVEL_ORDER.get(current, 0):  # 升档：需确认
        if signer is None or not signer.verify(entry.entry_id, target, input.get("confirm_token", "")):
            token = signer.sign(entry.entry_id, target) if signer is not None else ""
            output.update({"confirm_required": True, "confirm_token": token,
                           "from_level": current, "target_level": target})
            return True  # 待确认不是失败（受理语义）
    from_level = entry.visibility
    entry.visibility = target
    store.set_entry_visibility(entry)
    event_id = store.add_visibility_event(entry.entry_id, entry.domain_id, from_level, target,
                                          input.get("confirm_token"))
    published_ref = None
    if target in ("L4", "L5"):  # 发布投影：生成/发布分离（L4 先 draft，S5 publish 生成摘要）
        window_hours = int((context.config.get("governance") or {}).get("publish_withdraw_window_hours", 72))
        item = PublishedItem(
            item_id=store.next_id("pub"), entry_id=entry.entry_id, level=target,
            allowed_purposes=list(input.get("allowed_purposes") or []),
            review_status="published" if target == "L5" else "draft",
            published_at=time.time() if target == "L5" else None,
            withdraw_deadline=time.time() + window_hours * 3600,
        )
        store.save_published(item)
        published_ref = item.item_id
    _audit(context, tenant, "manage.visibility", entry.entry_id,
           {"from": from_level, "to": target, "event_id": event_id, "published": published_ref})
    metrics.counter("manage.visibility")
    output.update({"memory_id": entry.entry_id, "from_level": from_level,
                   "target_level": target, "visibility_event": event_id,
                   "published_item": published_ref})
    return True


# ---------------------------------------------------------------- authz

@unified_method("manage.authz", 1, "orchestration")
def manage_authz(input: dict, output: dict, context, tenant, metrics, evolution) -> bool:
    """API-010：授权三元组管理（grant/revoke；撤回幂等，停止未来检索）。"""
    store = context.deps["store"]
    authz = context.deps["authz"]
    action = input.get("action", "")
    if action == "grant":
        for field in ("grantee", "object_ref", "level", "purpose"):
            if not input.get(field):
                evolution.fail("VALID_FAILED", f"grant 缺必填字段: {field}")
                return False
        record = authz.grant(
            grantee=input["grantee"], object_ref=input["object_ref"], level=input["level"],
            purpose=input["purpose"], allowed_purposes=input.get("allowed_purposes"),
            ttl_seconds=input.get("ttl_seconds"), tenant_id=tenant.tenant_id,
            domain_id=tenant.domain_id)
        _audit(context, tenant, "manage.authz.grant", input["object_ref"],
               {"auth_id": record.auth_id, "grantee": record.grantee,
                "level": record.level, "purpose": record.purpose})
        output.update({"authorization_id": record.auth_id, "status": record.status})
    elif action == "revoke":
        if not authz.revoke(input.get("authorization_id", "")):
            evolution.fail("NOT_FOUND", "授权不存在或已撤回（幂等）")
            return False
        _audit(context, tenant, "manage.authz.revoke", input.get("authorization_id", ""), {})
        output.update({"authorization_id": input.get("authorization_id"), "status": "revoked"})
    else:
        evolution.fail("VALID_FAILED", "action 仅支持 grant|revoke")
        return False
    metrics.counter(f"manage.authz.{action}")
    return True


# ---------------------------------------------------------------- forget

@unified_method("manage.forget", 1, "orchestration")
def manage_forget(input: dict, output: dict, context, tenant, metrics, evolution) -> bool:
    """API-011：遗忘双轨——gray 降权可恢复 / hard 物理删除+血缘级联（唯一真删除）。"""
    store = context.deps["store"]
    memory_id = input.get("memory_id", "")
    mode = input.get("mode", "")
    if store.entries.get(memory_id) is None:
        evolution.fail("NOT_FOUND", "记忆不存在")
        return False
    if mode == "gray":
        store.gray_forget(memory_id)
        _audit(context, tenant, "manage.forget.gray", memory_id, {})
        output.update({"memory_id": memory_id, "mode": "gray", "restorable": True})
    elif mode == "restore":
        if not store.restore_gray(memory_id):
            evolution.fail("VALID_FAILED", "条目不在灰色遗忘态")
            return False
        _audit(context, tenant, "manage.forget.restore", memory_id, {})
        output.update({"memory_id": memory_id, "mode": "restore", "restored": True})
    elif mode == "hard":
        removed = store.hard_forget(memory_id)
        cascade = _cascade_index_removal(context, memory_id)
        _audit(context, tenant, "manage.forget.hard", memory_id, {**removed, **cascade})
        output.update({"memory_id": memory_id, "mode": "hard", "removed": removed,
                       "cascade": cascade})
    else:
        evolution.fail("VALID_FAILED", "mode 仅支持 gray|restore|hard")
        return False
    metrics.counter(f"manage.forget.{mode}")
    return True


def _cascade_index_removal(context, memory_id: str) -> dict:
    """索引级联：全文/向量/图槽位同步移除（硬遗忘=一切索引不留残迹）。"""
    removed: dict[str, int] = {}
    for key, method in (("fts", "remove"), ("vector_store", "remove"), ("graph_store", "remove_node")):
        store_slot = context.deps.get(key)
        if store_slot is None:
            continue
        try:
            if method == "remove":
                removed[key] = store_slot.remove("entries", memory_id)
            else:
                removed[key] = store_slot.remove_node(memory_id)
        except AttributeError:
            continue
    return removed


# ---------------------------------------------------------------- recompute 管道步骤

@unified_method("recompute.collect_stale", 1, "data")
def recompute_collect_stale(input: dict, output: dict, context, tenant, metrics, evolution) -> bool:
    """血缘回溯第一步：stale 标记在 correct 时已落库，此处汇总待重算清单（崩溃可续）。"""
    store = context.deps["store"]
    stale_ids = [entry.entry_id for entry in store.entries.values() if entry.stale]
    output["stale_ids"] = stale_ids
    metrics.gauge("recompute.pending", len(stale_ids))
    return True


@unified_method("recompute.recompute_one", 1, "data")
def recompute_recompute_one(input: dict, output: dict, context, tenant, metrics, evolution) -> bool:
    """逐条重算（work-queue 语义：每次取一条 stale，重算后清除；循环 until 无剩余）。"""
    store = context.deps["store"]
    entry = next((e for e in store.entries.values() if e.stale), None)
    if entry is None:
        output["done"] = True
        return True
    if entry.entry_type == "pattern":  # S4 模式原子：置信随源（derived 边指向的源原子）重算
        source_ids = [edge.dst_id for edge in store.edges
                      if edge.src_id == entry.entry_id and edge.edge_type == "derived"]
        confidences = [store.entries[sid].confidence for sid in source_ids if sid in store.entries]
        if confidences:
            entry.confidence = round(sum(confidences) / len(confidences), 3)
    entry.stale = False
    persist = getattr(store, "set_entry_stale", None)
    if persist is not None:
        persist(entry)
    else:
        store.add_entry(entry)
    output["recomputed"] = (output.get("recomputed") or 0) + 1
    output["done"] = not any(e.stale for e in store.entries.values())
    metrics.counter("recompute.rebuilt")
    return True


@unified_method("recompute.clear_stale", 1, "data")
def recompute_clear_stale(input: dict, output: dict, context, tenant, metrics, evolution) -> bool:
    """收尾清扫：残留 stale 标记归零（幂等）。"""
    store = context.deps["store"]
    cleared = 0
    for entry in store.entries.values():
        if entry.stale:
            entry.stale = False
            persist = getattr(store, "set_entry_stale", None)
            if persist is not None:
                persist(entry)
            cleared += 1
    output["cleared"] = cleared
    return True


class _QuietMetrics:
    def enter(self, *a, **k) -> None: ...
    def exit(self, *a, **k) -> None: ...
    def log(self, *a, **k) -> None: ...
    def counter(self, *a, **k) -> None: ...
    def gauge(self, *a, **k) -> None: ...
    def mark(self, *a, **k) -> None: ...


class _QuietEvo:
    ok = True
    reason_code = None
    reason = None
    retryable = False
    suggestions: list = []
    cause: list = []
