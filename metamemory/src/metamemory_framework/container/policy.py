"""异常策略引擎：策略即数据，abstain 一等处置（A15 / ADR-003）。

错误类型 × 处置：review / retry / fallback / reply / abstain / dead_letter。
禁配（CI 拒收）：Undecidable 以 reply/retry 处置；abstain 原因码缺省。
"""
from __future__ import annotations

from typing import Any


class ContractViolation(Exception):
    """契约校验失败：转复核队列，不重试。"""


class TransientError(Exception):
    """瞬态错误（超时/5xx）：重试，优先于新任务准入。"""


class DependencyError(Exception):
    """外部依赖错误：降级到备用实现（经防腐层声明）。"""


class BusinessReject(Exception):
    """业务失败：回复指定内容，流程可继续。"""

    def __init__(self, value: Any) -> None:
        super().__init__("business_reject")
        self.value = value


class Undecidable(Exception):
    """不可判定（abstain）：一等合法结论——挂人裁，禁默认值兜底。"""

    def __init__(self, reason_code: str, detail: str = "") -> None:
        super().__init__(detail or reason_code)
        if not reason_code or reason_code.strip() in ("", "unknown"):
            raise ValueError("abstain 原因码必填且不得为空/unknown")
        self.reason_code = reason_code
        self.detail = detail


DEFAULT_POLICY: dict[str, dict[str, Any]] = {
    "ContractViolation": {"action": "review"},
    "TransientError": {"action": "retry", "max": 3, "backoff": "exp"},
    "DependencyError": {"action": "fallback"},
    "BusinessReject": {"action": "reply"},
    "Undecidable": {"action": "abstain"},
    "*": {"action": "dead_letter", "alert": True},
}


def resolve_policy(input: dict, output: dict, context, tenant, metrics, evolution) -> bool:
    """I-004：按错误类型解析处置动作（策略即数据，节点级可覆盖）。"""
    error_type = input["error_type"]
    policy = dict(DEFAULT_POLICY)
    policy.update(input.get("policy_config") or {})
    action = dict(policy.get(error_type) or policy["*"])
    if error_type == "Undecidable" and action.get("action") not in ("abstain", "dead_letter"):
        raise ValueError(f"禁配：Undecidable 不得以 {action.get('action')} 处置")
    output.update({"action": action, "error_type": error_type})
    return True
