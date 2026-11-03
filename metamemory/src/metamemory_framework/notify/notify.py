"""三层通知（T-026）：推进（advance）/进度（progress）/回调（webhook）。

- advance：事件先落 outbox（与业务写入同事务语义），扫描兜底投递——至少一次；
- progress：进程内 PubSub → SSE Hub（lite 单容器即观察即所得）；
- webhook：HMAC-SHA256 签名 POST + 指数退避重试队列，超限转死态（显式失败）。
executor 的 notify 钩子经 NotifyFanout 同时喂三路。infra 技术动作（豁免五段）。
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections import deque

from ..stores.sqlite_impl import _dump, sign_webhook

_NOW = lambda: time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())  # noqa: E731


class ProgressBus:
    """进度总线：事件即发即收 + 最近窗口回放（SSE 订阅者的补发数据源）。"""

    def __init__(self, pubsub=None, window: int = 2000) -> None:
        from ..stores.fallbacks import InMemoryPubSub

        self.pubsub = pubsub or InMemoryPubSub()
        self._recent: deque[dict] = deque(maxlen=window)

    def progress(self, event: dict) -> None:
        stamped = {**event, "at": time.time()}
        self._recent.append(stamped)
        self.pubsub.publish("task.progress", stamped)

    def subscribe(self, callback) -> None:
        self.pubsub.subscribe("task.progress", callback)

    def recent(self, task_id: str | None = None, limit: int = 100) -> list[dict]:
        events = [e for e in self._recent if task_id is None or e.get("task_id") == task_id]
        return list(reversed(events))[:limit]


class OutboxNotifier:
    """推进通知：事件落 outbox_events（写路径同库同事务），drain 扫描兜底投递。"""

    def __init__(self, db) -> None:
        self.db = db

    def progress(self, event: dict) -> None:
        self.db.execute("INSERT INTO outbox_events(channel, payload, created_at) VALUES(?,?,?)",
                        ("advance", _dump(event), _NOW()))

    def drain(self, handler, limit: int = 100) -> int:
        """扫描兜底：未发布事件逐条交 handler；handler 异常不标记（下次再投，至少一次）。"""
        rows = self.db.query(
            "SELECT id, payload FROM outbox_events WHERE channel='advance' AND published=0"
            " ORDER BY id LIMIT ?", (limit,))
        delivered = 0
        for row in rows:
            handler(json.loads(row["payload"]))
            self.db.execute("UPDATE outbox_events SET published=1 WHERE id=?", (row["id"],))
            delivered += 1
        return delivered

    def pending(self) -> int:
        rows = self.db.query(
            "SELECT COUNT(*) AS n FROM outbox_events WHERE channel='advance' AND published=0")
        return int(rows[0]["n"])


class WebhookDispatcher:
    """回调通知：签名 POST + 退避重试（at-least-once）；超限转 dead 并计数告警。"""

    def __init__(self, db, url: str = "", secret: str = "", max_attempts: int = 5,
                 timeout_seconds: int = 10) -> None:
        self.db = db
        self.url = url
        self.secret = secret
        self.max_attempts = max_attempts
        self.timeout_seconds = timeout_seconds
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # 内部直连

    def progress(self, event: dict) -> None:
        if not self.url:
            return  # 未配置回调端点 = 该路关闭（显式配置选择，非降级）
        payload = json.dumps(event, ensure_ascii=False).encode("utf-8")
        self.db.execute(
            "INSERT INTO webhook_deliveries(url, payload, signature, attempts, status, created_at)"
            " VALUES(?,?,?,0,'pending',?)", (self.url, payload.decode("utf-8"),
                                             sign_webhook(payload, self.secret), _NOW()))

    def deliver_pending(self, limit: int = 20) -> dict:
        """投递到期回调：成功 delivered；失败按 2^n 秒退避；超限 dead。"""
        now = time.time()
        rows = self.db.query(
            "SELECT id, url, payload, signature, attempts FROM webhook_deliveries"
            " WHERE status='pending' AND (next_retry_at IS NULL OR next_retry_at<=?)"
            " ORDER BY id LIMIT ?", (now, limit))
        stats = {"delivered": 0, "retry": 0, "dead": 0}
        for row in rows:
            body = row["payload"].encode("utf-8")
            req = urllib.request.Request(
                row["url"], data=body, method="POST",
                headers={"Content-Type": "application/json",
                         "X-MetaMemory-Signature": row["signature"]})
            attempts = int(row["attempts"]) + 1
            try:
                with self._opener.open(req, timeout=self.timeout_seconds) as resp:
                    resp.read()
                self.db.execute("UPDATE webhook_deliveries SET status='delivered', attempts=?"
                                " WHERE id=?", (attempts, row["id"]))
                stats["delivered"] += 1
            except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError):
                if attempts >= self.max_attempts:
                    self.db.execute("UPDATE webhook_deliveries SET status='dead', attempts=?"
                                    " WHERE id=?", (attempts, row["id"]))
                    stats["dead"] += 1
                else:
                    retry_at = time.strftime("%Y-%m-%dT%H:%M:%S+00:00",
                                             time.gmtime(now + 2 ** attempts))
                    self.db.execute("UPDATE webhook_deliveries SET attempts=?, next_retry_at=?"
                                    " WHERE id=?", (attempts, retry_at, row["id"]))
                    stats["retry"] += 1
        return stats


class NotifyFanout:
    """三路扇出：executor.notify 钩子的组合入口（任一路异常不反噬执行）。"""

    def __init__(self, *sinks) -> None:
        self.sinks = list(sinks)

    def progress(self, event: dict) -> None:
        for sink in self.sinks:
            try:
                sink.progress(event)
            except Exception:
                continue  # 通知故障不阻断管道；journal 仍是事实源（可重建）
