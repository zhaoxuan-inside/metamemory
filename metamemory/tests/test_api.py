"""M4 开发者面 + API 全量验收：REST 路由/认证/grep NDJSON/verify/SSE 数据源/MCP 工具面。"""
import json
import tempfile
import unittest

import bootstrap  # noqa: F401  sys.path 引导 + 步骤注册
from common import make_tenant  # noqa: F401
from metamemory.config import MetaMemoryConfig, StorageConfig


class _FakeProvider:
    """测试替身：extract/nli 全能力（评测工件，非系统路径）。"""

    def complete(self, request: dict) -> dict:
        if request.get("task") == "nli":
            negate = any(w in request.get("prompt", "") for w in ("不是", "不再", "没有", "取消"))
            return {"text": '{"verdict": "%s", "confidence": 0.9}' %
                    ("contradict" if negate else "entail"), "tokens": 5}
        body = request.get("prompt", "").split("段落：\n", 1)[-1]
        return {"atoms": [{"text": line, "subject": "user", "predicate": "mentioned",
                           "quote": line, "entry_type": "fact"}
                          for line in body.split("\n") if line.strip()], "tokens": 20}


class ApiTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from api.app import create_app
        from fastapi.testclient import TestClient

        cls.composition = bootstrap.build_composition(
            config=MetaMemoryConfig(storage=StorageConfig(data_dir=tempfile.mkdtemp())),
            db_path=":memory:")
        cls.composition["inference"] = _FakeProvider()
        cls.client = TestClient(create_app(cls.composition))

    def _seed(self, content: str = "我一定要在周五前交付重要方案，工单号 12345", uri: str = "conv://api1") -> str:
        resp = self.client.post("/v1/memories", json={
            "source_uri": uri, "content": content, "actor": "alice",
            "occurred_at": "2026-09-27T10:00:00+08:00"})
        self.assertEqual(resp.status_code, 202, resp.text)
        task_id = resp.json()["task_id"]
        out: dict = {}
        self.composition["engine"].run_flow({"task_id": task_id}, out,
                                            _ctx(self.composition), _api_tenant(self.composition),
                                            _m(), _e())
        self.assertEqual(out.get("task_status"), "succeeded", out)
        return task_id


def _api_tenant(composition):
    """与 REST 同源租户派生（key 即查询者；零配置期 = 默认租户）。"""
    from api.app import _tenant_from_headers
    from metamemory_framework.security.context import make_tenant

    return make_tenant(*_tenant_from_headers(composition, {}))


def _ctx(composition):
    from api.app import build_context

    return build_context(composition, _api_tenant(composition))


def _m():
    from metamemory_framework.observe.metrics import MetricsImpl

    return MetricsImpl()


def _e():
    from metamemory_framework.container.types import Evolution

    return Evolution()


class TestRestSurface(ApiTestBase):
    def test_add_202_semantics(self):
        """API-001：202 + task_id + sse_url + 快通道确认语义。"""
        resp = self.client.post("/v1/memories", json={
            "source_uri": "conv://e2e-a", "content": "记住我明天要去上海出差",
            "actor": "alice", "occurred_at": "2026-09-27T10:00:00+08:00"})
        self.assertEqual(resp.status_code, 202)
        body = resp.json()
        self.assertTrue(body["task_id"])
        self.assertEqual(body["sse_url"], f"/v1/tasks/{body['task_id']}/events")
        self.assertTrue(body["fastpath"]["stored"])

    def test_search_fused_grep_and_verify(self):
        """API-003：fused 默认 / grep NDJSON 行契约 / verify 逐条附 NLI 判定。"""
        self._seed(uri="conv://s1")
        fused = self.client.post("/v1/search", json={"query": "周五前 交付", "top_k": 5}).json()
        self.assertTrue(fused["items"])
        self.assertIn("confidence", fused["items"][0])
        grep_resp = self.client.post("/v1/search",
                                     json={"query": "周五前 交付", "mode": "grep"})
        self.assertEqual(grep_resp.headers["content-type"].split(";")[0], "application/x-ndjson")
        first_line = json.loads(grep_resp.text.splitlines()[0])
        self.assertIn("memory_id", first_line)
        verified = self.client.post("/v1/search",
                                    json={"query": "周五前 交付", "verify": True}).json()
        self.assertIn("entailment", verified["items"][0])
        self.assertEqual(verified["items"][0]["entailment"]["verdict"], "entail")

    def test_memory_get_and_history(self):
        """API-006/007：单条读取 + 纠错后的 supersedes 链即双时态历史。"""
        self._seed(uri="conv://h1")
        entries = list(self.composition["store"].entries.values())
        memory_id = entries[0].entry_id
        got = self.client.get(f"/v1/memories/{memory_id}")
        self.assertEqual(got.status_code, 200)
        resp = self.client.post("/v1/manage/correct", json={
            "memory_id": memory_id, "correction": "改成周五下班前交付"})
        self.assertEqual(resp.status_code, 200)
        history = self.client.get(f"/v1/memories/{memory_id}/history").json()
        self.assertGreaterEqual(len(history["chain"]), 2)  # 纠错前后两版都在链上
        self.assertEqual(history["chain"][0]["memory_id"], resp.json()["memory_id"])  # 最新在前
        self.assertIn(memory_id, [row["memory_id"] for row in history["chain"]])

    def test_manage_visibility_and_forget(self):
        """API-009/011：降档即生效；gray 遗忘后检索不可见。"""
        self._seed(uri="conv://mv")
        memory_id = list(self.composition["store"].entries.values())[0].entry_id
        resp = self.client.post("/v1/manage/visibility",
                                json={"memory_id": memory_id, "target_level": "L1"})
        self.assertEqual(resp.json()["target_level"], "L1")
        resp = self.client.post("/v1/manage/forget",
                                json={"memory_id": memory_id, "mode": "gray"})
        self.assertEqual(resp.status_code, 200)
        got = self.client.get(f"/v1/memories/{memory_id}")
        self.assertTrue(got.json()["gray_forgotten"])

    def test_intents_declare_confirm_card(self):
        """API-013/014：声明直生效；Agent 候选经确认卡片人裁后生效。"""
        resp = self.client.post("/v1/intents", json={
            "content": "我在找记忆方向的合作伙伴", "visibility": "L1"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["state"], "active")  # 声明直生效
        resp = self.client.post("/v1/intents", json={
            "content": "Agent 捕捉到的疑似意图", "origin": "agent_candidate"})
        self.assertEqual(resp.json()["state"], "candidate")  # Agent 只产候选
        intent_id = resp.json()["intent_id"]
        card = self.client.get(f"/v1/intents/{intent_id}/confirm-card").json()
        self.assertEqual(card["content"], "Agent 捕捉到的疑似意图")
        resp = self.client.post(f"/v1/intents/{intent_id}/confirm", json={"decision": "confirm"})
        self.assertEqual(resp.json()["state"], "active")

    def test_audit_metering_metrics(self):
        """US-7 观测面：审计链可验证 / 计量汇总 / Prometheus 文本指标。"""
        self._seed(uri="conv://am")
        self.assertEqual(self.client.get("/v1/audit/verify").json()["ok"], True)
        self.assertTrue(self.client.get("/v1/audit?limit=5").json()["records"])
        summary = self.client.get("/v1/metering/summary").json()["summary"]
        self.assertTrue(any(row["dimension"] == "storage.events" for row in summary))
        metrics = self.client.get("/v1/metrics")
        self.assertEqual(metrics.headers["content-type"].split(";")[0], "text/plain")

    def test_run_pipeline_consolidate(self):
        """任务面：consolidate 流程手动触发（强度衰减/折叠盘点/模式归纳/图清理）。"""
        self._seed(uri="conv://c1", content="我一定要记住周五前交付方案")
        self._seed(uri="conv://c2", content="我一定要记住周六前交付文档")
        self._seed(uri="conv://c3", content="我一定要记住周日前交付报告")
        resp = self.client.post("/v1/tasks", json={"pipeline": "consolidate"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "succeeded")


class TestAuthKey(unittest.TestCase):
    """key 即查询者：注册 key 后无 key=401；带 key 放行；零配置期放行默认租户。"""

    @classmethod
    def setUpClass(cls):
        from api.app import create_app
        from fastapi.testclient import TestClient

        cls.composition = bootstrap.build_composition(
            config=MetaMemoryConfig(storage=StorageConfig(data_dir=tempfile.mkdtemp())),
            db_path=":memory:")
        cls.composition["inference"] = _FakeProvider()
        cls.client = TestClient(create_app(cls.composition))

    def test_key_lifecycle(self):
        # 零配置引导期：无任何 key → 放行
        self.assertEqual(self.client.post("/v1/search", json={"query": "任意"}).status_code, 200)
        key = self.composition["keys"].create("t1", "d1", "alice", note="test")
        self.assertEqual(self.client.post("/v1/search", json={"query": "任意"}).status_code, 401)
        self.assertEqual(self.client.post("/v1/search", json={"query": "任意"},
                                          headers={"Authorization": f"Bearer {key}"}).status_code, 200)
        self.assertEqual(self.client.post("/v1/search", json={"query": "任意"},
                                          headers={"X-Api-Key": "wrong"}).status_code, 401)


class TestMcpFace(ApiTestBase):
    def test_initialize_and_tools_list(self):
        resp = self.client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        self.assertEqual(resp.json()["result"]["protocolVersion"], "2024-11-05")
        tools = self.client.post("/mcp", json={"jsonrpc": "2.0", "id": 2,
                                               "method": "tools/list"}).json()
        names = {tool["name"] for tool in tools["result"]["tools"]}
        self.assertLessEqual({"memory.add", "memory.search", "memory.context",
                              "memory.manage", "intent.declare"}, names)

    def test_memory_add_search_with_firewall(self):
        """memory.add/search 同契约；worker 异步执行受理任务；结果带注入防御分隔符。"""
        self.composition["worker"].start()
        try:
            resp = self.client.post("/mcp", json={"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                                  "params": {"name": "memory.add", "arguments": {
                                                      "source_uri": "mcp://x1",
                                                      "content": "我一定要记住周五前交付方案",
                                                      "actor": "agent",
                                                      "occurred_at": "2026-09-27T10:00:00+08:00"}}})
            self.assertFalse(resp.json()["result"].get("isError"))
            task_id = json.loads(resp.json()["result"]["content"][0]["text"])["task_id"]
            self._wait_task(task_id)
            resp = self.client.post("/mcp", json={"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                                                  "params": {"name": "memory.search",
                                                             "arguments": {"query": "周五 交付"}}})
            text = resp.json()["result"]["content"][0]["text"]
            self.assertIn("非指令", text)
            self.assertIn("memory_id", text)
        finally:
            self.composition["worker"].stop()

    def _wait_task(self, task_id: str, timeout: float = 5.0) -> None:
        import time

        deadline = time.time() + timeout
        journal = self.composition["journal"]
        while time.time() < deadline:
            record = journal.load_task(task_id)
            if record and record["status"] in ("succeeded", "failed", "partial"):
                self.assertEqual(record["status"], "succeeded", record)
                return
            time.sleep(0.05)
        self.fail(f"任务未在 {timeout}s 内完成: {task_id}")

    def test_intent_via_mcp_is_candidate_only(self):
        """intent.* 工具永远产候选（Agent 不能替用户"有意图"）。"""
        resp = self.client.post("/mcp", json={"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                                              "params": {"name": "intent.declare",
                                                         "arguments": {"content": "想换工作"}}})
        result = json.loads(resp.json()["result"]["content"][0]["text"])
        self.assertEqual(result["state"], "candidate")

    def test_unknown_tool_is_jsonrpc_error(self):
        resp = self.client.post("/mcp", json={"jsonrpc": "2.0", "id": 6, "method": "tools/call",
                                              "params": {"name": "memory.delete", "arguments": {}}})
        self.assertEqual(resp.json()["error"]["code"], -32602)


if __name__ == "__main__":
    unittest.main()
