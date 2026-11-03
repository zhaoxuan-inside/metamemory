"""提取/锚定/忠实校验/四态路由（I-018~I-021）。

硬约束：无引文不记忆（锚定失败→abstain 转复核，不静默）；四态=提取/折叠/留原文/
abstain（约束 15：判不了是合法结论，禁默认落置）；注入疑段落置信强制封顶。
"""
from __future__ import annotations

import json

from metamemory_framework.container.policy import ContractViolation
from metamemory_framework.container.signature import unified_method


@unified_method("ingest.extract_deep", 1, "orchestration")
def extract_deep(input: dict, output: dict, context, tenant, metrics, evolution) -> bool:
    """I-018：深提取编排——强模型命题化；调用缓存命中直接回填（A7 幂等重放不重复付费）。"""
    provider = context.deps.get("inference")
    if provider is None:
        evolution.fail("PROVIDER_MISSING", "推理双通道未装配")
        return False
    prompt = _build_prompt(input.get("selected") or input.get("segments") or [])
    response = _complete_cached(context, metrics, provider, prompt)
    ledger = context.deps.get("metering")
    if ledger is not None and response.get("tokens"):
        ledger.record(tenant.tenant_id, "llm.tokens", response["tokens"], source="ingest.extract_deep")
    atoms = response.get("atoms")
    if atoms is None:
        raise ContractViolation("extract_response_missing_atoms")
    episode_content = input.get("normalized", {}).get("content", "")
    worthiness_map = {s["text"]: s.get("worthiness", "mid") for s in input.get("selected") or []}
    candidates = [_enrich(atom, episode_content, worthiness_map, tenant) for atom in atoms]
    output["atom_candidates"] = candidates
    output["candidates"] = candidates
    metrics.counter("extract.candidates", len(candidates))
    return True


def _complete_cached(context, metrics, provider, prompt: str) -> dict:
    """推理调用经 CallCache：键 = (step, input_hash, prompt_version)；未装配缓存时直连。"""
    cache = context.deps.get("call_cache")
    if cache is None:
        return provider.complete({"prompt": prompt, "prefer": "gateway"})
    input_hash = cache.input_hash(prompt)
    cached = cache.lookup("ingest.extract_deep", input_hash, "v1")
    if cached is not None:
        metrics.counter("llm_cache_hit")
        return {k: v for k, v in cached.items() if k != "cached_at"}
    response = provider.complete({"prompt": prompt, "prefer": "gateway"})
    if "atoms" in response:
        cache.store("ingest.extract_deep", input_hash, "v1", response)
    return response


def _enrich(atom: dict, episode_content: str, worthiness_map: dict, tenant) -> dict:
    """逐原子补引文锚点与 NLI 判定（锚定/校验算法下沉，提取只做编排）。"""
    anchor: dict = {}
    anchor_span({"quote": atom.get("quote") or atom["text"], "episode_content": episode_content},
                anchor, None, tenant, _Quiet(), _QuietEvo())
    span_text = ""
    if anchor.get("anchored"):
        span_text = episode_content[anchor["span"][0]:anchor["span"][1]]
    verdict: dict = {}
    verify_entailment({"atom_text": atom["text"], "span_text": span_text},
                      verdict, None, tenant, _Quiet(), _QuietEvo())
    worthiness = worthiness_map.get(atom.get("quote") or atom["text"], "mid")
    return {**atom, **anchor, **verdict, "worthiness": worthiness}


@unified_method("ingest.anchor_span", 1, "algorithm")
def anchor_span(input: dict, output: dict, context, tenant, metrics, evolution) -> bool:
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
def verify_entailment(input: dict, output: dict, context, tenant, metrics, evolution) -> bool:
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
def route_score(input: dict, output: dict, context, tenant, metrics, evolution) -> bool:
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


def _parse_atoms_text(text: str | None) -> list | None:
    """网关文本 → 原子列表：约定 JSON 数组（命题/主谓宾/引文），解析失败交复核。"""
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, list) else None


def _build_prompt(segments: list) -> str:
    """深提取 prompt（v1）：命题化 + 强制引文 + JSON 数组输出（prompt 版本入幂等键）。"""
    lines = [segment.get("text", "") for segment in segments]
    return (
        "你是记忆提取引擎。把下面的每一段话拆成原子断言（一句可拆多条），"
        "只输出 JSON 数组，每个元素含字段：text（断言内容）、subject、predicate、"
        "quote（必须逐字来自原文，用于引文锚定）、entry_type（preference/fact/promise/event）。\n"
        "段落：\n" + "\n".join(lines)
    )


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
