"""T-012：协议门面——add（202 语义/幂等/配额）与 search（裁决出口/不知道就说不知道）。"""
import unittest

from common import make_context, stubs

from metamemory.domain.models import MemoryEntry
from metamemory.protocol.facade import protocol_add, protocol_search


class FakeEngine:
    def __init__(self, pipeline) -> None:
        self.pipeline = pipeline
        self.submitted = []

    def submit(self, pipeline, params, task_id=None):
        self.submitted.append((pipeline, params))
        return f"tsk_{len(self.submitted):06d}"


class TestProtocolAdd(unittest.TestCase):
    def _ctx(self, **deps):
        ctx, tenant, m, evo = stubs()
        ctx.deps["engine"] = FakeEngine(pipeline=object())
        ctx.deps["flows"] = {"ingest": ctx.deps["engine"].pipeline}
        ctx.deps.update(deps)
        return ctx, tenant, m, evo

    def _event(self, uri="conv://a1"):
        return {"source_uri": uri, "content": "我偏好深色主题", "actor": "alice",
                "occurred_at": "2026-09-27T09:00:00+08:00"}

    def test_add_returns_202_semantics(self):
        ctx, tenant, m, evo = self._ctx()
        out = {}
        self.assertTrue(protocol_add({"event": self._event()}, out, ctx, tenant, m, evo))
        self.assertTrue(out["task_id"].startswith("tsk_"))
        self.assertTrue(out["fastpath"]["stored"])
        self.assertEqual(out["fastpath"]["searchable_in"], "<100ms")
        self.assertFalse(out["idempotency"]["replayed"])
        self.assertIn("store", ctx.deps)  # 原文已落袋（快通道）

    def test_add_idempotent_replay(self):
        ctx, tenant, m, evo = self._ctx()
        out1, out2 = {}, {}
        protocol_add({"event": self._event()}, out1, ctx, tenant, m, evo)
        protocol_add({"event": self._event()}, out2, ctx, tenant, m, evo)
        self.assertTrue(out2["idempotency"]["replayed"])

    def test_add_quota_blocked(self):
        ctx, tenant, m, evo = self._ctx()
        ctx.config["max_pending_events"] = 0
        out = {}
        self.assertFalse(protocol_add({"event": self._event()}, out, ctx, tenant, m, evo))
        self.assertEqual(evo.reason_code, "QUOTA_EXCEEDED")
        self.assertTrue(evo.retryable)


class TestProtocolSearch(unittest.TestCase):
    def _seed(self, ctx, tenant, m, evo):
        store = ctx.deps["store"]
        store.add_entry(MemoryEntry(
            entry_id=store.next_id("mem"), tenant_id=tenant.tenant_id,
            domain_id=tenant.domain_id, subject="user", predicate="prefers",
            object_value="deep dark theme in editor", entry_type="preference",
            valid_from="2026-09-01T00:00:00+08:00", source_level="S1", confidence=0.95,
            visibility="L2", anchor_episode="ep_x",
        ))

    def test_search_returns_decorated_items(self):
        ctx, tenant, m, evo = stubs()
        self._seed(ctx, tenant, m, evo)
        out = {}
        self.assertTrue(protocol_search({"query": "user prefers theme"}, out, ctx, tenant, m, evo))
        self.assertEqual(len(out["items"]), 1)
        item = out["items"][0]
        self.assertIn("fusion_score", item)
        self.assertIn("confidence", item)
        self.assertEqual(out["degraded"], {"rerank": False})  # lite 档 RRF 直出（C-G）

    def test_search_unanswered_when_nothing(self):
        ctx, tenant, m, evo = stubs()
        out = {}
        self.assertTrue(protocol_search({"query": "完全不相关的查询词"}, out, ctx, tenant, m, evo))
        self.assertEqual(out["items"], [])
        self.assertEqual(out["unanswered"], {"reason": "NO_MATCH"})  # 不知道就说不知道

    def test_search_purpose_gate_on_public(self):
        ctx, tenant, m, evo = stubs()
        self._seed(ctx, tenant, m, evo)
        entry = list(ctx.deps["store"].entries.values())[0]
        entry.visibility = "L4"
        out = {}
        protocol_search({"query": "user prefers theme", "purpose": "job_matching"}, out, ctx, tenant, m, evo)
        self.assertEqual(out["items"], [])
        self.assertTrue(any(d["reason"] in ("PURPOSE_MISMATCH", "PURPOSE_REQUIRED") for d in out["denials"]))


if __name__ == "__main__":
    unittest.main()
