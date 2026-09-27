"""T-016：查询读路径——五路召回/RRF 融合/预算组装（C-G：rerank 可降档）。"""
import unittest

from common import stubs

from metamemory.query.express import budget_pack
from metamemory.query.fusion import rrf_fuse
from metamemory.query.recall import query_recall


class TestRrfFuse(unittest.TestCase):
    def test_multi_path_summation(self):
        ctx, m, evo = stubs()
        out = {}
        ok = rrf_fuse({"ranked_lists": {"fulltext": ["a", "b"], "semantic": ["b", "c"]}}, out, ctx, m, evo)
        self.assertTrue(ok)
        fused = {f["memory_id"]: f["fusion_score"] for f in out["fused"]}
        self.assertGreater(fused["b"], fused["a"])  # 两路都命中者胜
        self.assertGreater(fused["b"], fused["c"])

    def test_custom_k(self):
        ctx, m, evo = stubs()
        out = {}
        rrf_fuse({"ranked_lists": {"p": ["a"]}, "rrf_k": 1}, out, ctx, m, evo)
        self.assertAlmostEqual(out["fused"][0]["fusion_score"], 0.5)


class TestBudgetPack(unittest.TestCase):
    def test_respects_budget(self):
        ctx, m, evo = stubs()
        items = [{"memory_id": f"m{i}", "content": "x" * 40} for i in range(5)]
        out = {}
        ok = budget_pack({"items": items, "token_budget": 50}, out, ctx, m, evo)
        self.assertTrue(ok)
        self.assertLessEqual(out["budget_used"], 50)
        self.assertGreater(out["dropped"], 0)

    def test_all_fit(self):
        ctx, m, evo = stubs()
        out = {}
        budget_pack({"items": [{"memory_id": "m", "content": "hi"}], "token_budget": 100}, out, ctx, m, evo)
        self.assertEqual(out["dropped"], 0)


class TestRecall(unittest.TestCase):
    def test_five_paths_merge(self):
        ctx, m, evo = stubs()
        store = ctx.deps["store"]
        from metamemory.ingest.organize import organize_links

        organize_links({
            "episode_id": "ep_1",
            "normalized": {"occurred_at": "2026-09-27T10:00:00+08:00"},
            "routes": [{"route": "extract", "text": "用户在准备记忆项目", "subject": "user",
                        "predicate": "working_on", "confidence": 0.9, "suspect": False,
                        "span": [0, 4], "entry_type": "fact"}],
        }, {}, ctx, m, evo)
        out = {}
        self.assertTrue(query_recall({"query": "user working_on 记忆项目", "episode_ref": "ep_1"},
                                     out, ctx, m, evo))
        self.assertGreaterEqual(len(out["candidates"]), 1)
        paths = out["candidates"][0]["paths"]
        self.assertIn("fulltext", paths)
        self.assertIn("timeline", paths)
        self.assertIn("context", paths)  # episode 直取（编码特异性）

    def test_empty_store_returns_empty(self):
        ctx, m, evo = stubs()
        out = {}
        self.assertTrue(query_recall({"query": "nothing here"}, out, ctx, m, evo))
        self.assertEqual(out["candidates"], [])


if __name__ == "__main__":
    unittest.main()
