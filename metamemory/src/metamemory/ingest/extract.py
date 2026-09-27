"""提取/锚定/忠实校验/四态路由（I-018~I-021）。

硬约束：无引文不记忆（锚定失败→abstain 转复核，不静默）；四态=提取/折叠/留原文/
abstain（约束 15：判不了是合法结论，禁默认落置）；注入疑段落置信强制封顶。
"""
from __future__ import annotations

from metamemory_framework.container.policy import ContractViolation
from metamemory_framework.container.signature import unified_method


@unified_method("ingest.extract_deep", 1, "orchestration")
def extract_deep(input: dict, output: dict, context, metrics, evolution) -> bool:
    """I-018：深提取编排——强模型命题化（推理阶梯末档，DependencyError→降级链）。"""
    provider = context.deps.get("inference")
    if provider is None:
        evolution.fail("PROVIDER_MISSING", "推理双通道未装配")
        return False
    prompt = _build_prompt(input.get("selected") or input.get("segments") or [])
    response = provider.complete({"prompt": prompt, "prefer": "gateway"})
    atoms = response.get("atoms")
    if atoms is None:
        raise ContractViolation("extract_response_missing_atoms")
    episode_content = input.get("normalized", {}).get("content", "")
    worthiness_map = {s["text"]: s.get("worthiness", "mid") for s in input.get("selected") or []}
    candidates = [_enrich(atom, episode_content, worthiness_map) for atom in atoms]
    output["atom_candidates"] = candidates
    output["candidates"] = candidates
    metrics.counter("extract.candidates", len(candidates))
    return True


def _enrich(atom: dict, episode_content: str, worthiness_map: dict) -> dict:
    """逐原子补引文锚点与 NLI 判定（锚定/校验算法下沉，提取只做编排）。"""
    anchor: dict = {}
    anchor_span({"quote": atom.get("quote") or atom["text"], "episode_content": episode_content},
                anchor, None, _Quiet(), _QuietEvo())
    span_text = ""
    if anchor.get("anchored"):
        span_text = episode_content[anchor["span"][0]:anchor["span"][1]]
    verdict: dict = {}
    verify_entailment({"atom_text": atom["text"], "span_text": span_text},
                      verdict, None, _Quiet(), _QuietEvo())
    worthiness = worthiness_map.get(atom.get("quote") or atom["text"], "mid")
    return {**atom, **anchor, **verdict, "worthiness": worthiness}


@unified_method("ingest.extract_lite", 1, "data")
def extract_lite(input: dict, output: dict, context, metrics, evolution) -> bool:
    """降级路径（policy fallback 目标）：规则级命题化 + 同一套锚定/校验，质量降级语义不丢。"""
    episode_content = input.get("normalized", {}).get("content", "")
    worthiness_map = {s["text"]: s.get("worthiness", "mid") for s in input.get("selected") or []}
    atoms = [{"text": s["text"], "subject": "user", "predicate": "mentioned",
              "quote": s["text"], "entry_type": "fact"}
             for s in input.get("selected") or input.get("segments") or []]
    candidates = [_enrich(atom, episode_content, worthiness_map) for atom in atoms]
    output["atom_candidates"] = candidates
    output["candidates"] = candidates
    output["degraded"] = True
    return True


@unified_method("ingest.anchor_span", 1, "algorithm")
def anchor_span(input: dict, output: dict, context, metrics, evolution) -> bool:
    """I-019：引文锚定——原文 span 定位（纯计算）；找不到是业务结论非异常。"""
    quote = input["quote"]
    content = input["episode_content"]
    start = content.find(quote)
    if start < 0 and len(quote) > 12:
        start = content.find(quote[:12])
    if start < 0:
        output["anchored"] = False
        return True
    output.update({"anchored": True, "span": [start, start + len(quote)]})
    return True


@unified_method("ingest.verify_entailment", 1, "data")
def verify_entailment(input: dict, output: dict, context, metrics, evolution) -> bool:
    """I-020：NLI 忠实校验骨架（三态）：entail / contradict / neutral（边界带）。"""
    atom_text = input["atom_text"].lower()
    span_text = input["span_text"].lower()
    if any(neg in atom_text for neg in ("不是", "不再", "没有", "取消")):
        output.update({"verdict": "contradict", "confidence": 0.4})
        return True
    overlap = len(set(atom_text.split()) & set(span_text.split())) if " " in atom_text else int(input["atom_text"] in span_text)
    if overlap > 0:
        output.update({"verdict": "entail", "confidence": 0.9})
    else:
        output.update({"verdict": "neutral", "confidence": 0.3})
    return True


@unified_method("ingest.route_score", 1, "data")
def route_score(input: dict, output: dict, context, metrics, evolution) -> bool:
    """I-021：四态路由——提取 / 折叠 / 留原文 / abstain（挂复核带原因码）。"""
    routes, review = [], []
    for candidate in input["candidates"]:
        verdict = candidate.get("verdict", "neutral")
        anchored = candidate.get("anchored", False)
        if not anchored:
            route, reason = "abstain", "NO_ANCHOR"
        elif verdict == "contradict":
            route, reason = "abstain", "NLI_REJECT"
        elif verdict == "neutral":
            route, reason = "abstain", "NLI_BOUNDARY"
        elif candidate.get("worthiness") == "high" and verdict == "entail":
            route, reason = "extract", None
        elif candidate.get("worthiness") == "mid":
            route, reason = "fold", None
        else:
            route, reason = "keep_raw", None
        item = {**candidate, "route": route, "abstain_reason": reason}
        if route == "abstain":
            review.append({"reason": reason, "text": candidate.get("text")})
        routes.append(item)
    output["routes"] = routes
    if review:
        output["review_refs"] = review
    metrics.counter("score.extract", sum(r["route"] == "extract" for r in routes))
    metrics.counter("score.abstain", len(review))
    return True


def _build_prompt(segments: list) -> str:
    return "\n".join(segment.get("text", "") for segment in segments)


class _Quiet:
    """组合调用（提取编排锚定/校验）时的静默观测桩。"""

    def enter(self, *a, **k) -> None: ...

    def exit(self, *a, **k) -> None: ...

    def log(self, *a, **k) -> None: ...

    def counter(self, *a, **k) -> None: ...

    def gauge(self, *a, **k) -> None: ...

    def mark(self, *a, **k) -> None: ...


class _QuietEvo:
    ok = True
    reason_code = None
    reason = None
    retryable = False
    suggestions: list = []
    cause: list = []
