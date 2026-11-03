"""表达组装（I-031）：Token 预算内择优装入，附加溯源引用。"""
from __future__ import annotations

from metamemory_framework.container.signature import unified_method


def _tokens(text: str) -> int:
    return max(1, len(text) // 2)


@unified_method("query.budget_pack", 1, "algorithm")
def budget_pack(input: dict, output: dict, context, tenant, metrics, evolution) -> bool:
    """预算内贪心装入：融合分×置信×强度 已在外层排序，此处只管预算。"""
    items: list[dict] = input["items"]
    budget = int(input.get("token_budget", 2048))
    packed, used = [], 0
    for item in items:
        cost = _tokens(item.get("content", ""))
        if used + cost > budget:
            continue
        packed.append({**item, "tokens": cost})
        used += cost
    output.update({"packed": packed, "budget_used": used, "dropped": len(items) - len(packed)})
    metrics.counter("express.packed", len(packed))
    return True
