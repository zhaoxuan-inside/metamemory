#!/usr/bin/env python3
"""评测入口：python3 run_eval.py [--suite all|golden|recall|injection] [--gate] [--out FILE]

--gate 时按 eval_config.json 阈值判定，不达标退出码 1（CI 门禁）。
阈值分两档：skeleton（骨架期，启发式提取）与 s1（理解闭环后收紧：忠实率代理 ≥0.90，
对齐核心层技术选型 §2.2 发布门禁）。当前配置在 skeleton 档。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

EVAL_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(EVAL_ROOT))

import harness  # noqa: E402

CHECKS = {
    "golden": lambda r: [("route_accuracy", r["golden"]["route_accuracy"]),
                         ("abstain_reason_nonempty_rate", r["golden"]["abstain_reason_nonempty_rate"])],
    "recall": lambda r: [("recall_at5", r["recall"]["recall_at5"]),
                         ("no_answer_accuracy", r["recall"]["no_answer_accuracy"])],
    "injection": lambda r: [("block_rate", r["injection"]["block_rate"])],
}


def main() -> int:
    parser = argparse.ArgumentParser(description="MetaMemory 骨架评测")
    parser.add_argument("--suite", default="all", choices=["all", "golden", "recall", "injection"])
    parser.add_argument("--gate", action="store_true", help="按阈值判定并返回退出码")
    parser.add_argument("--out", default=str(EVAL_ROOT / "reports" / "last_report.json"))
    args = parser.parse_args()

    thresholds = json.loads((EVAL_ROOT / "eval_config.json").read_text(encoding="utf-8"))
    active = thresholds[thresholds.get("active_profile", "skeleton")]

    report = harness.run_all()
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    g = report["golden"]
    print("== MetaMemory 骨架评测 ==")
    print(f"[golden]    四态路由一致率 {g['route_accuracy']:.2%} ({g['hit']}/{g['total']})  "
          f"abstain 原因码合规 {g['abstain_reason_nonempty_rate']:.0%}")
    for route, stats in g["per_route"].items():
        acc = f"{stats['acc']:.0%}" if stats["acc"] is not None else "-"
        print(f"            {route:<9} {stats['hit']}/{stats['total']} ({acc})")
    r = report["recall"]
    na = f"{r['no_answer_accuracy']:.0%}" if r["no_answer_accuracy"] is not None else "-"
    print(f"[recall]    recall@5 {r['recall_at5']:.2%}  MRR {r['mrr']:.3f}  拒答正确 {na}  "
          f"播种成功 {r['seed_ingest_ok']}")
    x = report["injection"]
    print(f"[injection] 拦截率 {x['block_rate']:.0%} ({x['blocked']}/{x['total']})")
    print(f"报告已写入 {args.out}")

    if not args.gate:
        return 0
    failures = []
    for suite in ([args.suite] if args.suite != "all" else ["golden", "recall", "injection"]):
        for metric, value in CHECKS[suite](report):
            limit = active.get(metric)
            if limit is None or value is None:
                continue
            if value < limit:
                failures.append(f"{suite}.{metric}={value} < {limit}")
    if failures:
        print("GATE FAIL:", "; ".join(failures))
        return 1
    print("GATE PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
