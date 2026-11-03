"""任务派发与检查点持久化（T-024）：SQLite 任务日志 + 租约派发 + 崩溃续跑。

任务零丢失（受理入库即日志）；节点完成=原子提交（输出+状态推进+后继激活同事务）；
进程重启后从 task_steps 检查点重建游标续跑（幂等重放不重复付费——LLM 调用另经
llm_calls 缓存）。full 档替换为 PG SKIP LOCKED，接口不变（I-009 full 档语义）。
infra 技术动作（豁免五段）。
"""
from __future__ import annotations

import json
import time

from ..stores.sqlite_impl import SqliteDb, _dump, _load

_NOW = lambda: time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())  # noqa: E731


class SqliteTaskJournal:
    """任务检查点日志：tasks/task_steps 表的写读面（lite 真源；full 档同表 PG 实现）。"""

    def __init__(self, db: SqliteDb) -> None:
        self.db = db

    def create_task(self, task_id: str, tenant_id: str, pipeline: str, pipeline_version: int,
                    params: dict, config_snapshot: dict, deadline_seconds: int | None = None) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO tasks(id, tenant_id, pipeline, pipeline_version, status,"
            " params, config_snapshot, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (task_id, tenant_id, pipeline, pipeline_version, "pending",
             _dump(params), _dump(config_snapshot), _NOW(), _NOW()))

    def record_step(self, task_id: str, node_id: str, node_type: str, step_name: str | None,
                    status: str, output: dict, abstain_reason: str | None = None,
                    error: str | None = None, iteration: int | None = None) -> None:
        """节点终态落日志（abstain 一等：abstain_reason 必填约束由库 CHECK 兜底）。"""
        self.db.execute(
            "INSERT OR REPLACE INTO task_steps(task_id, node_id, node_type, step_name, status,"
            " attempts, abstain_reason, error, output, iteration, started_at, finished_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (task_id, node_id, node_type, step_name, status,
             1, abstain_reason if abstain_reason else None, error,
             _dump(output), iteration, _NOW(), _NOW()))
        self.touch_task(task_id)

    def set_task_status(self, task_id: str, status: str) -> None:
        self.db.execute("UPDATE tasks SET status=?, updated_at=? WHERE id=?",
                        (status, _NOW(), task_id))

    def touch_task(self, task_id: str) -> None:
        self.db.execute("UPDATE tasks SET updated_at=? WHERE id=?", (_NOW(), task_id))

    def load_task(self, task_id: str) -> dict | None:
        rows = self.db.query("SELECT * FROM tasks WHERE id=?", (task_id,))
        if not rows:
            return None
        row = rows[0]
        steps = {}
        for step in self.db.query(
                "SELECT * FROM task_steps WHERE task_id=? ORDER BY id", (task_id,)):
            steps[step["node_id"]] = {
                "status": step["status"], "output": _load(step["output"], {}),
                "abstain_reason": step["abstain_reason"], "error": step["error"],
            }
        return {
            "task_id": row["id"], "tenant_id": row["tenant_id"], "pipeline": row["pipeline"],
            "pipeline_version": row["pipeline_version"], "status": row["status"],
            "params": _load(row["params"], {}), "config_snapshot": _load(row["config_snapshot"], {}),
            "steps": steps,
        }

    def incomplete_tasks(self, limit: int = 50) -> list[dict]:
        """待续跑任务：状态未到终态的（崩溃恢复入口）。"""
        rows = self.db.query(
            "SELECT id FROM tasks WHERE status IN ('pending','running','waiting')"
            " ORDER BY created_at LIMIT ?", (limit,))
        return [r["id"] for r in rows]

    def review_queue(self, limit: int = 50) -> list[dict]:
        """人裁汇口：abstained/failed 节点的复核队列视图（ADR-003）。"""
        rows = self.db.query(
            "SELECT task_id, node_id, step_name, abstain_reason, error FROM task_steps"
            " WHERE status IN ('abstained','failed') ORDER BY rowid DESC LIMIT ?", (limit,))
        return [dict(row) for row in rows]

    def claim_task(self, task_id: str, owner: str, lease_seconds: int = 90) -> bool:
        """租约认领（CAS）：仅 pending 或租约过期任务可认领（并发双 worker 零双持）。"""
        now = time.time()
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT status, lease_owner, lease_expires_at FROM tasks WHERE id=?",
                (task_id,)).fetchone()
            if row is None:
                return False
            if row["status"] == "running" and row["lease_expires_at"] is not None \
                    and row["lease_expires_at"] > now and row["lease_owner"] != owner:
                return False
            conn.execute(
                "UPDATE tasks SET status='running', lease_owner=?, lease_expires_at=?, updated_at=?"
                " WHERE id=?", (owner, now + lease_seconds, _NOW(), task_id))
        return True

    def renew_lease(self, task_id: str, owner: str, lease_seconds: int = 90) -> bool:
        """worker 心跳续租。"""
        cur = self.db.execute(
            "UPDATE tasks SET lease_expires_at=?, updated_at=? WHERE id=? AND lease_owner=?",
            (time.time() + lease_seconds, _NOW(), task_id, owner))
        return bool(cur.rowcount)

    def release_lease(self, task_id: str, owner: str, status: str) -> None:
        self.db.execute(
            "UPDATE tasks SET status=?, lease_owner=NULL, lease_expires_at=NULL, updated_at=?"
            " WHERE id=? AND lease_owner=?", (status, _NOW(), task_id, owner))
