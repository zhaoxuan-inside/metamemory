"""管理控制台冒烟测试：真实 HTTP 服务（随机端口）验证 API 全链路。"""
import json
import unittest
import urllib.request
from threading import Thread

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # 内部直连，绕过环境代理

from common import CODE_ROOT  # noqa: F401  sys.path 引导

from metamemory_console.server import ConsoleApp, make_handler  # noqa: E402
from metamemory_framework.adapters.inference import load_provider  # noqa: E402


class _FakeGateway:
    """测试替身：模拟已配置的强模型网关（ADR-006 唯一路径的注入点）。"""

    def complete(self, request: dict) -> dict:
        body = request.get("prompt", "").split("段落：\n", 1)[-1]
        atoms = [{"text": line, "subject": "user", "predicate": "mentioned",
                  "quote": line, "entry_type": "fact"}
                 for line in body.split("\n") if line.strip()]
        return {"atoms": atoms, "provider": "fake"}


class TestConsole(unittest.TestCase):
    def setUp(self):
        self.app.composition["inference"] = _FakeGateway()  # 每用例隔离：还原测试替身

    @classmethod
    def setUpClass(cls):
        cls.app = ConsoleApp()
        cls.app.composition["inference"] = _FakeGateway()
        from http.server import ThreadingHTTPServer

        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cls.app))
        Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def _get(self, path):
        with _OPENER.open(self.base + path) as resp:
            body = resp.read()
            ctype = resp.headers["Content-Type"]
        if "json" in ctype:
            return json.loads(body)
        return body

    def _post(self, path, payload):
        req = urllib.request.Request(self.base + path, data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        with _OPENER.open(req) as resp:
            return json.loads(resp.read())

    def test_index_served(self):
        body = self._get("/")
        self.assertIn(b"MetaMemory", body)
        self.assertIn(b"\xe7\xae\xa1\xe7\x90\x86\xe6\x8e\xa7\xe5\x88\xb6\xe5\x8f\xb0", body)  # 管理控制台

    def test_state_shape(self):
        state = self._get("/api/state")
        for key in ("profile", "config", "steps", "flows", "store", "tasks", "review_queue", "metrics", "eval"):
            self.assertIn(key, state)
        self.assertEqual(state["profile"], "lite")
        self.assertGreater(len(state["steps"]), 20)
        self.assertIn("route_accuracy", state["eval"] or {})

    def test_add_memory_and_trace(self):
        result = self._post("/api/memories", {"content": "我一定要在周五前完成重要方案"})
        self.assertTrue(result["ok"])
        self.assertIn("task_id", result)
        detail = self._get("/api/tasks/" + result["task_id"])
        self.assertEqual(detail["status"], "succeeded")
        self.assertIn("organize", detail["outputs"])

    def test_search_after_seed(self):
        self._post("/api/memories", {"content": "记住客户王总电话 13800138000，重要联系人"})
        result = self._get("/api/search?q=user%20mentioned%2013800138000")
        self.assertTrue(result["ok"])
        self.assertGreaterEqual(len(result["items"]), 1)
        self.assertIn("13800138000", result["items"][0]["content"])

    def test_config_apply(self):
        applied = self._post("/api/config", {"recall": {"top_k": 25}})
        self.assertTrue(applied["applied"])
        self.assertEqual(self.app.config.recall.top_k, 25)
        state = self._get("/api/state")
        self.assertEqual(state["config"]["recall"]["top_k"], 25)

    def test_inference_reconfig(self):
        self._post("/api/config", {"inference": {"base_url": "http://127.0.0.1:9/v1", "model": "m1"}})
        self.assertTrue(self.app.state()["inference_configured"])
        self.assertIsInstance(self.app.composition["inference"], type(load_provider({"base_url": "x", "model": "m"})))


if __name__ == "__main__":
    unittest.main()
