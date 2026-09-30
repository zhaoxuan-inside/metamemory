"""lite 内嵌执行器（I-009）：就绪调度/条件推进/检查点（进程内版本）。

full 档替换为 PG SKIP LOCKED 派发 + 租约 + llm_calls 幂等缓存，接口签名不变。
节点状态机：ready → running → succeeded | failed | abstained | skipped。
"""
from __future__ import annotations

from typing import Any

from ..container.runner import run_step
from ..container.signature import unified_method
from .graph import Pipeline


class JsonLogicLite:
    """json-logic 求值子集：var/比较/逻辑门（条件边与 loop until 共用）。"""

    OPS = {
        "==": lambda a, b: a == b, "!=": lambda a, b: a != b,
        ">": lambda a, b: a > b, "<": lambda a, b: a < b,
        ">=": lambda a, b: a >= b, "<=": lambda a, b: a <= b,
    }

    def evaluate(self, rule: Any, data: dict) -> Any:
        if isinstance(rule, bool) or not isinstance(rule, dict):
            return rule
        if "var" in rule:
            return self._var(rule["var"], data)
        op, args = next(iter(rule.items()))
        values = [self.evaluate(a, data) for a in args] if isinstance(args, list) else [self.evaluate(args, data)]
        if op == "and":
            return all(values)
        if op == "or":
            return any(values)
        if op == "!":
            return not values[0]
        if op in self.OPS:
            return self.OPS[op](values[0], values[1])
        raise ValueError(f"不支持的条件算子: {op}")

    def _var(self, path: str, data: dict) -> Any:
        node: Any = data
        for key in str(path).lstrip("$. ").split("."):
            if isinstance(node, dict) and key:
                node = node.get(key)
            else:
                return None
        return node


class GraphExecutor:
    """图执行器：维护任务检查点（活跃节点集 = 任务游标），驱动节点到终态。"""

    def __init__(self, registry=None) -> None:
        from ..registry.registry import REGISTRY

        self.registry = registry or REGISTRY
        self.logic = JsonLogicLite()
        self.review_queue: list[dict] = []
        self.tasks: dict[str, dict] = {}

    def submit(self, pipeline: Pipeline, params: dict | None = None, task_id: str | None = None) -> str:
        task_id = task_id or f"tsk_{len(self.tasks) + 1:06d}"
        self.tasks[task_id] = {
            "pipeline": pipeline, "params": params or {}, "status": "pending",
            "cursor": {pipeline.entry: "ready"}, "outputs": {}, "steps": {},
        }
        return task_id

    @unified_method("engine.run_flow", 1, "orchestration")
    def run_flow(self, input: dict, output: dict, context, tenant, metrics, evolution) -> bool:
        """I-009：执行任务至终态（lite 内嵌同步版；检查点在 tasks[task_id]）。"""
        task = self.tasks.get(input["task_id"])
        if task is None:
            evolution.fail("TASK_NOT_FOUND", f"任务不存在: {input['task_id']}")
            return False
        task["status"] = "running"
        self._drive(task, context, tenant, metrics, evolution)
        output.update({"task_status": task["status"], "cursor": task["cursor"],
                       "outputs": task["outputs"], "steps": task["steps"]})
        return task["status"] in ("succeeded", "partial")

    def _drive(self, task: dict, context, tenant, metrics, evolution) -> None:
        pipeline: Pipeline = task["pipeline"]
        while True:
            ready = [nid for nid, state in task["cursor"].items() if state == "ready"]
            if not ready:
                waiting = any(state == "waiting" for state in task["cursor"].values())
                if waiting:
                    task["status"] = "waiting"
                elif self._terminal_reached(task, pipeline):
                    task["status"] = "succeeded"
                else:
                    task["status"] = "partial"
                return
            node_id = ready[0]
            task["cursor"][node_id] = "running"
            node = pipeline.nodes[node_id]
            self._execute_node(task, node, context, tenant, metrics, evolution)

    def _terminal_reached(self, task: dict, pipeline: Pipeline) -> bool:
        return any(
            pipeline.nodes[nid].node_class == "terminal" and state == "succeeded"
            for nid, state in task["cursor"].items()
        )

    def _execute_node(self, task: dict, node, context, tenant, metrics, evolution) -> None:
        handler = {
            "step": self._run_step_node, "switch": self._run_switch_node,
            "loop": self._run_loop_node, "fork": self._run_fork_node,
            "wait": self._run_wait_node, "subflow": self._run_subflow_node,
        }[node.node_type]
        handler(task, node, context, tenant, metrics, evolution)

    def _run_step_node(self, task: dict, node, context, tenant, metrics, evolution) -> bool:
        if node.step_name is None:  # review/terminal 汇聚节点（无业务逻辑）
            out = {"sink": node.node_id, "node_class": node.node_class}
            task["cursor"][node.node_id] = "succeeded"
            task["outputs"][node.node_id] = out
            return self._advance(task, node, out, context, tenant, metrics, evolution)
        upstream: dict = {}
        for node_out in task["outputs"].values():
            if isinstance(node_out, dict):
                upstream.update(node_out)
        spec = {"step_name": node.step_name, "step_version": node.step_version,
                "input_map": {**task["params"], **upstream}, "contract": node.config.get("contract"),
                "policy_config": node.config.get("error_policy")}
        out: dict = {}
        ok = run_step(spec, out, context, tenant, metrics, evolution)
        if out.get("status") == "abstained":  # 节点级弃权（Undecidable / 全量不可判）
            refs = out.get("review_refs") or [{"reason": out.get("abstain_reason")}]
            for ref in refs:
                self.review_queue.append({"task_node": node.node_id, **ref})
            metrics.counter("fw_abstain_total")
            task["cursor"][node.node_id] = "abstained"
            task["outputs"][node.node_id] = out
            return self._advance_on_abstain(task, node)
        for ref in out.get("review_refs") or []:
            self.review_queue.append({"task_node": node.node_id, **ref})
        task["cursor"][node.node_id] = "succeeded" if ok else "failed"
        task["steps"][node.node_id] = out
        task["outputs"][node.node_id] = out
        return self._advance(task, node, out, context, tenant, metrics, evolution)

    def _advance_on_abstain(self, task: dict, node) -> bool:
        """abstain 后继不激活常规边，只允许汇入 review/abstain/dead_letter 类节点。"""
        for edge in node.edges:
            target = task["pipeline"].nodes.get(edge.to_node)
            if target is not None and target.node_class in ("review", "abstain", "dead_letter"):
                task["cursor"][edge.to_node] = "ready"
        return True

    def _advance(self, task: dict, node, out: dict, context, tenant, metrics, evolution) -> bool:
        task["cursor"][node.node_id] = task["cursor"].get(node.node_id, "running")
        for edge in node.edges:
            if edge.is_default:
                continue
            if edge.when is None or self.logic.evaluate(edge.when, {"out": out, "params": task["params"]}):
                task["cursor"][edge.to_node] = "ready"
        if not self._activated_any(task, node):
            defaults = [e for e in node.edges if e.is_default]
            for edge in defaults:
                task["cursor"][edge.to_node] = "ready"
            if defaults:
                metrics.counter("fw_default_path_total")
        return True

    def _activated_any(self, task: dict, node) -> bool:
        return any(task["cursor"].get(e.to_node) == "ready" for e in node.edges if not e.is_default)

    def _run_switch_node(self, task: dict, node, context, tenant, metrics, evolution) -> bool:
        self._advance(task, node, task["outputs"].get(_upstream(node), {}), context, tenant, metrics, evolution)
        task["cursor"][node.node_id] = "succeeded"
        return True

    def _run_loop_node(self, task: dict, node, context, tenant, metrics, evolution) -> bool:
        max_iterations = int(node.config.get("max_iterations", 100))
        for iteration in range(max_iterations):
            body_out = self._run_body(task, node, context, tenant, metrics, evolution)
            if self.logic.evaluate(node.config["until"], {"out": body_out, "params": task["params"]}):
                break
            task["steps"].setdefault(node.node_id, {"iterations": 0})["iterations"] = iteration + 1
        task["cursor"][node.node_id] = "succeeded"
        self._advance(task, node, task["steps"].get(node.node_id, {}), context, tenant, metrics, evolution)
        return True

    def _run_body(self, task: dict, node, context, tenant, metrics, evolution) -> dict:
        body = node.config.get("body") or []
        out: dict = {}
        for step_name in body:
            spec = {"step_name": step_name, "input_map": task["params"]}
            run_step(spec, out, context, tenant, metrics, evolution)
        return out

    def _run_fork_node(self, task: dict, node, context, tenant, metrics, evolution) -> bool:
        items = node.config.get("from_items") or []
        join_policy = node.config.get("join", "all")
        results = [self._run_body_one(task, node, item, context, tenant, metrics, evolution) for item in items]
        ok = all(results) if join_policy == "all" else any(results)
        task["cursor"][node.node_id] = "succeeded" if ok else "partial"
        return self._advance(task, node, {"fork_ok": ok, "results": results}, context, tenant, metrics, evolution)

    def _run_body_one(self, task, node, item, context, tenant, metrics, evolution) -> bool:
        body = node.config.get("body") or []
        out: dict = {}
        for step_name in body:
            spec = {"step_name": step_name, "input_map": {**task["params"], "item": item}}
            if not run_step(spec, out, context, tenant, metrics, evolution):
                return False
        return True

    def _run_wait_node(self, task: dict, node, context, tenant, metrics, evolution) -> bool:
        timeout = node.config.get("timeout_seconds")
        if node.config.get("event_received"):
            task["cursor"][node.node_id] = "succeeded"
            return self._advance(task, node, {"wait": "received"}, context, tenant, metrics, evolution)
        task["cursor"][node.node_id] = "waiting"
        if timeout is None:
            evolution.fail("WAIT_NO_TIMEOUT", f"{node.node_id}: wait 必有超时（防孤儿）")
        return False

    def _run_subflow_node(self, task: dict, node, context, tenant, metrics, evolution) -> bool:
        sub: Pipeline = node.config["subflow"]
        sub_task = self.submit(sub, task["params"])
        result: dict = {}
        ok = self.run_flow({"task_id": sub_task}, result, context, tenant, metrics, evolution)
        task["cursor"][node.node_id] = "succeeded" if ok else "failed"
        task["outputs"][node.node_id] = result
        return self._advance(task, node, result, context, tenant, metrics, evolution)


def _upstream(node) -> str:
    return node.config.get("upstream", "")
