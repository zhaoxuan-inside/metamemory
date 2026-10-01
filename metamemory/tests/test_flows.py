"""T-017/T-020：流程定义装载与编译校验（数据化流程图，ADR-005 契约样例）。"""
import json
import unittest
from pathlib import Path

from common import make_tenant, stubs  # noqa: F401  （sys.path 引导副作用：注册核心层步骤）

import bootstrap
from metamemory_framework.engine.graph import validate_graph

ROOT = Path(__file__).resolve().parent.parent
CONTRACT = json.loads((ROOT / "contracts" / "ingest_extract_deep.json").read_text(encoding="utf-8"))


class TestFlows(unittest.TestCase):
    def setUp(self):
        self.composition = bootstrap.build_composition()

    def test_all_flows_load_and_validate(self):
        self.assertIn("ingest", self.composition["flows"])
        results = bootstrap.validate_flows(self.composition)
        for name, result in results.items():
            self.assertTrue(result.get("ok"), f"{name}: {result.get('errors')}")

    def test_ingest_steps_registered(self):
        registry = self.composition["registry"]
        for step in ("ingest.normalize_event", "ingest.segment_units", "ingest.pre_gate",
                     "ingest.dedupe_route", "ingest.select_worthiness", "ingest.extract_deep",
                     "ingest.route_score", "ingest.organize_links",
                     "ingest.fastpath_store"):
            self.assertIsNotNone(registry.lookup(step), f"步骤未注册: {step}")

    def test_contract_sample_shape(self):
        # ADR-005：契约声明输入映射/拍平映射/断言/异常策略（含 abstain 一等处置）
        self.assertEqual(CONTRACT["name"], "ingest.extract_deep")
        self.assertIn("selected", CONTRACT["input_map"])
        self.assertEqual(CONTRACT["error_policy"]["Undecidable"]["action"], "abstain")
        self.assertEqual(CONTRACT["error_policy"]["DependencyError"]["action"], "retry")  # ADR-006 无降级
        self.assertEqual(CONTRACT["error_policy"]["*"]["action"], "dead_letter")

    def test_validate_contract_with_assertor(self):
        from metamemory_framework.contract.assertor import assert_fields

        ctx, tenant, m, evo = stubs()
        out = {}
        ok = assert_fields(input={"map": {"confidence": 0.9}, "judgment": True,
                                  "assertions": CONTRACT["assertions"]},
                           output=out, context=ctx, tenant=tenant, metrics=m, evolution=evo)
        self.assertTrue(ok)

    def test_registry_categories_complete(self):
        dump = self.composition["registry"].dump()
        self.assertIn("ingest.extract_deep@1", dump)
        self.assertEqual(dump["ingest.extract_deep@1"], "orchestration")


if __name__ == "__main__":
    unittest.main()
