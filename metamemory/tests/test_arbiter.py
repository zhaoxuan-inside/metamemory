"""T-011：VisionArbiter——粗筛/精裁/目的闸/唯一裁决入口（US-5/6/7）。"""
import unittest

from common import make_context, stubs

from metamemory.protocol.arbiter import arbiter_decide, coarse_filter, fine_check, purpose_gate


def candidate(**kw):
    base = {"memory_id": "mem_1", "tenant_id": "t1", "domain_id": "d1",
            "visibility": "L2", "source_level": "S1", "confidence": 0.9}
    base.update(kw)
    return base


class TestCoarseFilter(unittest.TestCase):
    def test_same_tenant_allowed(self):
        ctx, m, evo = stubs()
        out = {}
        coarse_filter({"candidates": [candidate()], "querier": {"tenant_id": "t1"},
                       "authz": ctx.deps["authz"]}, out, ctx, m, evo)
        self.assertEqual(len(out["allowed"]), 1)

    def test_cross_tenant_l1_blocked(self):
        ctx, m, evo = stubs()
        out = {}
        coarse_filter({"candidates": [candidate(tenant_id="t2")], "querier": {"tenant_id": "t1"},
                       "authz": ctx.deps["authz"]}, out, ctx, m, evo)
        self.assertEqual(out["denials"][0]["reason"], "TENANT_SCOPE")

    def test_cross_tenant_published_l4_allowed(self):
        ctx, m, evo = stubs()
        out = {}
        coarse_filter({"candidates": [candidate(tenant_id="t2", visibility="L4", published=True)],
                       "querier": {"tenant_id": "t1"}, "authz": ctx.deps["authz"]},
                      out, ctx, m, evo)
        self.assertEqual(len(out["allowed"]), 1)

    def test_authz_bridges_cross_tenant(self):
        ctx, m, evo = stubs()
        ctx.deps["authz"].grant(grantee="alice", object_ref="mem_9", level="L2", purpose="assist")
        out = {}
        coarse_filter({"candidates": [candidate(tenant_id="t2", memory_id="mem_9")],
                       "querier": {"tenant_id": "t1", "actor": "alice"}, "authz": ctx.deps["authz"]},
                      out, ctx, m, evo)
        self.assertEqual(len(out["allowed"]), 1)


class TestFineCheck(unittest.TestCase):
    def test_l1_stays_in_domain(self):
        ctx, m, evo = stubs()
        out = {}
        fine_check({"candidates": [candidate(visibility="L1", domain_id="d_other")],
                    "querier": {"domain_id": "d1"}}, out, ctx, m, evo)
        self.assertEqual(out["denials"][0]["reason"], "L1_DOMAIN_ONLY")

    def test_injected_suspect_forced_l1(self):
        ctx, m, evo = stubs()
        out = {}
        result = fine_check({"candidates": [candidate(visibility="L4", injected_suspect=True)],
                             "querier": {"domain_id": "d1"}}, out, ctx, m, evo)
        self.assertTrue(result)
        self.assertEqual(out["allowed"][0]["visibility"], "L1")  # 注入疑产物禁止自动升档

    def test_revoked_denied(self):
        ctx, m, evo = stubs()
        out = {}
        fine_check({"candidates": [candidate(revoked=True)], "querier": {"domain_id": "d1"}},
                   out, ctx, m, evo)
        self.assertEqual(out["denials"][0]["reason"], "AUTHZ_REVOKED")


class TestPurposeGate(unittest.TestCase):
    def test_l4_requires_purpose(self):
        ctx, m, evo = stubs()
        out = {}
        purpose_gate({"candidates": [candidate(visibility="L4")], "declared_purpose": None},
                     out, ctx, m, evo)
        self.assertEqual(out["denials"][0]["reason"], "PURPOSE_REQUIRED")

    def test_purpose_mismatch_blocked(self):
        ctx, m, evo = stubs()
        out = {}
        purpose_gate({"candidates": [candidate(visibility="L4", published_purposes=["job_matching"])],
                      "declared_purpose": "ad_marketing"}, out, ctx, m, evo)
        self.assertEqual(out["denials"][0]["reason"], "PURPOSE_MISMATCH")

    def test_purpose_match_allowed(self):
        ctx, m, evo = stubs()
        out = {}
        purpose_gate({"candidates": [candidate(visibility="L4", published_purposes=["job_matching"])],
                      "declared_purpose": "job_matching"}, out, ctx, m, evo)
        self.assertEqual(len(out["allowed"]), 1)


class TestArbiterDecide(unittest.TestCase):
    def test_end_to_end_three_stages(self):
        ctx, m, evo = stubs()
        candidates = [
            candidate(memory_id="m_in", visibility="L2"),                                  # 域内可达
            candidate(memory_id="m_pub", visibility="L4", tenant_id="t2", published=True,
                      published_purposes=["job_matching"]),                                # 公开面
            candidate(memory_id="m_block", visibility="L4", tenant_id="t2", published=True,
                      published_purposes=["ad_marketing"]),                                # 目的不匹配
        ]
        out = {}
        ok = arbiter_decide({"candidates": candidates, "querier": {"tenant_id": "t1", "domain_id": "d1",
                                                                  "actor": "alice"},
                             "authz": ctx.deps["authz"], "declared_purpose": "job_matching"},
                            out, ctx, m, evo)
        self.assertTrue(ok)
        allowed_ids = {c["memory_id"] for c in out["allowed"]}
        self.assertEqual(allowed_ids, {"m_in", "m_pub"})
        self.assertTrue(any(d["reason"] == "PURPOSE_MISMATCH" for d in out["denials"]))
        self.assertTrue(out["audit_ref"].startswith("audit:"))


if __name__ == "__main__":
    unittest.main()
