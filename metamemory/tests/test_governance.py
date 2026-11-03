"""M3 信任完整验收（S2 切片）：manage 四子动词/血缘回溯/意图状态机/审计链/计量。"""
import tempfile
import unittest
from pathlib import Path

import bootstrap  # noqa: F401  sys.path 引导 + 步骤注册
from common import make_tenant  # noqa: F401
from metamemory.config import MetaMemoryConfig, StorageConfig
from metamemory_framework.container.types import Context, Evolution
from metamemory_framework.observe.metrics import MetricsImpl
from metamemory_framework.security.context import set_tenant


class _FakeProvider:
    """测试替身：结构化原子提取（ADR-006 唯一路径的测试注入点）。"""

    def complete(self, request: dict) -> dict:
        if request.get("task") == "nli":  # NLI 裁判替身：含否定/取消词判矛盾
            negate = any(w in request.get("prompt", "") for w in ("不是", "不再", "没有", "取消"))
            verdict = "contradict" if negate else "entail"
            return {"text": '{"verdict": "%s", "confidence": 0.9}' % verdict,
                    "tokens": 5, "provider": "fake"}
        atoms = [{"text": line, "subject": "user", "predicate": "mentioned",
                  "quote": line, "entry_type": "fact"}
                 for line in request.get("prompt", "").split("\n") if line.strip()]
        return {"atoms": atoms, "tokens": 20, "provider": "fake"}


class GovernanceTestBase(unittest.TestCase):
    def setUp(self):
        self.composition = bootstrap.build_composition(
            config=MetaMemoryConfig(storage=StorageConfig(data_dir=tempfile.mkdtemp())),
            db_path=":memory:")
        self.composition["inference"] = _FakeProvider()
        self.store = self.composition["store"]
        self.audit = self.composition["audit"]
        self.metering = self.composition["metering"]

    def session(self):
        tenant = make_tenant()
        set_tenant(tenant)
        context = Context(
            deps={"store": self.store, "authz": self.composition["authz"],
                  "engine": self.composition["engine"], "inference": self.composition["inference"],
                  "call_cache": self.composition["call_cache"], "flows": self.composition["flows"],
                  "audit": self.audit, "metering": self.metering,
                  "confirm_signer": self.composition["confirm_signer"],
                  "fts": self.composition["fts"], "vector_store": self.composition["vector_store"],
                  "graph_store": self.composition["graph_store"]},
            config=self.composition["config"])
        return context, tenant, MetricsImpl(), Evolution()

    def add_memory(self, content: str, source_uri: str) -> str:
        context, tenant, metrics, evolution = self.session()
        out: dict = {}
        assert self.composition["protocol"]["add"](
            {"event": {"source_uri": source_uri, "actor": tenant.actor,
                       "occurred_at": "2026-09-27T10:00:00+08:00", "content": content}},
            out, context, tenant, metrics, evolution)
        task_out: dict = {}
        self.composition["engine"].run_flow({"task_id": out["task_id"]}, task_out,
                                            context, tenant, metrics, evolution)
        assert task_out.get("task_status") == "succeeded", task_out
        return out["task_id"]

    def call_manage(self, action: str, payload: dict):
        from metamemory.protocol import governance

        context, tenant, metrics, evolution = self.session()
        out: dict = {}
        step = getattr(governance, f"manage_{action}")
        ok = step(payload, out, context, tenant, metrics, evolution)
        return ok, out, evolution

    def first_entry(self):
        return next(iter(self.store.entries.values()))

    def seed(self):
        self.add_memory("我一定要记住：我在北京做后端工程师，工号 12345", "conv://g1")
        return self.first_entry()


class TestManageVerbs(GovernanceTestBase):
    def seed_removed(self):  # seed 上移至基类
        pass
    def test_correct_supersedes_and_recomputes(self):
        """纠错：S1 新原子关闭旧原子 + 血缘下游 stale → recompute 清零（错误不扩散）。"""
        old = self.seed()
        ok, out, evolution = self.call_manage("correct", {
            "memory_id": old.entry_id, "correction": "我在上海工作，职位是后端工程师"})
        self.assertTrue(ok, evolution.reason_code)
        self.assertEqual(out["superseded"], old.entry_id)
        corrected = self.store.entries[out["memory_id"]]
        self.assertEqual(corrected.source_level, "S1")
        self.assertEqual(corrected.confidence, 1.0)
        self.assertEqual(corrected.supersedes, old.entry_id)
        self.assertIsNotNone(old.valid_to)  # 双时态关闭
        self.assertEqual(out["task_status"] if "task_status" in out else "n/a", "n/a")
        # recompute 流程已跑：无残留 stale（out 带 task_id，任务 succeeded）
        self.assertFalse(any(e.stale for e in self.store.entries.values()))
        records = self.audit.tail(action_prefix="manage.correct")
        self.assertEqual(records[0]["object_ref"], old.entry_id)

    def test_visibility_down_immediate_up_needs_confirm(self):
        """视野变更：降档即生效；升档需确认（二次提交 confirm_token）。"""
        entry = self.seed()
        ok, out, evolution = self.call_manage("visibility", {
            "memory_id": entry.entry_id, "target_level": "L1"})
        self.assertTrue(ok, evolution.reason_code)
        self.assertEqual(out["from_level"], "L2")  # 降档即生效
        self.assertEqual(self.store.entries[entry.entry_id].visibility, "L1")
        # 升档 L1→L3：第一次返回待确认
        ok, out, evolution = self.call_manage("visibility", {
            "memory_id": entry.entry_id, "target_level": "L3"})
        self.assertTrue(ok)
        self.assertTrue(out["confirm_required"])
        token = out["confirm_token"]
        self.assertEqual(self.store.entries[entry.entry_id].visibility, "L1")  # 未生效
        ok, out, evolution = self.call_manage("visibility", {
            "memory_id": entry.entry_id, "target_level": "L3", "confirm_token": token})
        self.assertTrue(ok, evolution.reason_code)
        self.assertEqual(self.store.entries[entry.entry_id].visibility, "L3")
        self.assertEqual(len(self.audit.tail(action_prefix="manage.visibility")), 2)

    def test_visibility_l4_creates_published_projection(self):
        """L4 发布=变更（升档需确认）：发布投影 draft + 允许目的集 + 撤回窗。"""
        entry = self.seed()
        ok, out, evolution = self.call_manage("visibility", {
            "memory_id": entry.entry_id, "target_level": "L4",
            "allowed_purposes": ["job_matching"]})
        self.assertTrue(ok, evolution.reason_code)
        self.assertTrue(out["confirm_required"])  # 升档第一段：返回确认令牌
        ok, out, evolution = self.call_manage("visibility", {
            "memory_id": entry.entry_id, "target_level": "L4",
            "allowed_purposes": ["job_matching"], "confirm_token": out["confirm_token"]})
        self.assertTrue(ok, evolution.reason_code)
        item = self.store.published[out["published_item"]]
        self.assertEqual(item.level, "L4")
        self.assertEqual(item.review_status, "draft")  # 生成（摘要）前是草稿
        self.assertEqual(item.allowed_purposes, ["job_matching"])
        self.assertIsNotNone(item.withdraw_deadline)

    def test_authz_grant_revoke_idempotent(self):
        """授权三元组：grant→生效；revoke 幂等（停止未来检索）；审计留痕。"""
        entry = self.seed()
        ok, out, evolution = self.call_manage("authz", {
            "action": "grant", "grantee": "bob", "object_ref": entry.entry_id,
            "level": "L2", "purpose": "assist", "ttl_seconds": 3600})
        self.assertTrue(ok, evolution.reason_code)
        auth_id = out["authorization_id"]
        self.assertIsNotNone(self.composition["authz"].active_for("bob", entry.entry_id))
        ok, out, _ = self.call_manage("authz", {"action": "revoke", "authorization_id": auth_id})
        self.assertTrue(ok)
        self.assertIsNone(self.composition["authz"].active_for("bob", entry.entry_id))
        ok, out, evolution = self.call_manage("authz", {"action": "revoke", "authorization_id": auth_id})
        self.assertFalse(ok)  # 撤回幂等：二次撤回显式失败（非静默成功）
        self.assertEqual(evolution.reason_code, "NOT_FOUND")

    def test_forget_gray_restore_hard(self):
        """遗忘双轨：gray 降权退出检索可恢复；hard 物理删除+级联（唯一真删除）。"""
        entry = self.seed()
        context, tenant, metrics, evolution = self.session()
        # gray：检索面消失
        ok, out, _ = self.call_manage("forget", {"memory_id": entry.entry_id, "mode": "gray"})
        self.assertTrue(ok)
        found = self.store.entries[entry.entry_id].gray_forgotten
        self.assertTrue(found)
        ok, out, _ = self.call_manage("forget", {"memory_id": entry.entry_id, "mode": "restore"})
        self.assertTrue(ok)
        self.assertFalse(self.store.entries[entry.entry_id].gray_forgotten)
        # hard：物理删除 + supersedes 引用清理 + 索引级联
        ok, out, _ = self.call_manage("forget", {"memory_id": entry.entry_id, "mode": "hard"})
        self.assertTrue(ok)
        self.assertNotIn(entry.entry_id, self.store.entries)
        self.assertEqual(out["cascade"].get("fts"), 1)  # 全文索引同步移除
        # 审计只追加不删除：硬遗忘记录在链且链完整（审计自身治理）
        hard_records = self.audit.tail(action_prefix="manage.forget.hard")
        self.assertEqual(hard_records[0]["object_ref"], entry.entry_id)
        self.assertTrue(self.audit.verify()["ok"])

    def test_forget_not_found_explicit(self):
        ok, out, evolution = self.call_manage("forget", {"memory_id": "mem_none", "mode": "gray"})
        self.assertFalse(ok)
        self.assertEqual(evolution.reason_code, "NOT_FOUND")


class TestLineageRecompute(GovernanceTestBase):
    def test_collect_one_clear(self):
        """recompute 三步：collect 汇总 → recompute_one 逐条（work-queue）→ clear 清扫。"""
        from metamemory.protocol import governance

        entry = self.seed()
        entry.stale = True
        self.store.set_entry_stale(entry)
        context, tenant, metrics, evolution = self.session()
        out: dict = {}
        governance.recompute_collect_stale({}, out, context, tenant, metrics, evolution)
        self.assertEqual(out["stale_ids"], [entry.entry_id])
        governance.recompute_recompute_one({}, out, context, tenant, metrics, evolution)
        self.assertTrue(out["done"])
        self.assertFalse(self.store.entries[entry.entry_id].stale)

    def test_derived_downstream_marked_stale(self):
        """血缘 BFS：derived 边下游随源纠错进入 stale（正确性传播的半径）。"""
        from metamemory.protocol import governance

        src = self.seed()
        derived = self.store.next_id("mem")
        from metamemory.domain.models import AssociationEdge, MemoryEntry

        child = MemoryEntry(entry_id=derived, tenant_id="t1", domain_id="d1", subject="user",
                            predicate="summarized", object_value="派生陈述", entry_type="fact",
                            valid_from="2026-09-27T10:00:00+08:00", anchor_episode=src.anchor_episode,
                            source_level="S4", confidence=0.8)
        self.store.add_entry(child)
        self.store.add_edge(AssociationEdge(edge_id=self.store.next_id("edge"),
                                            edge_type="derived", src_id=derived,
                                            dst_id=src.entry_id, evidence={"rule": "test"}))
        stale = governance.mark_lineage_stale(self.store, [src.entry_id])
        self.assertIn(derived, stale)
        self.assertTrue(self.store.entries[derived].stale)


class TestAuditChain(GovernanceTestBase):
    def test_chain_verify_and_tamper_detection(self):
        """审计链：append 链式防篡改；篡改中间记录 → verify 断链定位。"""
        for i in range(3):
            self.audit.append("alice", f"test.action.{i}", f"obj_{i}", tenant_id="t1")
        result = self.audit.verify()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["checked"], 3)
        # 篡改第二条（绕过 append 面，模拟直接改库）
        self.store.db.execute("UPDATE audit_log SET action='hacked' WHERE id=2")
        result = self.audit.verify()
        self.assertFalse(result["ok"])
        self.assertEqual(result["broken_at"], 2)


class TestMetering(GovernanceTestBase):
    def test_add_search_llm_metered(self):
        """计量横切：写入/检索/LLM token 三维度落账（配额即 COGS 上界的账本）。"""
        self.add_memory("我一定要记住：我在北京做后端工程师，工号 12345", "conv://m1")
        context, tenant, metrics, evolution = self.session()
        self.composition["protocol"]["search"]({"query": "北京 工作"}, {},
                                               context, tenant, metrics, evolution)
        summary = {row["dimension"]: row["total"] for row in self.metering.summary("t1")}
        self.assertGreaterEqual(summary.get("storage.events", 0), 1)
        self.assertGreaterEqual(summary.get("search.served", 0), 1)
        self.assertGreaterEqual(summary.get("llm.tokens", 0), 20)


class TestIntentStateMachine(GovernanceTestBase):
    def call_intent(self, step_name: str, payload: dict):
        from metamemory.protocol import intents

        context, tenant, metrics, evolution = self.session()
        out: dict = {}
        ok = getattr(intents, step_name)(payload, out, context, tenant, metrics, evolution)
        return ok, out, evolution

    def test_declared_active_agent_candidate(self):
        """双入口：声明直生效 / Agent 只产候选（默认仅自己可见）。"""
        ok, out, evolution = self.call_intent("intent_declare", {
            "content": "我在找 AI 记忆方向的合作", "visibility": "L1"})
        self.assertTrue(ok, evolution.reason_code)
        self.assertEqual(out["state"], "active")
        ok, out, evolution = self.call_intent("intent_declare", {
            "content": "疑似想换工作", "origin": "agent_candidate", "visibility": "L1"})
        self.assertTrue(ok, evolution.reason_code)
        self.assertEqual(out["state"], "candidate")
        # 候选需人裁确认才生效
        ok, out, evolution = self.call_intent("intent_confirm", {
            "intent_id": out["intent_id"], "decision": "confirm"})
        self.assertTrue(ok, evolution.reason_code)
        self.assertEqual(out["state"], "active")

    def test_confirm_reject_completes(self):
        ok, out, _ = self.call_intent("intent_declare", {
            "content": "Agent 捕捉的意图", "origin": "agent_candidate"})
        intent_id = out["intent_id"]
        ok, out, evolution = self.call_intent("intent_confirm", {
            "intent_id": intent_id, "decision": "reject"})
        self.assertTrue(ok, evolution.reason_code)
        self.assertEqual(out["state"], "completed")
        ok, out, evolution = self.call_intent("intent_confirm", {
            "intent_id": intent_id, "decision": "confirm"})
        self.assertFalse(ok)  # 终态不可再确认
        self.assertEqual(evolution.reason_code, "VALID_FAILED")

    def test_discovery_quota_g3(self):
        """发现位配额（G3）：免费 3 个生效网络意图；意图本体不限量（L1 不占位）。"""
        for i in range(3):
            ok, out, evolution = self.call_intent("intent_declare", {
                "content": f"网络意图{i}", "visibility": "L3"})
            self.assertTrue(ok, evolution.reason_code)
        ok, out, evolution = self.call_intent("intent_declare", {
            "content": "第 4 个网络意图", "visibility": "L3"})
        self.assertFalse(ok)
        self.assertEqual(evolution.reason_code, "QUOTA_EXCEEDED")
        # 意图本体不限量：L1（不进网络）仍可声明
        ok, out, evolution = self.call_intent("intent_declare", {
            "content": "私有意图不占位", "visibility": "L1"})
        self.assertTrue(ok, evolution.reason_code)
        private_id = out["intent_id"]
        # 把一个网络意图降档释放配额后，私有意图可升入网络
        first_network = next(i for i in self.store.intents.values()
                             if i.state == "active" and i.visibility == "L3")
        ok, out, evolution = self.call_intent("intent_update", {
            "intent_id": first_network.entry_id, "visibility": "L1"})
        self.assertTrue(ok, evolution.reason_code)
        ok, out, evolution = self.call_intent("intent_update", {
            "intent_id": private_id, "visibility": "L3"})
        self.assertTrue(ok, evolution.reason_code)

    def test_ttl_expiry_sweep(self):
        """意图到期：自动转 expired → 退出发现索引（血缘级联撤下）。"""
        ok, out, evolution = self.call_intent("intent_declare", {
            "content": "短时效意图", "visibility": "L3", "ttl_expires_at": "2000-01-01T00:00:00+08:00"})
        self.assertTrue(ok, evolution.reason_code)
        intent_id = out["intent_id"]
        ok, out, evolution = self.call_intent("intent_sweep_expiry", {})
        self.assertTrue(ok, evolution.reason_code)
        self.assertIn(intent_id, out["expired"])
        self.assertEqual(self.store.get_intent(intent_id).state, "expired")

    def test_intent_anchor_exception_persists(self):
        """意图条目无原文情境（声明即来源）：schema 唯一例外，重启重建不丢。"""
        ok, out, evolution = self.call_intent("intent_declare", {"content": "无锚点意图"})
        self.assertTrue(ok, evolution.reason_code)
        intent_id = out["intent_id"]
        from metamemory.domain.persist import PersistentMemoryStore

        reopened = PersistentMemoryStore(self.composition["db"])
        self.assertIn(intent_id, reopened.entries)
        self.assertEqual(reopened.entries[intent_id].entry_type, "intent")


if __name__ == "__main__":
    unittest.main()
