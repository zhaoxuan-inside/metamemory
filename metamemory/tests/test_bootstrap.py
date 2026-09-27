"""T-020：组合根冒烟——装配→装载→校验→端到端 add→search（lite 全闭环）。"""
import unittest

import bootstrap
from metamemory_framework.security.context import make_tenant, set_tenant
from metamemory_framework.container.types import Evolution
from metamemory_framework.observe.metrics import MetricsImpl


class TestBootstrapEndToEnd(unittest.TestCase):
    def setUp(self):
        self.composition = bootstrap.build_composition()
        set_tenant(make_tenant("t1", "d1", "alice"))

    def _context(self):
        from metamemory_framework.container.types import Context

        return Context(deps={"store": self.composition["store"], "authz": self.composition["authz"],
                             "engine": self.composition["engine"],
                             "inference": self.composition["inference"],
                             "flows": self.composition["flows"]},
                       config=self.composition["config"], tenant=make_tenant("t1", "d1", "alice"))

    def test_composition_shape(self):
        for key in ("store", "authz", "engine", "inference", "flows", "registry", "protocol", "config"):
            self.assertIn(key, self.composition)

    def test_end_to_end_add_then_search(self):
        """受理即落袋 → 引擎驱动 ingest 流程 → 原子入库 → search 可召回（US-1 冒烟）。"""
        add = self.composition["protocol"]["add"]
        search = self.composition["protocol"]["search"]
        ctx, m, evo = self._context(), MetricsImpl(), Evolution()
        out = {}
        self.assertTrue(add({"event": {"source_uri": "conv://e2e", "actor": "alice",
                                       "occurred_at": "2026-09-27T10:00:00+08:00",
                                       "content": "我一定要在周五前把方案发给客户，这是重要承诺。我最近在准备 Agent 记忆项目"}},
                           out, ctx, m, evo))
        self.assertTrue(out["task_id"])
        # 同步驱动 ingest 流程（lite 内嵌执行器）
        task_out = {}
        engine_result = self.composition["engine"].run_flow({"task_id": out["task_id"]}, task_out, ctx, m, evo)
        self.assertTrue(engine_result, f"ingest 流程未成功: {task_out}")
        self.assertEqual(task_out["task_status"], "succeeded")
        self.assertGreater(len(self.composition["store"].entries), 0, "原文应被提取为语义原子")
        # 查询：原文落袋内容可检索（快通道承诺的检索面）
        search_out = {}
        self.assertTrue(search({"query": "user mentioned 方案"}, search_out, ctx, m, evo))
        self.assertIsNotNone(search_out["items"])
        self.assertGreaterEqual(len(search_out["items"]), 1, "提取路由的原子应可召回")
        # 审计面：复核队列存在（四态路由的 abstain 汇口）
        self.assertIsInstance(self.composition["engine"].review_queue, list)

    def test_duplicate_content_shortcuts_via_switch(self):
        add = self.composition["protocol"]["add"]
        ctx, m, evo = self._context(), MetricsImpl(), Evolution()
        event = {"source_uri": "conv://dup1", "actor": "alice",
                 "occurred_at": "2026-09-27T10:00:00+08:00", "content": "完全相同的记忆内容Q"}
        out1, out2 = {}, {}
        self.assertTrue(add({"event": event}, out1, ctx, m, evo))
        self.composition["engine"].run_flow({"task_id": out1["task_id"]}, {}, ctx, m, evo)
        self.assertTrue(add({"event": {**event, "source_uri": "conv://dup2"}}, out2, ctx, m, evo))
        task_out = {}
        self.composition["engine"].run_flow({"task_id": out2["task_id"]}, task_out, ctx, m, evo)
        self.assertEqual(task_out["task_status"], "succeeded")
        # dedupe → switch → done（重复内容不再进提取）
        self.assertIn("dedupe", task_out["outputs"])
        self.assertEqual(task_out["outputs"]["dedupe"].get("route"), "duplicate")


if __name__ == "__main__":
    unittest.main()
