"""T-008/T-009：Store 槽位降级实现、cosine_top_k、推理双通道。"""
import unittest

from common import make_tenant, stubs

from metamemory_framework.adapters.inference import (
    DependencyError,
    EmbeddedProvider,
    GatewayProvider,
    InferenceRouter,
)
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


class TestInferenceRouter(unittest.TestCase):
    def test_gateway_unavailable_raises_dependency_error(self):
        provider = GatewayProvider()
        with self.assertRaises(DependencyError):
            provider.complete({"prompt": "x"})

    def test_router_falls_back_to_embedded(self):
        router = InferenceRouter(gateway=GatewayProvider())
        response = router.complete({"prompt": "hello", "prefer": "gateway"})
        self.assertEqual(response["routed"], "EmbeddedProvider")
        self.assertIn("hello", response["text"])

    def test_embedded_direct(self):
        response = EmbeddedProvider().complete({"prompt": "abc"})
        self.assertEqual(response["provider"], "embedded")


if __name__ == "__main__":
    unittest.main()
