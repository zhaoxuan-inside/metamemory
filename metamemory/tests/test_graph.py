"""T-006/T-007：图约束校验（含 ADR-003 禁静默兜底）与 lite 内嵌执行器。"""
import json
import unittest
from pathlib import Path

from common import make_tenant, stubs

from metamemory_framework.engine.executor import GraphExecutor
from metamemory_framework.engine.graph import pipeline_from_dict, validate_graph

FLOWS = Path(__file__).resolve().parent.parent / "flows" / "core"


def load(name: str):
    return pipeline_from_dict(json.loads((FLOWS / f"{name}.json").read_text(encoding="utf-8")))


class TestGraphValidation(unittest.TestCase):
    def test_ingest_flow_valid(self):
        ctx, tenant, m, evo = stubs()
        out = {}
        self.assertTrue(validate_graph({"pipeline": load("ingest")}, out, ctx, tenant, m, evo))
        self.assertEqual(out["errors"], [])

    def test_cycle_detected(self):
        from metamemory_framework.engine.graph import detect_cycle

        ctx, tenant, m, evo = stubs()
        out = {}
        detect_cycle({"adjacency": {"a": ["b"], "b": ["a"]}}, out, ctx, tenant, m, evo)
        self.assertTrue(out["has_cycle"])

    def test_no_cycle_in_linear(self):
        from metamemory_framework.engine.graph import detect_cycle

        ctx, tenant, m, evo = stubs()
        out = {}
        detect_cycle({"adjacency": {"a": ["b"], "b": []}}, out, ctx, tenant, m, evo)
        self.assertFalse(out["has_cycle"])

    def test_default_must_not_reach_success(self):
        pipeline = load("ingest")
        # 把 default 目标从 review 换成终态节点 → 约束⑤必须拒绝
        pipeline.nodes["route"].edges[-1].to_node = "done"
        ctx, tenant, m, evo = stubs()
        out = {}
        self.assertFalse(validate_graph({"pipeline": pipeline}, out, ctx, tenant, m, evo))
        self.assertTrue(any("约束⑤" in e for e in out["errors"]))

    def test_switch_requires_explicit_default(self):
        pipeline = load("ingest")
        pipeline.nodes["route"].edges = [e for e in pipeline.nodes["route"].edges if not e.is_default]
        ctx, tenant, m, evo = stubs()
        out = {}
        self.assertFalse(validate_graph({"pipeline": pipeline}, out, ctx, tenant, m, evo))
        self.assertTrue(any("缺显式 default" in e for e in out["errors"]))

    def test_wait_requires_timeout(self):
        from metamemory_framework.engine.graph import Edge, Node

        pipeline = load("ingest")
        pipeline.nodes["w"] = Node("w", "wait", edges=[Edge("done")])  # 未配置超时
        pipeline.nodes["gate"].edges.append(Edge("w"))
        ctx, tenant, m, evo = stubs()
        out = {}
        validate_graph({"pipeline": pipeline}, out, ctx, tenant, m, evo)
        self.assertTrue(any("wait 必有超时" in e for e in out["errors"]))


class TestExecutor(unittest.TestCase):
    def _ctx_with_steps(self):
        # 注册一条最小步骤族供执行器驱动
        from metamemory_framework.container.signature import unified_method

        @unified_method("demo.pick", 1, "data")
        def pick(input, output, context, tenant, metrics, evolution) -> bool:
            output["route"] = input.get("route", "new")
            return True

        @unified_method("demo.work", 1, "data")
        def work(input, output, context, tenant, metrics, evolution) -> bool:
            output["worked"] = True
            return True

        return stubs()

    def _mini_flow(self):
        from metamemory_framework.engine.graph import Edge, Node, Pipeline

        nodes = {
            "start": Node("start", "step", step_name="demo.pick",
                          edges=[Edge("branch", when={"==": [{"var": "$.out.route"}, "new"]}),
                                 Edge("done", when={"==": [{"var": "$.out.route"}, "skip"]})]),
            "branch": Node("branch", "step", step_name="demo.work", edges=[Edge("done")]),
            "done": Node("done", "step", node_class="terminal"),
        }
        return Pipeline("mini", 1, "start", nodes, deadline_seconds=60)

    def test_run_flow_happy_path(self):
        ctx, tenant, m, evo = self._ctx_with_steps()
        engine = GraphExecutor()
        task_id = engine.submit(self._mini_flow(), {"route": "new"})
        out = {}
        self.assertTrue(engine.run_flow({"task_id": task_id}, out, ctx, tenant, m, evo))
        self.assertEqual(out["task_status"], "succeeded")
        self.assertTrue(out["outputs"]["branch"].get("worked"))

    def test_run_flow_conditional_shortcut(self):
        ctx, tenant, m, evo = self._ctx_with_steps()
        engine = GraphExecutor()
        task_id = engine.submit(self._mini_flow(), {"route": "skip"})
        out = {}
        self.assertTrue(engine.run_flow({"task_id": task_id}, out, ctx, tenant, m, evo))
        self.assertEqual(out["task_status"], "succeeded")
        self.assertNotIn("branch", out["outputs"])  # 条件边直取终态

    def test_missing_task(self):
        ctx, tenant, m, evo = self._ctx_with_steps()
        engine = GraphExecutor()
        out = {}
        self.assertFalse(engine.run_flow({"task_id": "nope"}, out, ctx, tenant, m, evo))
        self.assertEqual(evo.reason_code, "TASK_NOT_FOUND")

    def test_abstain_routes_to_review_only(self):
        from metamemory_framework.container.policy import Undecidable
        from metamemory_framework.container.signature import unified_method
        from metamemory_framework.engine.graph import Edge, Node, Pipeline

        @unified_method("demo.undecidable", 1, "data")
        def undecidable(input, output, context, tenant, metrics, evolution) -> bool:
            raise Undecidable("entity_merge_conflict")

        nodes = {
            "start": Node("start", "step", step_name="demo.undecidable",
                          edges=[Edge("next"), Edge("review", None, True)]),
            "next": Node("next", "step", step_name="demo.work", edges=[Edge("done")]),
            "review": Node("review", "step", node_class="review", edges=[Edge("done")]),
            "done": Node("done", "step", node_class="terminal"),
        }
        pipeline = Pipeline("abst", 1, "start", nodes, deadline_seconds=60)
        ctx, tenant, m, evo = self._ctx_with_steps()
        engine = GraphExecutor()
        task_id = engine.submit(pipeline, {})
        out = {}
        engine.run_flow({"task_id": task_id}, out, ctx, tenant, m, evo)
        self.assertEqual(out["task_status"], "succeeded")
        self.assertNotIn("next", out["outputs"])                # 常规后继不激活
        self.assertIn("review", out["outputs"])                 # 汇入 review
        self.assertEqual(engine.review_queue[0]["reason"], "entity_merge_conflict")

    def test_loop_until_terminates(self):
        from metamemory_framework.container.signature import unified_method
        from metamemory_framework.engine.graph import Edge, Node, Pipeline

        @unified_method("demo.tick", 1, "data")
        def tick(input, output, context, tenant, metrics, evolution) -> bool:
            output["count"] = input.get("count", 0) + 1
            output["done"] = output["count"] >= 3
            return True

        nodes = {
            "loop": Node("loop", "loop",
                         config={"until": {"==": [{"var": "$.out.done"}, True]}, "max_iterations": 10,
                                 "body": ["demo.tick"]},
                         edges=[Edge("done")]),
            "done": Node("done", "step", node_class="terminal"),
        }
        pipeline = Pipeline("loopf", 1, "loop", nodes, deadline_seconds=60)
        ctx, tenant, m, evo = self._ctx_with_steps()
        engine = GraphExecutor()
        task_id = engine.submit(pipeline, {})
        out = {}
        self.assertTrue(engine.run_flow({"task_id": task_id}, out, ctx, tenant, m, evo))
        self.assertEqual(out["task_status"], "succeeded")
        self.assertGreaterEqual(out["steps"]["loop"]["iterations"] + 1, 3)


if __name__ == "__main__":
    unittest.main()
