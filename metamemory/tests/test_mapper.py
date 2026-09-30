"""T-003：数据契约映射器——提取/拍平/断言（ADR-005）。"""
import unittest

from common import make_tenant, stubs

from metamemory_framework.contract.assertor import assert_fields
from metamemory_framework.contract.mapper import (
    extract_by_mappings,
    flatten_by_mappings,
    resolve_path,
)
from metamemory_framework.container.policy import ContractViolation


class TestResolvePath(unittest.TestCase):
    def test_nested_path(self):
        found, value = resolve_path({"a": {"b": {"c": 7}}}, "$.a.b.c")
        self.assertTrue(found)
        self.assertEqual(value, 7)

    def test_wildcard_array(self):
        found, value = resolve_path({"items": [{"v": 1}, {"v": 2}]}, "$.items[*]")
        self.assertTrue(found)
        self.assertEqual(value, [{"v": 1}, {"v": 2}])

    def test_missing(self):
        found, value = resolve_path({"a": 1}, "$.x.y")
        self.assertFalse(found)
        self.assertIsNone(value)

    def test_invalid_path_rejected(self):
        with self.assertRaises(ContractViolation):
            resolve_path({}, "a.b")


class TestExtractFlatten(unittest.TestCase):
    def test_extract_with_default_and_type(self):
        ctx, tenant, m, evo = stubs()
        out = {}
        ok = extract_by_mappings(input={
            "source": {"payload": {"n": "3"}},
            "mappings": {
                "mode": {"path": "$.meta.mode", "default": "std"},
                "n": {"path": "$.payload.n", "required": True, "type": "str"},
            },
        }, output=out, context=ctx, tenant=tenant, metrics=m, evolution=evo)
        self.assertTrue(ok)
        self.assertEqual(out["values"], {"mode": "std", "n": "3"})

    def test_extract_missing_required_raises(self):
        ctx, tenant, m, evo = stubs()
        with self.assertRaises(ContractViolation):
            extract_by_mappings(input={"source": {}, "mappings": {"n": {"path": "$.n", "required": True}}},
                                output={}, context=ctx, tenant=tenant, metrics=m, evolution=evo)

    def test_flatten_writes_output_map(self):
        ctx, tenant, m, evo = stubs()
        out = {}
        ok = flatten_by_mappings(
            input={"obj": {"items": [1, 2], "status": "ok"},
                   "mappings": {"items": {"from": "items"}, "status": {"from": "status", "default": "ok"}}},
            output=out, context=ctx, tenant=tenant, metrics=m, evolution=evo)
        self.assertTrue(ok)
        self.assertEqual(out, {"items": [1, 2], "status": "ok"})


class TestAssertFields(unittest.TestCase):
    def test_judgment_step_requires_confidence(self):
        ctx, tenant, m, evo = stubs()
        out = {}
        ok = assert_fields(input={"map": {"confidence": 0.9}, "judgment": True, "assertions": []},
                           output=out, context=ctx, tenant=tenant, metrics=m, evolution=evo)
        self.assertTrue(ok)
        self.assertEqual(out["violations"], [])

    def test_missing_confidence_violation(self):
        ctx, tenant, m, evo = stubs()
        out = {}
        ok = assert_fields(input={"map": {"confidence": 2.0}, "judgment": True, "assertions": []},
                           output=out, context=ctx, tenant=tenant, metrics=m, evolution=evo)
        self.assertFalse(ok)
        self.assertTrue(any("confidence" in v for v in out["violations"]))

    def test_range_assertion(self):
        ctx, tenant, m, evo = stubs()
        out = {}
        ok = assert_fields(input={"map": {"score": 5},
                                  "assertions": [{"field": "score", "type": "number", "min": 0, "max": 1}]},
                           output=out, context=ctx, tenant=tenant, metrics=m, evolution=evo)
        self.assertFalse(ok)
        self.assertIn("score: >max", out["violations"])


if __name__ == "__main__":
    unittest.main()
