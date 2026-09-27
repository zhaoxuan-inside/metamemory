"""流程图模型与编译期校验（A2/A12）：六型节点 + 条件边 + 图约束①-⑤。

约束⑤（ADR-003）：switch 的 default 与条件边全未激活的兜底路径不得直接进入
成功终态——必须显式指向 review/abstain/dead_letter 类节点。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..container.signature import unified_method

NODE_TYPES = ("step", "fork", "loop", "switch", "wait", "subflow")
REVIEW_CLASSES = ("review", "abstain", "dead_letter")


@dataclass
class Edge:
    to_node: str
    when: dict | None = None  # json-logic 条件；None = 无条件边
    is_default: bool = False


@dataclass
class Node:
    node_id: str
    node_type: str
    step_name: str | None = None
    step_version: int | None = None
    node_class: str = "work"  # work | review | abstain | dead_letter | terminal
    edges: list[Edge] = field(default_factory=list)
    config: dict[str, Any] = field(default_factory=dict)  # until/max_iterations/on/cases/join/timeout/subflow


@dataclass
class Pipeline:
    name: str
    version: int
    entry: str
    nodes: dict[str, Node]
    deadline_seconds: int | None = None


def pipeline_from_dict(data: dict) -> Pipeline:
    """解析流程定义（JSON/YAML 同构数据）；供 flows/ 与 CI 编译使用。"""
    nodes = {}
    for raw in data["nodes"]:
        edges = [Edge(e["to"], e.get("when"), bool(e.get("default"))) for e in raw.get("edges", [])]
        nodes[raw["node_id"]] = Node(
            node_id=raw["node_id"], node_type=raw["node_type"], step_name=raw.get("step_name"),
            step_version=raw.get("step_version"), node_class=raw.get("node_class", "work"),
            edges=edges, config=raw.get("config") or {},
        )
    return Pipeline(data["name"], int(data.get("version", 1)), data["entry"], nodes,
                    data.get("deadline_seconds"))


@unified_method("engine.detect_cycle", 1, "algorithm")
def detect_cycle(input: dict, output: dict, context, metrics, evolution) -> bool:
    """I-006：静态无环检测（循环仅经 loop 节点，DFS 三色标记，纯计算）。"""
    adjacency: dict[str, list[str]] = input["adjacency"]
    color: dict[str, int] = {n: 0 for n in adjacency}
    output["has_cycle"] = False

    def visit(node: str) -> bool:
        color[node] = 1
        for nxt in adjacency.get(node, []):
            if color.get(nxt) == 1 or (color.get(nxt) == 0 and visit(nxt)):
                return True
        color[node] = 2
        return False

    for node in adjacency:
        if color[node] == 0 and visit(node):
            output["has_cycle"] = True
            break
    return True


@unified_method("engine.check_no_silent_default", 1, "data")
def check_no_silent_default(input: dict, output: dict, context, metrics, evolution) -> bool:
    """I-007：default/兜底边目标必须为 review/abstain/dead_letter 类节点。"""
    nodes: dict[str, Node] = input["nodes"]
    violations: list[str] = []
    for node in nodes.values():
        for edge in node.edges:
            if not edge.is_default:
                continue
            target = nodes.get(edge.to_node)
            if target is None or target.node_class not in REVIEW_CLASSES:
                violations.append(f"{node.node_id}: default 兜底禁止直连 {edge.to_node}")
        if node.node_type == "switch" and not any(e.is_default for e in node.edges):
            violations.append(f"{node.node_id}: switch 缺显式 default（abstain 汇聚）")
    output["violations"] = violations
    return not violations


@unified_method("engine.validate_graph", 1, "orchestration")
def validate_graph(input: dict, output: dict, context, metrics, evolution) -> bool:
    """I-008：编译期校验汇总（图约束①-⑤），不过则拒绝发布。"""
    pipeline: Pipeline = input["pipeline"]
    errors: list[str] = []
    for node in pipeline.nodes.values():
        if node.node_type not in NODE_TYPES:
            errors.append(f"{node.node_id}: 非法节点类型 {node.node_type}")
        if node.node_type == "step" and node.node_class == "work" and not node.step_name:
            errors.append(f"{node.node_id}: step 节点缺 step_name")
        if node.node_type in ("wait",) and not node.config.get("timeout_seconds"):
            errors.append(f"{node.node_id}: wait 必有超时（防孤儿）")
        if not node.edges and node.node_class != "terminal":
            errors.append(f"{node.node_id}: 非终态节点缺出边（路径可达终态）")
    errors += _structural_errors(pipeline)
    output["errors"] = errors
    output["ok"] = not errors
    return not errors


def _structural_errors(pipeline: Pipeline) -> list[str]:
    adjacency = {nid: [e.to_node for e in n.edges] for nid, n in pipeline.nodes.items()}
    cycle_out: dict = {}
    detect_cycle({"adjacency": adjacency}, cycle_out, None, _Quiet(), _QuietEvo())
    errors: list[str] = []
    if cycle_out.get("has_cycle"):
        errors.append("静态图存在环（循环仅允许经 loop 节点）")
    probe: dict = {"nodes": pipeline.nodes}
    default_out: dict = {}
    check_no_silent_default(probe, default_out, None, _Quiet(), _QuietEvo())
    errors += [f"约束⑤: {v}" for v in default_out.get("violations", [])]
    if pipeline.entry not in pipeline.nodes:
        errors.append(f"入口节点不存在: {pipeline.entry}")
    if not pipeline.deadline_seconds:
        errors.append("缺任务级总闸 deadline_seconds")
    return errors


class _Quiet:
    """编译期内部调用的静默观测桩（不入指标）。"""

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
