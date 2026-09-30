"""契约断言（A14-⑤ 软信号契约）：判定型步骤 output 必带 confidence。"""
from __future__ import annotations

from .mapper import TYPE_TABLE
from ..container.signature import unified_method


@unified_method("contract.assert_fields", 1, "data")
def assert_fields(input: dict, output: dict, context, tenant, metrics, evolution) -> bool:
    """I-003：字段断言（类型/范围/required）；判定型 map 强制 confidence ∈ [0,1]。"""
    target = input["map"]
    violations: list[str] = []
    for rule in input.get("assertions") or []:
        violations += _check_rule(target, rule)
    if input.get("judgment"):
        violations += _check_soft_signal(target)
    output["violations"] = violations
    output["ok"] = not violations
    return not violations


def _check_rule(target: dict, rule: dict) -> list[str]:
    field, name = target.get(rule["field"]), rule["field"]
    if field is None:
        return [f"{name}: required"] if rule.get("required") else []
    errors: list[str] = []
    expected = rule.get("type")
    types = TYPE_TABLE.get(expected or "")
    if types and not isinstance(field, types):
        errors.append(f"{name}: type!={expected}")
    if "min" in rule and field < rule["min"]:
        errors.append(f"{name}: <min")
    if "max" in rule and field > rule["max"]:
        errors.append(f"{name}: >max")
    return errors


def _check_soft_signal(target: dict) -> list[str]:
    confidence = target.get("confidence")
    if confidence is None or not isinstance(confidence, (int, float)) or not 0.0 <= confidence <= 1.0:
        return ["confidence: 判定型步骤必须携带 [0,1] 置信（软信号契约）"]
    return []
