"""五路融合排序：RRF 初融（纯计算，可跨项目复用）。

full 档在 RRF 后接 reranker 精排（可降档，C-G：缺席/超时退 RRF 直出）再乘置信×强度。
"""
from __future__ import annotations

from metamemory_framework.container.signature import unified_method

DEFAULT_RRF_K = 60


@unified_method("query.rrf_fuse", 1, "algorithm")
def rrf_fuse(input: dict, output: dict, context, metrics, evolution) -> bool:
    """I-030：RRF 融合——按排名 1/(k+rank) 求和，不依赖分数量纲。"""
    ranked_lists: dict[str, list[str]] = input["ranked_lists"]
    k = int(input.get("rrf_k", DEFAULT_RRF_K))
    scores: dict[str, float] = {}
    for path, ids in ranked_lists.items():
        for rank, doc_id in enumerate(ids):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank + 1)
    fused = sorted(scores.items(), key=lambda pair: pair[1], reverse=True)
    output["fused"] = [{"memory_id": doc_id, "fusion_score": round(score, 6)} for doc_id, score in fused]
    output["paths"] = sorted(ranked_lists)
    return True
