"""任务 worker（受理 202 语义的执行侧）：认领→续租→驱动→释放循环。

lite 单容器 = 单 worker 线程；full 档 = 多进程 worker（PG SKIP LOCKED 派发，接口同构）。
崩溃恢复内建于 run_flow（journal 检查点续跑），worker 只负责认领与心跳。
"""
from __future__ import annotations

import threading
import time
import traceback


class TaskWorker(threading.Thread):
    """后台任务循环：incomplete_tasks → claim → run_flow（异常不致命，记死信继续）。"""

    def __init__(self, engine, journal, context_factory=None,
                 interval_seconds: float = 0.5) -> None:
        super().__init__(daemon=True, name="metamemory-task-worker")
        self.engine = engine
        self.journal = journal
        self.context_factory = context_factory  # (record) → (Context, TenantCtx)；完整依赖
        self.interval_seconds = interval_seconds
        self._stop = threading.Event()
        self.last_error: str | None = None

    def run(self) -> None:
        from ..container.types import Context, Evolution
        from ..observe.metrics import MetricsImpl
        from ..security.context import make_tenant

        while not self._stop.is_set():
            try:
                task_ids = self.journal.incomplete_tasks(limit=10)
                for task_id in task_ids:
                    if self._stop.is_set():
                        return
                    record = self.journal.load_task(task_id)
                    if record is None:
                        continue
                    if self.context_factory is not None:
                        context, tenant = self.context_factory(record)
                    else:
                        context = Context(config=record.get("config_snapshot") or {})
                        tenant = make_tenant(record.get("tenant_id") or "t_system",
                                             record.get("tenant_id") or "d_system", "worker")
                    ok = self.engine.run_flow({"task_id": task_id}, {}, context, tenant,
                                              MetricsImpl(), Evolution())
                    if not ok:
                        self.journal.touch_task(task_id)  # partial/waiting：下轮再续
            except Exception as exc:  # worker 循环不死（显式记录，观测面可见）
                self.last_error = f"{type(exc).__name__}: {exc}"
                traceback.print_exc()
            self._stop.wait(self.interval_seconds)

    def stop(self) -> None:
        self._stop.set()
