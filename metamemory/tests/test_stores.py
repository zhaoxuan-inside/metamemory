"""T-008/T-009：Store 槽位降级实现、cosine_top_k、推理双通道。"""
import unittest

from common import make_tenant, stubs

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from metamemory_framework.adapters.inference import DependencyError, GatewayProvider
from metamemory_framework.stores.fallbacks import (
    InMemoryCache,
    InMemoryLock,
    InMemoryPubSub,
    InMemoryQueue,
    InMemoryVector,
    cosine_top_k,
)


class TestInMemoryStores(unittest.TestCase):
    def test_cache_set_get_and_ttl(self):
        cache = InMemoryCache()
        cache.set("k", {"v": 1}, ttl_seconds=60)
        self.assertEqual(cache.get("k"), {"v": 1})
        cache.set("pref:x", 1)
        cache.invalidate_tag("pref:")
        self.assertIsNone(cache.get("pref:x"))

    def test_queue_fifo(self):
        queue = InMemoryQueue()
        queue.enqueue("q", {"id": "1"})
        queue.enqueue("q", {"id": "2"})
        self.assertEqual([i["id"] for i in queue.poll("q", 2)], ["1", "2"])
        self.assertEqual(queue.poll("q"), [])

    def test_lock_exclusive(self):
        lock = InMemoryLock()
        self.assertTrue(lock.acquire("k", 10))
        self.assertFalse(lock.acquire("k", 10))
        lock.release("k")
        self.assertTrue(lock.acquire("k", 10))

    def test_pubsub_delivers(self):
        bus, seen = InMemoryPubSub(), []
        bus.subscribe("ch", seen.append)
        bus.publish("ch", {"e": 1})
        self.assertEqual(seen, [{"e": 1}])

    def test_vector_search_uses_cosine(self):
        vector = InMemoryVector()
        vector.upsert("ns", [("a", [1.0, 0.0]), ("b", [0.0, 1.0]), ("c", [0.9, 0.1])])
        hits = vector.search("ns", [1.0, 0.0], k=2)
        self.assertEqual([h["id"] for h in hits], ["a", "c"])


class TestCosineTopK(unittest.TestCase):
    def test_ordering_and_k(self):
        ctx, tenant, m, evo = stubs()
        out = {}
        ok = cosine_top_k({"query_vec": [1.0, 0.0], "items": {"a": [1.0, 0.0], "b": [0.0, 1.0], "c": [0.5, 0.5]}, "k": 2},
                          out, ctx, tenant, m, evo)
        self.assertTrue(ok)
        self.assertEqual([h["id"] for h in out["hits"]], ["a", "c"])

    def test_zero_vector_safe(self):
        ctx, tenant, m, evo = stubs()
        out = {}
        self.assertTrue(cosine_top_k({"query_vec": [0.0, 0.0], "items": {"a": [0.0, 0.0]}, "k": 1},
                                     out, ctx, tenant, m, evo))
        self.assertEqual(len(out["hits"]), 1)


class _OpenAICompatHandler(BaseHTTPRequestHandler):
    """本地假 OpenAI 兼容端点：验证网关真实 HTTP 协议与解析。"""

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        payload = {"choices": [{"message": {"content": json.dumps(
            [{"text": body["messages"][0]["content"], "subject": "user", "predicate": "mentioned",
              "quote": body["messages"][0]["content"], "entry_type": "fact"}], ensure_ascii=False)}}],
            "usage": {"total_tokens": 42}}
        data = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):  # 静默
        pass


class TestGatewayProvider(unittest.TestCase):
    def _start(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), _OpenAICompatHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_address[1]}/v1"

    def test_unconfigured_is_explicit_error(self):
        with self.assertRaises(DependencyError):
            GatewayProvider().complete({"prompt": "x"})  # ADR-006：无降级，显式失败

    def test_real_http_roundtrip(self):
        provider = GatewayProvider(base_url=self._start(), model="test-model", api_key="sk-test")
        response = provider.complete({"prompt": "我一定要完成重要方案"})
        self.assertEqual(response["provider"], "gateway")
        self.assertEqual(response["model"], "test-model")
        self.assertEqual(response["tokens"], 42)
        atoms = json.loads(response["text"])
        self.assertEqual(atoms[0]["predicate"], "mentioned")

    def test_connection_refused_is_dependency_error(self):
        provider = GatewayProvider(base_url="http://127.0.0.1:1/v1", model="m", timeout_seconds=1)
        with self.assertRaises(DependencyError):
            provider.complete({"prompt": "x"})


if __name__ == "__main__":
    unittest.main()
