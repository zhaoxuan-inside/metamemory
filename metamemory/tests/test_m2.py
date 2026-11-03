"""M2 理解闭环验收（S1 切片）：网关 NLI/embeddings/rerank 最佳路径组件。"""
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import bootstrap  # noqa: F401  sys.path 引导 + 步骤注册
from common import make_tenant, stubs  # noqa: F401
from metamemory.config import MetaMemoryConfig, StorageConfig
from metamemory_framework.adapters.inference import GatewayProvider
from metamemory_framework.container.policy import DependencyError
from metamemory_framework.container.types import Context, Evolution
from metamemory_framework.observe.metrics import MetricsImpl
from metamemory_framework.security.context import set_tenant


class _M2Provider:
    """测试替身：extract/nli/rerank/embed 全能力（评测工件，非系统路径）。"""

    embedding_model = "test-embed"
    rerank_model = "test-rerank"

    def complete(self, request: dict) -> dict:
        task = request.get("task")
        if task == "nli":
            return {"text": '{"verdict": "entail", "confidence": 0.95}', "tokens": 5}
        if task == "rerank":
            return {"text": '[{"id": "mem_000002", "score": 9}, {"id": "mem_000001", "score": 2}]',
                    "tokens": 10}
        return {"atoms": [{"text": "原子一", "subject": "user", "predicate": "mentioned",
                           "quote": "原子一", "entry_type": "fact"}], "tokens": 20}

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[1.0, 0.0] for _ in texts]


class TestRerank(unittest.TestCase):
    def test_rerank_applied_when_configured(self):
        """rerank_model 配置即启用：LLM 打分重排融合序（mem_000002 提到首位）。"""
        from metamemory.query.rerank import rerank

        provider = _M2Provider()
        ctx = Context(deps={"inference": provider, "store": bootstrap.build_composition(
            config=MetaMemoryConfig(storage=StorageConfig(data_dir=tempfile.mkdtemp())),
            db_path=":memory:")["store"]}, config={})
        _, tenant, m, evo = make_tenant(), make_tenant(), MetricsImpl(), Evolution()
        store = ctx.deps["store"]
        from metamemory.domain.models import MemoryEntry

        for i, text in enumerate(("候选一", "候选二"), 1):
            store.add_entry(MemoryEntry(entry_id=f"mem_{i:06d}", tenant_id="t", domain_id="d",
                                        subject="user", predicate="mentioned", object_value=text,
                                        entry_type="fact", valid_from="2026-01-01",
                                        anchor_episode="ep"))
        fused = [{"memory_id": "mem_000001", "fusion_score": 0.05},
                 {"memory_id": "mem_000002", "fusion_score": 0.04}]
        out: dict = {}
        self.assertTrue(rerank({"query": "查询", "fused": fused}, out, ctx, tenant, m, evo))
        self.assertTrue(out["reranked"])
        self.assertEqual(out["fused"][0]["memory_id"], "mem_000002")  # LLM 打分 9 分居首

    def test_rerank_absent_is_explicit_passthrough(self):
        """rerank_model 未配置 = RRF 直出（显式配置选择，reranked=false，非静默降级）。"""
        from metamemory.query.rerank import rerank

        ctx = Context(deps={"inference": object(), "store": None}, config={})
        _, tenant, m, evo = make_tenant(), make_tenant(), MetricsImpl(), Evolution()
        fused = [{"memory_id": "mem_000001", "fusion_score": 0.05}]
        out: dict = {}
        self.assertTrue(rerank({"query": "查询", "fused": fused}, out, ctx, tenant, m, evo))
        self.assertFalse(out["reranked"])
        self.assertEqual(out["fused"], fused)


class TestEmbedEntries(unittest.TestCase):
    def _composition(self):
        composition = bootstrap.build_composition(
            config=MetaMemoryConfig(storage=StorageConfig(data_dir=tempfile.mkdtemp())),
            db_path=":memory:")
        composition["inference"] = _M2Provider()
        return composition

    def test_embed_persists_vectors(self):
        """语义向量落库：条目 + 向量槽位双写（重启不丢，语义路可召回）。"""
        from metamemory.domain.persist import PersistentMemoryStore
        from metamemory.ingest.embedding import embed_entries

        composition = self._composition()
        store = composition["store"]
        from metamemory.domain.models import MemoryEntry

        entry = MemoryEntry(entry_id="mem_000001", tenant_id="t", domain_id="d", subject="user",
                            predicate="mentioned", object_value="向量化的记忆", entry_type="fact",
                            valid_from="2026-01-01", anchor_episode="ep")
        store.add_entry(entry)
        ctx = Context(deps={"store": store, "inference": composition["inference"],
                            "vector_store": composition["vector_store"],
                            "call_cache": composition["call_cache"]},
                      config=composition["config"])
        _, tenant, m, evo = make_tenant(), make_tenant(), MetricsImpl(), Evolution()
        out: dict = {}
        self.assertTrue(embed_entries({"entry_ids": [entry.entry_id]}, out, ctx, tenant, m, evo))
        self.assertEqual(out["embedded"], 1)
        self.assertIsNotNone(entry.embedding)
        hits = composition["vector_store"].search("entries", [1.0, 0.0], k=1)
        self.assertEqual(hits[0]["id"], entry.entry_id)
        # 重启：向量从库重载
        reopened = PersistentMemoryStore(composition["db"])
        self.assertIsNotNone(reopened.entries[entry.entry_id].embedding)

    def test_embed_unconfigured_is_explicit_skip(self):
        """embedding_model 未配置 = 语义路显式关闭（计数可见），不静默不伪造。"""
        from metamemory.ingest.embedding import embed_entries

        composition = self._composition()
        composition["inference"] = object()  # 无 embedding_model 属性
        ctx = Context(deps={"store": composition["store"], "inference": composition["inference"],
                            "vector_store": composition["vector_store"]},
                      config=composition["config"])
        _, tenant, m, evo = make_tenant(), make_tenant(), MetricsImpl(), Evolution()
        out: dict = {}
        self.assertTrue(embed_entries({"entry_ids": ["mem_x"]}, out, ctx, tenant, m, evo))
        self.assertEqual(out["embedded"], 0)
        self.assertEqual(m.counters.get("recall.semantic_unavailable"), 1)


class TestGatewayEmbeddings(unittest.TestCase):
    def test_embed_roundtrip_and_unconfigured(self):
        """网关 /embeddings 真实 HTTP 往返；未配置 embedding_model = DependencyError。"""
        captured: list[dict] = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                captured.append(body)
                resp = {"data": [{"index": i, "embedding": [float(i), 1.0]}
                                 for i in range(len(body["input"]))]}
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(resp).encode())

            def log_message(self, *args):
                pass

        httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            provider = GatewayProvider(base_url=f"http://127.0.0.1:{httpd.server_address[1]}",
                                       model="chat-m", embedding_model="embed-m")
            vecs = provider.embed(["你好", "世界"])
            self.assertEqual(vecs, [[0.0, 1.0], [1.0, 1.0]])  # index 排序回填
            self.assertEqual(captured[0]["model"], "embed-m")
        finally:
            httpd.shutdown()
        unconfigured = GatewayProvider(base_url="http://127.0.0.1:1", model="m")
        with self.assertRaises(DependencyError):
            unconfigured.embed(["文本"])


if __name__ == "__main__":
    unittest.main()
