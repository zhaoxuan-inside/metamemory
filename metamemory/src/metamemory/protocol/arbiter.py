"""VisionArbiter：视野裁决器，一切读取路径的唯一裁决点（公理 6，无特殊通道）。

三级：coarse_filter（租户+档位粗筛）→ fine_check（规则精裁）→ purpose_gate（公开层
目的闸，约束 16：发布者允许目的集 × 查询者声明目的，不匹配不放行）。
"""
from __future__ import annotations

from metamemory_framework.container.signature import unified_method

from .authorization import AuthzRegistry

_OPEN_LEVELS = ("L3D", "L4", "L5")


def _denied(candidate: dict, reason: str, denials: list) -> None:
    denials.append({"memory_id": candidate.get("memory_id"), "reason": reason})


@unified_method("arbiter.coarse_filter", 1, "data")
def coarse_filter(input: dict, output: dict, context, metrics, evolution) -> bool:
    """I-023：裁决粗筛——同租户全可达；跨租户仅开放档位或持有效授权。"""
    querier = input["querier"]
    authz: AuthzRegistry = input["authz"]
    allowed, denials = [], []
    for candidate in input["candidates"]:
        if candidate.get("tenant_id") == querier["tenant_id"]:
            allowed.append(candidate)
        elif candidate.get("visibility") in _OPEN_LEVELS and candidate.get("published"):
            allowed.append(candidate)
        elif authz.active_for(querier.get("actor", ""), candidate.get("memory_id", "")) is not None:
            allowed.append(candidate)
        else:
            _denied(candidate, "TENANT_SCOPE", denials)
    output.update({"allowed": allowed, "denials": denials})
    return True


@unified_method("arbiter.fine_check", 1, "data")
def fine_check(input: dict, output: dict, context, metrics, evolution) -> bool:
    """I-024：精裁——撤回/过期拦截、L1 出域拦截、注入疑产物强制 L1。"""
    querier = input["querier"]
    allowed, denials = [], []
    for candidate in input["candidates"]:
        level = "L1" if candidate.get("injected_suspect") else candidate.get("visibility", "L1")
        if level == "L1" and candidate.get("domain_id") != querier.get("domain_id"):
            _denied(candidate, "L1_DOMAIN_ONLY", denials)
            continue
        if candidate.get("revoked"):
            _denied(candidate, "AUTHZ_REVOKED", denials)
            continue
        allowed.append({**candidate, "visibility": level})
    output.update({"allowed": allowed, "denials": denials})
    return True


@unified_method("arbiter.purpose_gate", 1, "data")
def purpose_gate(input: dict, output: dict, context, metrics, evolution) -> bool:
    """I-025：公开层目的闸——目的字段是裁决谓词不是记录项（约束 16/G1）。"""
    declared = input.get("declared_purpose")
    allowed, denials = [], []
    for candidate in input["candidates"]:
        if candidate.get("visibility") not in ("L4", "L5"):
            allowed.append(candidate)
            continue
        if not declared:
            _denied(candidate, "PURPOSE_REQUIRED", denials)
        elif declared in candidate.get("published_purposes", []):
            allowed.append(candidate)
        else:
            _denied(candidate, "PURPOSE_MISMATCH", denials)
    output.update({"allowed": allowed, "denials": denials})
    return True


@unified_method("arbiter.decide", 1, "orchestration")
def arbiter_decide(input: dict, output: dict, context, metrics, evolution) -> bool:
    """I-026：唯一裁决入口（粗筛→精裁→目的闸）；放行结果写审计流。"""
    current: list = input["candidates"]
    denials: list = []
    stages = (
        (coarse_filter, {}),
        (fine_check, {}),
        (purpose_gate, {"declared_purpose": input.get("declared_purpose")}),
    )
    for stage, extra in stages:
        result: dict = {}
        stage({**input, "candidates": current, **extra}, result, context, metrics, evolution)
        current = result.get("allowed", [])
        denials += result.get("denials", [])
    metrics.counter("arbiter.allowed", len(current))
    metrics.counter("arbiter.denied", len(denials))
    trace = context.tenant.trace_id if context.tenant else "n/a"
    output.update({"allowed": current, "denials": denials, "audit_ref": f"audit:{trace}"})
    return True
