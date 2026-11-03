"""M1 框架全量验收（T-024/T-025/T-026）：任务日志/租约派发/调用缓存持久化/三层通知。"""
import json
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from common import make_tenant  # noqa: F401  sys.path 引导
from metamemory_framework.container.signature import unified_method
from metamemory_framework.container.types import Context, Evolution
from metamemory_framework.engine.executor import GraphExecutor
from metamemory_framework.engine.graph import pipeline_from_dict
from metamemory_framework.notify.notify import (
    NotifyFanout,
    OutboxNotifier,
    ProgressBus,
    WebhookDispatcher,
)
from metamemory_framework.observe.metrics import MetricsImpl
from metamemory_framework.queue.dispatch import SqliteTaskJournal
from metamemory_framework.stores.fallbacks import InMemoryPubSub
from metamemory_framework.stores.sqlite_impl import (
    FsObjectStore,
    SqliteCallCache,
    SqliteDb,
    SqliteFullTextStore,
    SqliteGraphStore,
    SqliteLockStore,
    SqliteTaskQueue,
    SqliteVectorStore,
    sign_webhook,
)


def _flow(step_b_name: str = "t.step_b") -> dict:
    return {
        "name": "resume", "version": 1, "entry": "a", "deadline_seconds": 60,
        "nodes": [
            {"node_id": "a", "node_type": "step", "step_name": "t.step_a", "edges": [{"to": "b"}]},
            {"node_id": "b", "node_type": "step", "step_name": step_b_name, "edges": [{"to": "done"}]},
            {"node_id": "done", "node_type": "step", "node_class": "terminal"},
        ],
    }


_CRASH_FIRST = {"value": True}


@unified_method("t.step_a", 1, "data")
def _step_a(input, output, context, tenant, metrics, evolution) -> bool:
    output["a"] = "done"
    return True


@unified_method("t.step_b", 1, "data")
def _step_b(input, output, context, tenant, metrics, evolution) -> bool:
    if _CRASH_FIRST["value"]:  # 模拟进程崩溃：BaseException 直穿 runner（非业务失败）
        _CRASH_FIRST["value"] = False
        raise KeyboardInterrupt("simulated crash")
    output["b"] = "done"
    return True


class TestTaskJournalAndResume(unittest.TestCase):
    def setUp(self):
        self.db = SqliteDb(":memory:")
        self.journal = SqliteTaskJournal(self.db)

    def _flows(self):
        flow = pipeline_from_dict(_flow())
        return {"resume": flow}

    def test_crash_resume_from_checkpoint(self):
        """崩溃第 k 轮续跑：节点完成即检查点；重启后从日志重建游标，任务零丢失。"""
        flows = self._flows()
        ex1 = GraphExecutor(flow_provider=lambda name, version: flows.get(name), journal=self.journal)
        task_id = ex1.submit(flows["resume"], params={"x": 1}, tenant_id="t1")
        with self.assertRaises(KeyboardInterrupt):  # 崩溃点：节点 b 执行中
            ex1.run_flow({"task_id": task_id}, {}, Context(), make_tenant(), MetricsImpl(), Evolution())
        # 重启：新执行器（同日志库）恢复
        ex2 = GraphExecutor(flow_provider=lambda name, version: flows.get(name), journal=self.journal)
        out: dict = {}
        ok = ex2.run_flow({"task_id": task_id}, out, Context(), make_tenant(), MetricsImpl(), Evolution())
        self.assertTrue(ok)
        self.assertEqual(out["task_status"], "succeeded")
        self.assertEqual(out["outputs"]["a"], {"a": "done"})          # a 不重跑（检查点回放）
        self.assertEqual(out["outputs"]["b"], {"b": "done"})          # b 续跑完成
        record = self.journal.load_task(task_id)
        self.assertEqual(record["status"], "succeeded")
        self.assertEqual(record["steps"]["a"]["status"], "succeeded")

    def test_lease_claim_zero_double_hold(self):
        """并发双 worker 同任务零双持：CAS 认领，持约者心跳续租。"""
        flows = self._flows()
        ex = GraphExecutor(flow_provider=lambda n, v: flows.get(n), journal=self.journal)
        task_id = ex.submit(flows["resume"], params={}, tenant_id="t1")
        self.assertTrue(self.journal.claim_task(task_id, owner="w1"))
        self.assertFalse(self.journal.claim_task(task_id, owner="w2"))  # 持约期内拒绝
        self.assertTrue(self.journal.renew_lease(task_id, owner="w1"))
        self.journal.release_lease(task_id, owner="w1", status="succeeded")
        self.assertTrue(self.journal.claim_task(task_id, owner="w2"))   # 释放后可认领

    def test_lease_expiry_reclaim(self):
        """租约判死：持约 worker 死亡（租约过期）后他人可重派。"""
        flows = self._flows()
        ex = GraphExecutor(flow_provider=lambda n, v: flows.get(n), journal=self.journal)
        task_id = ex.submit(flows["resume"], params={}, tenant_id="t1")
        self.assertTrue(self.journal.claim_task(task_id, owner="w1", lease_seconds=-1))  # 已过期
        self.assertTrue(self.journal.claim_task(task_id, owner="w2"))

    def test_incomplete_and_review_views(self):
        flows = self._flows()
        ex = GraphExecutor(flow_provider=lambda n, v: flows.get(n), journal=self.journal)
        task_id = ex.submit(flows["resume"], params={}, tenant_id="t1")
        self.assertIn(task_id, self.journal.incomplete_tasks())
        # 制造一个 abstained 步骤记录（人裁汇口视图）
        self.journal.record_step(task_id, "score", "step", "ingest.route_score", "abstained",
                                 {}, abstain_reason="NO_ANCHOR")
        views = self.journal.review_queue()
        self.assertEqual(views[0]["abstain_reason"], "NO_ANCHOR")


class TestSqliteTaskQueue(unittest.TestCase):
    def setUp(self):
        self.queue = SqliteTaskQueue(SqliteDb(":memory:"))

    def test_cas_claim_disjoint(self):
        """并发认领零双持：N 项两 worker 各取不重。"""
        for i in range(6):
            self.queue.enqueue("ingest", {"seq": i})
        got1 = self.queue.poll("ingest", limit=3, owner="w1")
        got2 = self.queue.poll("ingest", limit=3, owner="w2")
        self.assertEqual(len(got1), 3)
        self.assertEqual(len(got2), 3)
        ids1 = {item["_id"] for item in got1}
        ids2 = {item["_id"] for item in got2}
        self.assertFalse(ids1 & ids2)
        self.assertEqual(self.queue.depth("ingest"), 6)

    def test_lease_expiry_reclaim_and_dead(self):
        item_id = self.queue.enqueue("ingest", {"work": 1})
        claimed = self.queue.poll("ingest", owner="w1", lease_seconds=-1)  # 租约即过期
        self.assertEqual(len(claimed), 1)
        reclaimed = self.queue.poll("ingest", owner="w2")
        self.assertEqual(reclaimed[0]["_id"], item_id)
        self.assertEqual(reclaimed[0]["_attempts"], 2)  # 重派计数
        # nack 超限转死态（显式失败不静默）
        for _ in range(6):
            self.queue.nack(item_id, dead_after=5)
        rows = self.queue.db.query("SELECT status FROM queue_items WHERE id=?", (item_id,))
        self.assertEqual(rows[0]["status"], "dead")


class TestSqliteCallCachePersistence(unittest.TestCase):
    def test_crash_replay_same_key_hit(self):
        """T-025：崩溃重启（新实例同库）后同键命中——幂等重放不重复付费。"""
        db = SqliteDb(":memory:")
        cache1 = SqliteCallCache(db)
        payload_hash = cache1.input_hash({"prompt": "你好"})
        cache1.store("ingest.extract_deep", payload_hash, "v1",
                     {"atoms": [{"text": "原子"}]}, step="ingest.extract_deep",
                     provider="gateway", model="m1", cost_tokens=42)
        cache2 = SqliteCallCache(db)  # 模拟重启
        hit = cache2.lookup("ingest.extract_deep", payload_hash, "v1")
        self.assertIsNotNone(hit)
        self.assertEqual(hit["atoms"][0]["text"], "原子")
        self.assertEqual(cache2.hits, 1)
        self.assertIsNone(cache2.lookup("ingest.extract_deep", payload_hash, "v2"))  # prompt 版本隔离


class TestSlotStores(unittest.TestCase):
    def setUp(self):
        self.db = SqliteDb(":memory:")

    def test_fulltext_cjk(self):
        fts = SqliteFullTextStore(self.db)
        fts.index("entries", "mem_1", "客户王总的电话是一三八零零一三八零零零")
        fts.index("entries", "mem_2", "明天下午三点的会议")
        hits = fts.search("entries", "王总的电话", k=5)
        self.assertEqual(hits[0]["id"], "mem_1")

    def test_graph_walk_depth(self):
        graph = SqliteGraphStore(self.db)
        graph.upsert_edge("a", "b", "entity", {"w": 1})
        graph.upsert_edge("b", "c", "semantic", {"w": 2})
        walked = graph.walk("a", ["entity", "semantic"], depth=2)
        self.assertEqual([w["dst"] for w in walked], ["b", "c"])

    def test_vector_persist_and_search(self):
        vecs = SqliteVectorStore(self.db)
        vecs.upsert("entries", [("mem_1", [1.0, 0.0]), ("mem_2", [0.0, 1.0])])
        hits = vecs.search("entries", [0.9, 0.1], k=1)
        self.assertEqual(hits[0]["id"], "mem_1")
        reopened = SqliteVectorStore(self.db)  # 重启：从库重载向量
        self.assertEqual(reopened.search("entries", [0.9, 0.1], k=1)[0]["id"], "mem_1")

    def test_lock_exclusive_and_ttl(self):
        lock = SqliteLockStore(self.db)
        self.assertTrue(lock.acquire("ingest:ep_1", ttl_seconds=10))
        self.assertFalse(lock.acquire("ingest:ep_1", ttl_seconds=10))
        lock.release("ingest:ep_1")
        self.assertTrue(lock.acquire("ingest:ep_1", ttl_seconds=10))

    def test_object_store_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = FsObjectStore(tmp)
            store.put("raw", "ep_1/content.txt", "原文内容".encode("utf-8"))
            self.assertEqual(store.get("raw", "ep_1/content.txt"), "原文内容".encode("utf-8"))


class TestNotify(unittest.TestCase):
    def setUp(self):
        self.db = SqliteDb(":memory:")

    def test_progress_bus_recent_window(self):
        bus = ProgressBus(InMemoryPubSub())
        seen: list[dict] = []
        bus.subscribe(seen.append)
        bus.progress({"task_id": "t1", "status": "succeeded", "node_id": "a"})
        bus.progress({"task_id": "t2", "status": "running", "node_id": "b"})
        self.assertEqual(len(seen), 2)
        self.assertEqual([e["task_id"] for e in bus.recent(task_id="t1")], ["t1"])

    def test_outbox_drain_at_least_once(self):
        outbox = OutboxNotifier(self.db)
        outbox.progress({"task_id": "t1", "status": "succeeded"})
        outbox.progress({"task_id": "t1", "status": "succeeded"})
        delivered: list[dict] = []
        self.assertEqual(outbox.drain(delivered.append), 2)
        self.assertEqual(outbox.pending(), 0)
        self.assertEqual(outbox.drain(delivered.append), 0)  # 不重复投递（已标记）

    def test_webhook_signing_delivery_retry_dead(self):
        """回调三态：验签投递 / 失败退避重试 / 超限转死。"""
        received: list[dict] = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                body = self.rfile.read(int(self.headers["Content-Length"]))
                received.append({"sig": self.headers.get("X-MetaMemory-Signature"), "body": body})
                always_fail = b"t10" in body  # t10 场景：接收端恒 500（超限转死）
                self.send_response(500 if (always_fail or len(received) == 1) else 200)
                self.end_headers()

            def log_message(self, *args):
                pass

        httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{httpd.server_address[1]}/hook"
        try:
            hook = WebhookDispatcher(self.db, url=url, secret="s3cret", max_attempts=3)
            payload = {"task_id": "t9", "status": "succeeded"}
            hook.progress(payload)
            stats = hook.deliver_pending()
            self.assertEqual(stats["retry"], 1)  # 首次 500 → 退避
            self.assertEqual(len(received), 1)
            self.assertEqual(received[0]["sig"],
                             sign_webhook(json.dumps(payload, ensure_ascii=False).encode(), "s3cret"))
            self.db.execute("UPDATE webhook_deliveries SET next_retry_at=NULL")  # 到期
            stats = hook.deliver_pending()
            self.assertEqual(stats["delivered"], 1)
            self.assertEqual(len(received), 2)
            # 超限转死
            hook.progress({"task_id": "t10", "status": "failed"})
            for _ in range(4):
                self.db.execute("UPDATE webhook_deliveries SET next_retry_at=NULL")
                hook.deliver_pending()
            rows = self.db.query("SELECT status FROM webhook_deliveries ORDER BY id DESC LIMIT 1")
            self.assertEqual(rows[0]["status"], "dead")
        finally:
            httpd.shutdown()

    def test_fanout_isolation(self):
        class _Boom:
            def progress(self, event):
                raise RuntimeError("通知路故障")

        bus = ProgressBus(InMemoryPubSub())
        fanout = NotifyFanout(_Boom(), bus)
        fanout.progress({"task_id": "t1", "status": "succeeded"})  # 故障路不反噬
        self.assertEqual(len(bus.recent()), 1)


class TestMetricsSnapshot(unittest.TestCase):
    def test_snapshot_and_prometheus(self):
        m = MetricsImpl()
        m.enter("fw.step")
        m.exit("fw.step", ok=True)
        m.counter("fw_abstain_total", 2)
        m.gauge("queue_depth", 3)
        snapshot = m.snapshot()
        self.assertEqual(snapshot["counters"]["fw_abstain_total"], 2)
        self.assertIn("fw.step", snapshot["p95_duration_ms"])
        text = m.prometheus()
        self.assertIn("fw_abstain_total 2", text)
        self.assertIn("queue_depth 3", text)


if __name__ == "__main__":
    unittest.main()
