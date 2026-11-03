"""评测骨架：装载核心层组合根 → 执行管道 → 计算双指标（忠实率代理/召回率）。

三套件：golden（四态路由一致率）、recall（recall@5 + 拒答正确性）、injection（注入拦截率）。
存储完备率与检索召回率分开报告（设计原理 R3 双指标治理，不得互相伪装）。
本文件是测试工件（非业务方法），不入 _07（_00.conventions 豁免约定）。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

EVAL_ROOT = Path(__file__).resolve().parent
REPO = EVAL_ROOT.parent
for p in (str(REPO / "metamemory"), str(REPO / "metamemory" / "src"),
          str(REPO / "metamemory" / "tests"), str(EVAL_ROOT / "datasets")):
    if p not in sys.path:
        sys.path.insert(0, p)

from common import make_tenant  # noqa: E402  （tests/common 的 sys.path 副作用被复用）
import bootstrap  # noqa: E402
from metamemory_framework.container.types import Context, Evolution  # noqa: E402
from metamemory_framework.observe.metrics import MetricsImpl  # noqa: E402
from metamemory_framework.security.context import set_tenant  # noqa: E402
from metamemory.ingest.extract import extract_deep, route_score  # noqa: E402
from metamemory.ingest.fastpath import fastpath_store  # noqa: E402
from metamemory.ingest.steps import (  # noqa: E402
    normalize_event,
    pre_gate,
    segment_units,
    select_worthiness,
)

ROUTES = ("extract", "fold", "keep_raw", "abstain")


class RuleExtractionProvider:
    """评测替身（仅评测用，非系统路径——ADR-006）：解析 prompt 正文段落，逐句产出原子；
    NLI 按否定/取消词三态判定；embed 为确定性词袋哈希向量（五路评测齐备）。"""

    embedding_model = "eval-hash-bag-16"

    def complete(self, request: dict) -> dict:
        if request.get("task") == "nli":
            prompt = request.get("prompt", "")
            negate = any(w in prompt for w in ("不是", "不再", "没有", "取消", "忽略之前", "忽略以上"))
            verdict = "contradict" if negate else "entail"
            return {"text": '{"verdict": "%s", "confidence": 0.95}' % verdict,
                    "tokens": 5, "provider": "eval_fixture"}
        body = request.get("prompt", "").split("段落：\n", 1)[-1]
        atoms = [{"text": line, "subject": "user", "predicate": "mentioned",
                  "quote": line, "entry_type": "fact"}
                 for line in body.split("\n") if line.strip()]
        return {"atoms": atoms, "tokens": 20, "provider": "eval_fixture"}

    def embed(self, texts: list[str]) -> list[list[float]]:
        """确定性词袋向量（4096 维，md5 散列）：无碰撞 → 余弦≈词面重叠，不制造虚假相关。"""
        import hashlib
        import math

        vectors = []
        for text in texts:
            vec = [0.0] * 4096
            for token in text.lower().split():
                digest = hashlib.md5(token.encode()).digest()
                dim = int.from_bytes(digest[:4], "big") % 4096
                vec[dim] += 1.0
            norm = math.sqrt(sum(x * x for x in vec)) or 1.0
            vectors.append([x / norm for x in vec])
        return vectors


def load_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def new_context(composition: dict, inference=None):
    tenant = make_tenant()
    set_tenant(tenant)
    context = Context(
        deps={"store": composition["store"], "authz": composition["authz"],
              "engine": composition["engine"], "inference": inference or composition["inference"],
              "call_cache": composition["call_cache"], "flows": composition["flows"],
              "fts": composition.get("fts"), "vector_store": composition.get("vector_store"),
              "audit": composition.get("audit"), "metering": composition.get("metering"),
              "confirm_signer": composition.get("confirm_signer")},
        config=composition["config"],
    )
    return context, tenant, MetricsImpl(), Evolution()


def run_ingest(composition: dict, ctx_tenant_m_evo, content: str, source_uri: str) -> dict:
    """受理→快通道→引擎跑 ingest 流程图，返回 (ok, task_output)。"""
    context, tenant, metrics, evolution = ctx_tenant_m_evo
    add = composition["protocol"]["add"]
    out: dict = {}
    ok = add({"event": {"source_uri": source_uri, "actor": tenant.actor,
                        "occurred_at": "2026-09-27T10:00:00+08:00", "content": content}},
             out, context, tenant, metrics, evolution)
    if not ok:
        return {"ok": False, "reason": evolution.reason_code}
    task_out: dict = {}
    composition["engine"].run_flow({"task_id": out["task_id"]}, task_out, context, tenant, metrics, evolution)
    return {"ok": task_out.get("task_status") == "succeeded", "task": task_out}


def run_route_suite(composition: dict, records: list[dict]) -> dict:
    """四态路由一致率：标注是语义意图，骨架启发式与标注的分歧即真实差距。"""
    per_route = {r: {"total": 0, "hit": 0} for r in ROUTES}
    confusion: dict[str, dict[str, int]] = {r: {a: 0 for a in ROUTES} for r in ROUTES}
    abstain_reason_ok = 0
    misses: list[dict] = []
    for record in records:
        ctx = new_context(composition)
        context, tenant, metrics, evolution = ctx
        uri = f"eval://{record['id']}"
        fp: dict = {}
        fastpath_store({"event": {"source_uri": uri, "content": record["content"],
                                  "actor": tenant.actor, "occurred_at": "2026-09-27T10:00:00+08:00"}},
                       fp, context, tenant, metrics, evolution)
        norm: dict = {}
        normalize_event({"event": {"source_uri": uri, "content": record["content"],
                                   "actor": tenant.actor, "occurred_at": "t"}}, norm, context, tenant, metrics, evolution)
        seg: dict = {}
        segment_units({**norm, "episode_id": fp["episode_id"]}, seg, context, tenant, metrics, evolution)
        gate: dict = {}
        pre_gate({"segments": seg["segments"]}, gate, context, tenant, metrics, evolution)
        sel: dict = {}
        select_worthiness({"gated": gate["gated"]}, sel, context, tenant, metrics, evolution)
        ext: dict = {}
        extract_deep({"selected": sel["selected"], "normalized": norm["normalized"]}, ext, context, tenant, metrics, evolution)
        score: dict = {}
        route_score({"candidates": ext["candidates"]}, score, context, tenant, metrics, evolution)
        actual = score["routes"][0]["route"] if score.get("routes") else "keep_raw"
        expected = record["expected_route"]
        record["_faithful"] = bool(score.get("routes")) and all(
            r.get("anchored") and r.get("verdict") == "entail" for r in score.get("routes", []))
        per_route[expected]["total"] += 1
        confusion[expected][actual] += 1
        if actual == expected:
            per_route[expected]["hit"] += 1
        else:
            misses.append({"id": record["id"], "expected": expected, "actual": actual})
        if actual != "abstain" or (score.get("review_refs") and score["review_refs"][0].get("reason")):
            abstain_reason_ok += 1
    total = sum(v["total"] for v in per_route.values())
    hit = sum(v["hit"] for v in per_route.values())
    faithful = sum(1 for record in records if record.get("_faithful"))
    return {
        "suite": "golden_route", "total": total, "hit": hit,
        "faithfulness_proxy": round(faithful / total, 4) if total else 0.0,
        "route_accuracy": round(hit / total, 4) if total else 0.0,
        "per_route": {r: {"hit": v["hit"], "total": v["total"],
                          "acc": round(v["hit"] / v["total"], 3) if v["total"] else None}
                      for r, v in per_route.items()},
        "confusion": confusion,
        "abstain_reason_nonempty_rate": round(abstain_reason_ok / total, 4) if total else 0.0,
        "misses_sample": misses[:10],
    }


def run_recall_suite(composition: dict, records: list[dict]) -> dict:
    """检索召回：先播种（add→ingest），再逐题 search；拒答题要求 items 空且 unanswered 置位。"""
    ctx = new_context(composition)
    context, tenant, metrics, evolution = ctx
    seeds = {}
    for i, record in enumerate(records):
        if record.get("seed"):
            seeds[record["id"]] = run_ingest(composition, ctx, record["seed"], f"bench://{i:03d}")
    hits, no_answer_ok, no_answer_total, mrr_total, answered = 0, 0, 0, 0.0, 0
    failures: list[dict] = []
    search = composition["protocol"]["search"]
    for record in records:
        out: dict = {}
        ok = search({"query": record["query"], "purpose": record.get("purpose"),
                     "top_k": record.get("top_k", 5), "intent": record.get("intent", "fact")},
                    out, context, tenant, metrics, evolution)
        if not ok:
            failures.append({"id": record["id"], "error": evolution.reason_code})
            continue
        if record.get("expect_no_answer"):
            no_answer_total += 1
            if not out["items"] and out.get("unanswered"):
                no_answer_ok += 1
            else:
                failures.append({"id": record["id"], "got_items": len(out["items"])})
            continue
        answered += 1
        limit = record.get("top_k", 5)
        contents = [item.get("content", "") for item in out["items"]][:limit]
        rank = next((i + 1 for i, c in enumerate(contents) if record["expect_substr"] in c), None)
        if rank:
            hits += 1
            mrr_total += 1.0 / rank
        else:
            failures.append({"id": record["id"], "query": record["query"],
                             "top_contents": [c[:24] for c in contents[:3]]})
    seed_ok = sum(1 for v in seeds.values() if v["ok"])
    return {
        "suite": "recall_bench", "answered": answered, "no_answer": no_answer_total,
        "recall_at5": round(hits / answered, 4) if answered else 0.0,
        "mrr": round(mrr_total / answered, 4) if answered else 0.0,
        "no_answer_accuracy": round(no_answer_ok / no_answer_total, 4) if no_answer_total else None,
        "seed_ingest_ok": seed_ok,
        "storage_completeness_note": "写侧指标（原文层覆盖率）独立于本召回率报告（R3 双指标）",
        "failures_sample": failures[:10],
    }


def run_injection_suite(composition: dict, records: list[dict]) -> dict:
    """注入拦截率：pre_gate 安全门 fail-closed，疑似注入段落必须被标记。"""
    context, tenant, metrics, evolution = new_context(composition)
    blocked = 0
    missed: list[str] = []
    for record in records:
        out: dict = {}
        pre_gate({"segments": [{"segment_id": record["id"], "text": record["content"], "span": [0, 1]}]},
                 out, context, tenant, metrics, evolution)
        if out["gated"][0]["suspect"]:
            blocked += 1
        else:
            missed.append(record["id"])
    return {"suite": "injection_redteam", "total": len(records), "blocked": blocked,
            "block_rate": round(blocked / len(records), 4) if records else 0.0,
            "missed_ids": missed}


def run_all(datasets_dir: Path | None = None, data_dir: str | None = None,
            provider_mode: str = "eval") -> dict:
    datasets = datasets_dir or EVAL_ROOT / "datasets"
    # 三分离：评测是测试工件，数据面落独立临时目录（不污染运行档 var/，可重复播种）
    import tempfile

    from metamemory.config import MetaMemoryConfig, StorageConfig

    import os

    if provider_mode == "gateway":
        # 网关模式：真实推理网关（s1 门禁口径——忠实率/召回数字即系统真实能力）
        cfg = MetaMemoryConfig(storage=StorageConfig(data_dir=data_dir or tempfile.mkdtemp(prefix="metamemory-eval-")))
        cfg.inference.base_url = os.environ.get("METAMEMORY_INFERENCE_BASE_URL", cfg.inference.base_url)
        cfg.inference.api_key = os.environ.get("METAMEMORY_INFERENCE_API_KEY", cfg.inference.api_key)
        cfg.inference.model = os.environ.get("METAMEMORY_INFERENCE_MODEL", cfg.inference.model)
        cfg.inference.embedding_model = os.environ.get(
            "METAMEMORY_INFERENCE_EMBEDDING_MODEL", cfg.inference.embedding_model)
        composition = bootstrap.build_composition(config=cfg)
        if not (cfg.inference.base_url and cfg.inference.model):
            raise SystemExit("gateway 模式需配置推理网关（METAMEMORY_INFERENCE_BASE_URL/MODEL）")
    else:
        cfg = MetaMemoryConfig(storage=StorageConfig(data_dir=data_dir or tempfile.mkdtemp(prefix="metamemory-eval-")))
        composition = bootstrap.build_composition(config=cfg)
        composition["inference"] = RuleExtractionProvider()  # 评测运行统一使用替身（非系统路径）
    report = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "profile": composition["profile"],
        "golden": run_route_suite(composition, load_jsonl(datasets / "golden_v0.jsonl")),
        "recall": run_recall_suite(composition, load_jsonl(datasets / "recall_bench.jsonl")),
        "injection": run_injection_suite(composition, load_jsonl(datasets / "injection_redteam.jsonl")),
    }
    return report
