"""五段执行 runner（A9 / I-005）：①提取→②操作→③拍平→④异常策略。

注册的步骤函数本身即"操作"段（唯一业务代码）；提取/拍平由契约映射配置驱动，
失败路径全部交异常策略引擎（业务代码零容错样板，abstain 一等处置）。
"""
from __future__ import annotations

from ..contract.mapper import extract_by_mappings, flatten_by_mappings
from ..registry.registry import REGISTRY
from ..container.signature import unified_method
from .policy import (
    BusinessReject,
    ContractViolation,
    DependencyError,
    TransientError,
    Undecidable,
    resolve_policy,
)

_TYPED = (ContractViolation, TransientError, DependencyError, BusinessReject, Undecidable)
_CONTINUE = ("reply", "abstain", "review")


@unified_method("framework.run_step", 1, "orchestration")
def run_step(input: dict, output: dict, context, metrics, evolution) -> bool:
    """I-005：执行单个步骤节点（提取→操作→拍平→异常策略处置）。"""
    step = REGISTRY.lookup(input["step_name"], input.get("step_version"))
    if step is None:
        evolution.fail("STEP_NOT_REGISTERED", f"步骤未注册: {input['step_name']}")
        return False
    return _invoke_with_policy(step, input, output, context, metrics, evolution)


def _invoke_with_policy(step, input, output, context, metrics, evolution) -> bool:
    attempts = 0
    while True:
        attempts += 1
        try:
            values = _extract_values(input, output, context, metrics, evolution)
            ok = step(values, output, context, metrics, evolution)
            if ok:
                _flatten(values, input, output, context, metrics, evolution)
            return ok
        except _TYPED as typed:
            decision = _apply_policy(typed, input, output, metrics, evolution)
            if decision == "retry" and attempts <= _max_attempts(input):
                continue
            if decision == "fallback":
                return _run_fallback(input, output, context, metrics, evolution)
            return decision in _CONTINUE
        except Exception as unexpected:  # 兜底：死信 + 告警（禁默认值兜底）
            metrics.mark("dead_letter", {"step": step.step_name, "error": str(unexpected)})
            metrics.counter("fw_dead_letter_total")
            evolution.fail("UNEXPECTED_ERROR", str(unexpected))
            return False


def _extract_values(input, output, context, metrics, evolution) -> dict:
    contract = input.get("contract") or {}
    mappings = contract.get("input") or {}
    source = input.get("input_map") or {}
    if not mappings:
        return dict(source)
    extracted: dict = {}
    extract_by_mappings({"source": source, "mappings": mappings}, extracted, context, metrics, evolution)
    return extracted.get("values") or {}


def _flatten(values, input, output, context, metrics, evolution) -> None:
    mappings = (input.get("contract") or {}).get("output") or {}
    if mappings and isinstance(output, dict):
        flatten_by_mappings({"obj": output, "mappings": mappings}, output, context, metrics, evolution)


def _apply_policy(typed: Exception, input, output, metrics, evolution) -> str:
    probe = {"error_type": type(typed).__name__, "policy_config": input.get("policy_config") or {}}
    resolved: dict = {}
    resolve_policy(probe, resolved, None, metrics, evolution)
    action: dict = resolved["action"]
    name = action.get("action", "dead_letter")
    metrics.counter(f"fw_policy_{name}")
    if isinstance(typed, Undecidable):
        output["status"] = "abstained"
        output["abstain_reason"] = typed.reason_code
        output.setdefault("review_refs", []).append({"reason": typed.reason_code, "detail": typed.detail})
        metrics.counter("fw_abstain_total")
    elif isinstance(typed, BusinessReject):
        output.update({"status": "replied", "reply": typed.value})
    else:
        output["status"] = name
    return name


def _max_attempts(input) -> int:
    cfg = (input.get("policy_config") or {}).get("TransientError") or {}
    return int(cfg.get("max", DEFAULT_RETRY_MAX))


DEFAULT_RETRY_MAX = 3


def _run_fallback(input, output, context, metrics, evolution) -> bool:
    spec = ((input.get("policy_config") or {}).get("DependencyError") or {}).get("to", "")
    if not spec:
        evolution.fail("NO_FALLBACK", "DependencyError 未声明降级路径")
        return False
    name, _, version = spec.partition("@")
    step = REGISTRY.lookup(name, int(version) if version else None)
    if step is None:
        evolution.fail("FALLBACK_MISSING", f"降级步骤未注册: {spec}")
        return False
    output["status"] = "fallback"
    return step(input.get("input_map") or {}, output, context, metrics, evolution)
