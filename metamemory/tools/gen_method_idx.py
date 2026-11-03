"""从注册表实况生成 _07_method_idx.json（防重索引与代码强一致）。"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # metamemory/ 代码根
REPO = ROOT.parent                                      # 仓库根
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

import bootstrap  # noqa: E402

# method 名 → （描述, reusable, tests, dependencies 提示由名称前缀推导）
META = {
    "extract_by_mappings": ("按输入映射从 source map 抽取字段（required/default/类型断言）", True, "test_mapper.py", []),
    "flatten_by_mappings": ("按输出映射把操作段产出拍平写回 output map", True, "test_mapper.py", []),
    "assert_fields": ("契约断言：类型/范围/required；判定型步骤强制 confidence∈[0,1]", True, "test_mapper.py", []),
    "resolve_policy": ("按错误类型解析处置动作（策略即数据；Undecidable 禁 reply/retry）", True, "test_container.py", []),
    "run_step": ("五段执行：提取→操作→拍平→异常策略处置（retry/fallback/reply/abstain/dead_letter）", False, "test_container.py", ["extract_by_mappings", "flatten_by_mappings", "resolve_policy"]),
    "detect_cycle": ("流程图静态无环检测（DFS 三色标记，纯计算）", True, "test_graph.py", []),
    "check_no_silent_default": ("约束⑤校验：default/兜底边禁止直连成功终态", True, "test_graph.py", []),
    "validate_graph": ("编译期校验汇总（图约束①-⑤），不过则拒绝发布", False, "test_graph.py", ["detect_cycle", "check_no_silent_default"]),
    "run_flow": ("lite 内嵌执行器：就绪调度/条件推进/检查点/abstain 只汇入 review 类节点", False, "test_graph.py", ["run_step"]),
    "cosine_top_k": ("余弦相似 top-k（纯计算，内存向量降级实现）", True, "test_stores.py", []),
    "fastpath_store": ("快通道：原文层同步落袋，source_uri 幂等", False, "test_ingest.py", []),
    "normalize_event": ("事件归一：空白清理/时间锚定/空内容拒收", False, "test_ingest.py", []),
    "segment_units": ("单元切分（写侧第一决策）：段落/轮次边界，span 保底可逆", False, "test_ingest.py", []),
    "pre_gate": ("安全/PII 门 fail-closed + 注入疑标记；输出带 confidence 软信号", False, "test_ingest.py", []),
    "dedupe_route": ("查重短路：同内容 hash 已入库路由 duplicate（省强模型调用）", False, "test_ingest.py", []),
    "select_worthiness": ("五维信号骨架+反面清单 → 值得度 high/mid/low/none", False, "test_ingest.py", []),
    "extract_deep": ("深提取编排：强模型命题化，逐原子补锚定与 NLI 判定", False, "test_ingest.py", ["anchor_span", "verify_entailment"]),
    "anchor_span": ("引文锚定：原文 span 定位（纯计算；找不到是业务结论）", True, "test_ingest.py", []),
    "verify_entailment": ("NLI 忠实校验骨架（entail/contradict/neutral 三态）", False, "test_ingest.py", []),
    "route_score": ("四态路由：提取/折叠/留原文/abstain（原因码必填，禁默认落置）", False, "test_ingest.py", []),
    "organize_links": ("组织：双时态落库+supersedes（窗口内）+情境/时序确定性建边", False, "test_ingest.py", []),
    "coarse_filter": ("裁决粗筛：租户+档位谓词，跨租户需开放档或有效授权", False, "test_arbiter.py", []),
    "fine_check": ("裁决精裁：L1 出域拦截/撤回拦截/注入疑产物强制 L1", False, "test_arbiter.py", []),
    "purpose_gate": ("公开层目的闸：允许目的集×声明目的，不匹配不放行（约束 16）", False, "test_arbiter.py", []),
    "arbiter_decide": ("VisionArbiter 唯一裁决入口：粗筛→精裁→目的闸，放行写审计", False, "test_arbiter.py", ["coarse_filter", "fine_check", "purpose_gate"]),
    "protocol_add": ("add 门面：配额→幂等→快通道→同事务入队（202 受理语义）", False, "test_protocol.py", ["fastpath_store"]),
    "protocol_search": ("search 门面：召回→RRF→裁决→预算组装（key 即查询者）", False, "test_protocol.py", ["query_recall", "rrf_fuse", "arbiter_decide", "budget_pack"]),
    "merge_candidates": ("合并五路候选并去重，保留路径与最好分", False, "test_query.py", []),
    "query_recall": ("五路并行召回编排（fulltext/semantic/graph/timeline/context）", False, "test_query.py", ["merge_candidates"]),
    "rrf_fuse": ("RRF 融合：1/(k+rank) 按排名求和，不依赖分数量纲", True, "test_query.py", []),
    "budget_pack": ("Token 预算内贪心装入，落选计数透出", True, "test_query.py", []),
}

MODULE_LOCATION = {
    "contract": "metamemory/src/metamemory_framework/contract/mapper.py",
    "assert_fields": "metamemory/src/metamemory_framework/contract/assertor.py",
    "container": "metamemory/src/metamemory_framework/container/runner.py",
    "resolve_policy": "metamemory/src/metamemory_framework/container/policy.py",
    "framework": "metamemory/src/metamemory_framework/container/runner.py",
    "engine": "metamemory/src/metamemory_framework/engine/",
    "stores": "metamemory/src/metamemory_framework/stores/fallbacks.py",
    "arbiter": "metamemory/src/metamemory/protocol/arbiter.py",
    "protocol": "metamemory/src/metamemory/protocol/facade.py",
    "ingest": "metamemory/src/metamemory/ingest/",
    "query": "metamemory/src/metamemory/query/",
}


def location_for(step_name: str, fn) -> str:
    domain = step_name.split(".")[0]
    base = MODULE_LOCATION.get(step_name.split(".")[1]) or MODULE_LOCATION.get(domain) or ""
    if base.endswith("/"):
        filenames = {
            "engine/detect_cycle": "graph.py", "engine/check_no_silent_default": "graph.py",
            "engine/validate_graph": "graph.py", "engine/run_flow": "executor.py",
            "ingest/extract_deep": "extract.py", "ingest/extract_lite": "extract.py",
            "ingest/anchor_span": "extract.py", "ingest/verify_entailment": "extract.py",
            "ingest/route_score": "extract.py",
            "query/rrf_fuse": "fusion.py", "query/budget_pack": "express.py",
            "query/merge_candidates": "recall.py", "query/query_recall": "recall.py",
            "query/recall": "recall.py",
        }
        base += filenames.get(f"{domain}/{step_name.split('.')[1]}", "steps.py")
    return base


def main() -> None:
    entries = sorted(bootstrap.build_composition()["registry"].entries())
    methods, seq = [], 0
    name_to_id: dict[str, str] = {}
    for _, _, _, func_name in entries:
        seq += 1
        name_to_id[func_name] = f"m-{seq:03d}"
    for step_name, version, category, func_name in entries:
        desc, reusable, tests, deps = META[func_name]
        domain = step_name.split(".")[0]
        methods.append({
            "id": name_to_id[func_name],
            "signature": f"boolean {func_name}(input, output, context, tenant, metrics, evolution)",
            "name": func_name,
            "category": category,
            "layer": "framework" if domain in ("contract", "container", "engine", "stores") else "core",
            "module": domain,
            "location": location_for(step_name, None),
            "description": desc,
            "reusable": reusable,
            "dependencies": [name_to_id[d] for d in deps if d in name_to_id],
            "tests": f"metamemory/tests/{tests}",
            "deprecated": False,
            "replaced_by": None,
        })
    stats = {"total": len(methods)}
    stats["by_category"] = {
        cat: sum(1 for m in methods if m["category"] == cat)
        for cat in ("algorithm", "orchestration", "data")
    }
    payload = {
        "schema_version": "1.0",
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "methods": methods,
        "stats": stats,
    }
    out = REPO / "_07_method_idx.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"_07 written: {stats}")


if __name__ == "__main__":
    main()
