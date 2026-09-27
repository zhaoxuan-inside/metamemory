"""T-001/T-002/T-005：五段支撑类型、unified_method AOP、五段执行 runner。"""
import unittest

from common import stubs

from metamemory_framework.container.policy import (
    BusinessReject,
    ContractViolation,
    DependencyError,
    TransientError,
    Undecidable,
)
from metamemory_framework.container.runner import run_step
from metamemory_framework.container.signature import unified_method
from metamemory_framework.registry.registry import REGISTRY
from metamemory_framework.container.types import Context, Evolution
from metamemory_framework.observe.metrics import MetricsImpl


class TestTypes(unittest.TestCase):
    def test_evolution_fail_and_cause(self):
        evo = Evolution()
        evo.fail("QUOTA_EXCEEDED", "配额封顶", retryable=True)
        self.assertFalse(evo.ok)
        self.assertEqual(evo.reason_code, "QUOTA_EXCEEDED")
        self.assertTrue(evo.retryable)
        child = Evolution()
        child.fail("X", "y")
        evo.push_cause("step_a", child)
        self.assertEqual(evo.cause[0]["step"], "step_a")


class TestUnifiedMethodAOP(unittest.TestCase):
    def test_aop_records_enter_exit_and_logs(self):
        metrics, evo = MetricsImpl(), Evolution()

        @unified_method("test.echo", 1, "data")
        def echo(input, output, context, metrics, evolution) -> bool:
            output["v"] = input["v"] * 2
            metrics.counter("test.business_counter")  # 业务指标由方法写
            return True

        ctx, m = Context(), MetricsImpl()
        out = {}
        self.assertTrue(echo({"v": 21}, out, ctx, m, evo))
        self.assertEqual(out["v"], 42)
        self.assertEqual(m.counters.get("test.business_counter"), 1)
        self.assertAlmostEqual(m.duration_of("test.echo"), m.durations["test.echo"][-1])
        levels = [log["level"] for log in m.logs]
        self.assertIn("debug", levels)
        self.assertIn("info", levels)

    def test_aop_records_failure_and_reraises(self):
        @unified_method("test.boom", 1, "data")
        def boom(input, output, context, metrics, evolution) -> bool:
            raise DependencyError("downstream down")

        m, evo = MetricsImpl(), Evolution()
        with self.assertRaises(DependencyError):
            boom({}, {}, Context(), m, evo)
        self.assertEqual([log["level"] for log in m.logs][-1], "error")

    def test_same_key_reregistration_replaces(self):
        @unified_method("test.rereg", 1, "data")
        def first(input, output, context, metrics, evolution) -> bool:
            output["v"] = 1
            return True

        @unified_method("test.rereg", 1, "data")
        def second(input, output, context, metrics, evolution) -> bool:
            output["v"] = 2
            return True

        out = {}
        REGISTRY.lookup("test.rereg", 1)({"v": 0}, out, Context(), MetricsImpl(), Evolution())
        self.assertEqual(out["v"], 2)  # 新实现覆盖旧实现（旧记录保留演化线索）


class TestRunner(unittest.TestCase):
    def _spec(self, **overrides):
        spec = {"step_name": "test.target", "input_map": {"v": 1}, "contract": {},
                "policy_config": {}}
        spec.update(overrides)
        return spec

    def setUp(self):
        @unified_method("test.target", 1, "data")
        def target(input, output, context, metrics, evolution) -> bool:
            output["saw"] = input.get("v")
            return True

        self.target = target

    def test_happy_path(self):
        ctx, m, evo = stubs()
        out = {}
        self.assertTrue(run_step(self._spec(), out, ctx, m, evo))
        self.assertEqual(out.get("saw"), 1)

    def test_unregistered_step(self):
        ctx, m, evo = stubs()
        out = {}
        self.assertFalse(run_step(self._spec(step_name="test.missing"), out, ctx, m, evo))
        self.assertEqual(evo.reason_code, "STEP_NOT_REGISTERED")

    def test_business_reject_replies_and_continues(self):
        @unified_method("test.reject", 1, "data")
        def reject(input, output, context, metrics, evolution) -> bool:
            raise BusinessReject({"status": "skipped"})

        ctx, m, evo = stubs()
        out = {}
        self.assertTrue(run_step(self._spec(step_name="test.reject"), out, ctx, m, evo))
        self.assertEqual(out["status"], "replied")
        self.assertEqual(out["reply"], {"status": "skipped"})

    def test_undecidable_is_first_class_abstain(self):
        @unified_method("test.undecidable", 1, "data")
        def undecidable(input, output, context, metrics, evolution) -> bool:
            raise Undecidable("entity_merge_conflict", "两位候选实体")

        ctx, m, evo = stubs()
        out = {}
        self.assertTrue(run_step(self._spec(step_name="test.undecidable"), out, ctx, m, evo))
        self.assertEqual(out["status"], "abstained")
        self.assertEqual(out["abstain_reason"], "entity_merge_conflict")
        self.assertGreaterEqual(m.counters.get("fw_abstain_total", 0), 1)

    def test_undecidable_reason_code_must_not_be_empty(self):
        with self.assertRaises(ValueError):
            Undecidable("")

    def test_dependency_error_falls_back(self):
        @unified_method("test.gated", 1, "data")
        def gated(input, output, context, metrics, evolution) -> bool:
            raise DependencyError("gateway down")

        @unified_method("test.lite", 1, "data")
        def lite(input, output, context, metrics, evolution) -> bool:
            output["degraded"] = True
            return True

        ctx, m, evo = stubs()
        out = {}
        spec = self._spec(step_name="test.gated",
                          policy_config={"DependencyError": {"action": "fallback", "to": "test.lite@1"}})
        self.assertTrue(run_step(spec, out, ctx, m, evo))
        self.assertEqual(out["status"], "fallback")
        self.assertTrue(out["degraded"])

    def test_transient_error_retries_then_succeeds(self):
        calls = {"n": 0}

        @unified_method("test.flaky", 1, "data")
        def flaky(input, output, context, metrics, evolution) -> bool:
            calls["n"] += 1
            if calls["n"] < 3:
                raise TransientError("upstream timeout")
            return True

        ctx, m, evo = stubs()
        spec = self._spec(step_name="test.flaky",
                          policy_config={"TransientError": {"action": "retry", "max": 3}})
        self.assertTrue(run_step(spec, {}, ctx, m, evo))
        self.assertEqual(calls["n"], 3)

    def test_unexpected_error_dead_letters(self):
        @unified_method("test.explode", 1, "data")
        def explode(input, output, context, metrics, evolution) -> bool:
            raise RuntimeError("boom")

        ctx, m, evo = stubs()
        self.assertFalse(run_step(self._spec(step_name="test.explode"), {}, ctx, m, evo))
        self.assertEqual(evo.reason_code, "UNEXPECTED_ERROR")
        self.assertGreaterEqual(m.counters.get("fw_dead_letter_total", 0), 1)

    def test_contract_flatten_roundtrip(self):
        contract = {"input": {"v": {"path": "$.v", "required": True, "type": "int"}},
                    "output": {"doubled": {"from": "doubled"}}}

        @unified_method("test.contracted", 1, "data")
        def contracted(input, output, context, metrics, evolution) -> bool:
            output["doubled"] = input["v"] * 2
            return True

        ctx, m, evo = stubs()
        spec = self._spec(step_name="test.contracted", contract=contract, input_map={"v": 3})
        out = {}
        self.assertTrue(run_step(spec, out, ctx, m, evo))
        self.assertEqual(out.get("doubled"), 6)

    def test_registry_latest_lookup(self):
        self.assertIsNone(REGISTRY.lookup("test.nope"))
        self.assertIsNotNone(REGISTRY.lookup("test.target", 1))


if __name__ == "__main__":
    unittest.main()
