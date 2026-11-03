"""lite 内嵌执行器（I-009）：就绪调度/条件推进/检查点（进程内版本）。

M1 全量：可选 TaskJournal 挂钩——节点完成=原子落日志（task_steps 检查点），
崩溃重启后从日志重建游标续跑（任务零丢失）；可选租约认领（并发 worker 零双持）；
可选 notify 钩子——节点终态/任务终态即发布进度事件（三层通知之源）。
full 档替换为 PG SKIP LOCKED 派发 + 租约 + llm_calls 幂等缓存，接口签名不变。
节点状态机：ready → running → succeeded | failed | abstained | skipped | waiting。
"""
from __future__ import annotations

import time
import uuid
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
    """图执行器：维护任务检查点（活跃节点集 = 任务游标），驱动节点到终态。

    journal（可选）：节点/任务终态写 task_steps/tasks（崩溃续跑与审计数据源）；
    notify（可选）：节点终态/任务终态回调（notify.ProgressBus 同构签名）；
    flow_provider（可选）：(name, version) → Pipeline，供崩溃后重建流程对象。
    """

    def __init__(self, registry=None, journal=None, notify=None, flow_provider=None) -> None:
        from ..registry.registry import REGISTRY

        self.registry = registry or REGISTRY
        self.logic = JsonLogicLite()
        self.review_queue: list[dict] = []
        self.tasks: dict[str, dict] = {}
        self.journal = journal
        self.notify = notify
        self.flow_provider = flow_provider

    # ---- 受理与恢复 ----

    def submit(self, pipeline: Pipeline, params: dict | None = None, task_id: str | None = None,
               tenant_id: str | None = None) -> str:
        task_id = task_id or f"tsk_{len(self.tasks) + 1:06d}"
        self.tasks[task_id] = {
            "task_id": task_id, "pipeline": pipeline, "params": params or {}, "status": "pending",
            "cursor": {pipeline.entry: "ready"}, "outputs": {}, "steps": {},
            "submitted_at": time.time(),
        }
        if self.journal is not None:
            self.journal.create_task(task_id, tenant_id or "", pipeline.name, pipeline.version,
                                     params or {}, {}, pipeline.deadline_seconds)
        self._emit(task_id, "accepted", pipeline.entry, {"pipeline": pipeline.name})
        return task_id

    def _restore(self, task_id: str) -> bool:
        """崩溃恢复：从 task_steps 检查点重建任务（游标=各节点终态 + 推进后继）。"""
        if self.journal is None or self.flow_provider is None:
            return False
        record = self.journal.load_task(task_id)
        if record is None:
            return False
        pipeline = self.flow_provider(record["pipeline"], record["pipeline_version"])
        if pipeline is None:
            return False
        cursor: dict[str, str] = {}
        outputs: dict[str, dict] = {}
        steps: dict[str, dict] = {}
        for node_id, step in record["steps"].items():
            if node_id not in pipeline.nodes:
                continue
            outputs[node_id] = step["output"] or {}
            steps[node_id] = step["output"] or {}
            cursor[node_id] = step["status"]
        for node_id, status in list(cursor.items()):
            node = pipeline.nodes[node_id]
            if status in ("succeeded", "failed", "partial"):
                self._advance_cursor(pipeline, cursor, node_id, outputs.get(node_id, {}),
                                     record["params"], include_defaults=False)
            elif status == "abstained":  # 弃权只汇入 review/abstain/dead_letter（ADR-003）
                for edge in node.edges:
                    target = pipeline.nodes.get(edge.to_node)
                    if target is not None and target.node_class in ("review", "abstain", "dead_letter"):
                        cursor.setdefault(edge.to_node, "ready")
        cursor.setdefault(pipeline.entry, "ready")
        self.tasks[task_id] = {
            "task_id": task_id, "pipeline": pipeline, "params": record["params"], "status": "pending",
            "cursor": cursor, "outputs": outputs, "steps": steps, "restored": True,
        }
        return True

    # ---- 主执行 ----

    @unified_method("engine.run_flow", 1, "orchestration")
    def run_flow(self, input: dict, output: dict, context, tenant, metrics, evolution) -> bool:
        """I-009：执行任务至终态；任务不在内存时经日志恢复（崩溃续跑）。"""
        task_id = input["task_id"]
        if task_id not in self.tasks and not self._restore(task_id):
            evolution.fail("TASK_NOT_FOUND", f"任务不存在: {task_id}")
            return False
        task = self.tasks[task_id]
        owner = f"worker-{uuid.uuid4().hex[:8]}"
        if self.journal is not None and not task.get("restored"):
            if not self.journal.claim_task(task_id, owner):  # 租约认领（并发零双持）
                evolution.fail("TASK_LEASED", f"任务被其他 worker 持有: {task_id}")
                return False
        task["status"] = "running"
        self._drive(task, context, tenant, metrics, evolution)
        if self.journal is not None:
            self.journal.set_task_status(task_id, task["status"])
            if task["status"] in ("succeeded", "partial", "failed"):
                self.journal.release_lease(task_id, owner, task["status"])
            else:
                self.journal.renew_lease(task_id, owner)  # waiting：保留租约待唤醒
        self._emit(task_id, task["status"], None, {"cursor": task["cursor"]})
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
                if metrics is not None and task["status"] in ("succeeded", "partial", "failed"):
                    metrics.mark("task_e2e_ms", round((time.time() - task.get("submitted_at", time.time())) * 1000, 1))
                    metrics.gauge("queue_depth", sum(1 for t in self.tasks.values() if t["status"] in ("pending", "running", "waiting")))
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

    # ---- 节点终态：游标推进 + 检查点 + 通知（单一出口）----

    def _finish_node(self, task: dict, node, status: str, out: dict,
                     abstain_reason: str | None = None, error: str | None = None,
                     metrics=None) -> bool:
        task["cursor"][node.node_id] = status
        task["outputs"][node.node_id] = out
        task["steps"][node.node_id] = out
        if self.journal is not None:
            self.journal.record_step(task["task_id"], node.node_id, node.node_type, node.step_name,
                                     status, out, abstain_reason, error)
        self._emit(task["task_id"], status, node.node_id, out)
        if status == "abstained":
            return self._advance_on_abstain(task, node)  # 弃权只汇入 review 类节点（ADR-003）
        if status in ("succeeded", "failed", "partial"):
            return self._advance(task, node, out, metrics=metrics)
        return True  # waiting：不推进后继

    def _advance(self, task: dict, node, out: dict, metrics=None) -> bool:
        self._advance_cursor(task["pipeline"], task["cursor"], node.node_id, out, task["params"],
                             include_defaults=True)
        if metrics is not None and not self._activated_any(task, node):
            defaults = [e for e in node.edges if e.is_default]
            if defaults:
                metrics.counter("fw_default_path_total")
        return True

    def _advance_on_abstain(self, task: dict, node) -> bool:
        """abstain 后继不激活常规边，只允许汇入 review/abstain/dead_letter 类节点。"""
        for edge in node.edges:
            target = task["pipeline"].nodes.get(edge.to_node)
            if target is not None and target.node_class in ("review", "abstain", "dead_letter"):
                task["cursor"][edge.to_node] = "ready"
        return True

    def _advance_cursor(self, pipeline: Pipeline, cursor: dict, node_id: str, out: dict,
                        params: dict, include_defaults: bool) -> None:
        """按条件边推进后继（succeeded/failed 同语义；default 仅兜底激活）。"""
        node = pipeline.nodes[node_id]
        for edge in node.edges:
            if edge.is_default:
                continue
            if edge.when is None or self.logic.evaluate(edge.when, {"out": out, "params": params}):
                cursor.setdefault(edge.to_node, "ready")
        if include_defaults and not any(
                cursor.get(e.to_node) == "ready" for e in node.edges if not e.is_default):
            for edge in node.edges:
                if edge.is_default:
                    cursor.setdefault(edge.to_node, "ready")

    def _run_step_node(self, task: dict, node, context, tenant, metrics, evolution) -> bool:
        if node.step_name is None:  # review/terminal 汇聚节点（无业务逻辑）
            out = {"sink": node.node_id, "node_class": node.node_class}
            return self._finish_node(task, node, "succeeded", out)
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
            if metrics is not None:
                metrics.counter("fw_abstain_total")
            return self._finish_node(task, node, "abstained", out,
                                     abstain_reason=out.get("abstain_reason") or "UNSPECIFIED",
                                     metrics=metrics)
        for ref in out.get("review_refs") or []:
            self.review_queue.append({"task_node": node.node_id, **ref})
        return self._finish_node(task, node, "succeeded" if ok else "failed", out,
                                 error=None if ok else (evolution.reason_code if evolution else None),
                                 metrics=metrics)

    def _activated_any(self, task: dict, node) -> bool:
        return any(task["cursor"].get(e.to_node) == "ready" for e in node.edges if not e.is_default)

    def _run_switch_node(self, task: dict, node, context, tenant, metrics, evolution) -> bool:
        out = task["outputs"].get(_upstream(node), {})
        self._advance(task, node, out, metrics=metrics)
        task["cursor"][node.node_id] = "succeeded"
        if self.journal is not None:
            self.journal.record_step(task["task_id"], node.node_id, node.node_type,
                                     None, "succeeded", out)
        self._emit(task["task_id"], "succeeded", node.node_id, out)
        return True

    def _run_loop_node(self, task: dict, node, context, tenant, metrics, evolution) -> bool:
        max_iterations = int(node.config.get("max_iterations", 100))
        for iteration in range(max_iterations):
            body_out = self._run_body(task, node, context, tenant, metrics, evolution)
            if self.logic.evaluate(node.config["until"], {"out": body_out, "params": task["params"]}):
                break
            task["steps"].setdefault(node.node_id, {"iterations": 0})["iterations"] = iteration + 1
        out = task["steps"].get(node.node_id, {})
        return self._finish_node(task, node, "succeeded", out)

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
        return self._finish_node(task, node, "succeeded" if ok else "partial",
                                 {"fork_ok": ok, "results": results})

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
            return self._finish_node(task, node, "succeeded", {"wait": "received"})
        out: dict = {}
        if timeout is None:
            evolution.fail("WAIT_NO_TIMEOUT", f"{node.node_id}: wait 必有超时（防孤儿）")
            return self._finish_node(task, node, "failed", out, error="WAIT_NO_TIMEOUT")
        # waiting：挂起待事件，不推进后继（唤醒后从本节点续跑）；检查点照记
        task["cursor"][node.node_id] = "waiting"
        if self.journal is not None:
            self.journal.record_step(task["task_id"], node.node_id, node.node_type,
                                     node.step_name, "waiting", out)
        self._emit(task["task_id"], "waiting", node.node_id, out)
        return False

    def _run_subflow_node(self, task: dict, node, context, tenant, metrics, evolution) -> bool:
        sub: Pipeline = node.config["subflow"]
        sub_task = self.submit(sub, task["params"])
        result: dict = {}
        ok = self.run_flow({"task_id": sub_task}, result, context, tenant, metrics, evolution)
        return self._finish_node(task, node, "succeeded" if ok else "failed", result)

    # ---- 通知 ----

    def _emit(self, task_id: str, status: str, node_id: str | None, payload: dict) -> None:
        if self.notify is None:
            return
        try:
            self.notify.progress({"task_id": task_id, "status": status,
                                  "node_id": node_id, **({"payload": payload} if payload else {})})
        except Exception:  # 通知失败不反噬执行（进度丢失可由 journal 重建）
            pass


def _upstream(node) -> str:
    return node.config.get("upstream", "")
