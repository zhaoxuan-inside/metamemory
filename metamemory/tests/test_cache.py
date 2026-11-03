"""T-023（M1）：调用缓存——llm_calls 幂等重放的 lite 版（A7：崩溃重试不重复付费）。"""
import unittest

from common import stubs

from metamemory.ingest.extract import extract_deep
from metamemory_framework.recovery.cache import CallCache


class CountingProvider:
    """计数替身：统计真实强模型调用次数。"""

    def __init__(self, atoms) -> None:
        self.atoms = atoms
        self.calls = 0

    def complete(self, request: dict) -> dict:
        self.calls += 1
        if request.get("task") == "nli":
            return {"text": '{"verdict": "entail", "confidence": 0.95}', "tokens": 5, "provider": "counting"}
        return {"atoms": self.atoms, "tokens": 20, "provider": "counting"}


def _selected(text: str, worthiness: str = "high"):
    return [{"segment_id": "s1", "text": text, "span": [0, len(text)],
             "suspect": False, "pii": False, "confidence": 0.95, "worthiness": worthiness}]


class TestCallCache(unittest.TestCase):
    def _run_extract(self, ctx, tenant, m, evo, provider, text):
        out = {}
        ok = extract_deep({"selected": _selected(text), "normalized": {"content": text}},
                          out, ctx, tenant, m, evo)
        return ok, out

    def test_same_input_hits_cache_and_never_repay(self):
        provider = CountingProvider([{"text": "我在准备记忆项目", "subject": "user",
                                      "predicate": "working_on", "quote": "我在准备记忆项目",
                                      "entry_type": "fact"}])
        cache = CallCache()
        ctx, tenant, m, evo = stubs(deps={"inference": provider, "call_cache": cache})
        text = "我在准备记忆项目"
        ok1, out1 = self._run_extract(ctx, tenant, m, evo, provider, text)
        ok2, out2 = self._run_extract(ctx, tenant, m, evo, provider, text)
        self.assertTrue(ok1 and ok2)
        self.assertEqual(provider.calls, 2)  # 首轮 extract×1 + NLI×1；重放双命中缓存零新增
        self.assertEqual(m.counters.get("llm_cache_hit"), 2)  # 第二轮 extract+NLI 各命中一次
        self.assertEqual(out1["candidates"], out2["candidates"])

    def test_different_input_is_cache_miss(self):
        provider = CountingProvider([{"text": "A", "subject": "user", "predicate": "p",
                                      "quote": "A", "entry_type": "fact"}])
        ctx, tenant, m, evo = stubs(deps={"inference": provider, "call_cache": CallCache()})
        self._run_extract(ctx, tenant, m, evo, provider, "内容一")
        self._run_extract(ctx, tenant, m, evo, provider, "内容二")
        self.assertEqual(provider.calls, 2)  # 异构输入 miss：extract×2；quote 不在原文 → 无锚点 → NLI 不调用
        self.assertIsNone(m.counters.get("llm_cache_hit"))

    def test_cache_key_includes_prompt_version(self):
        cache = CallCache()
        cache.store("step", "hash", "v1", {"atoms": []})
        self.assertIsNotNone(cache.lookup("step", "hash", "v1"))
        self.assertIsNone(cache.lookup("step", "hash", "v2"))  # prompt 变更 → 不复用

    def test_without_cache_calls_directly(self):
        provider = CountingProvider([{"text": "A", "subject": "user", "predicate": "p",
                                      "quote": "A", "entry_type": "fact"}])
        ctx, tenant, m, evo = stubs(deps={"inference": provider})
        self._run_extract(ctx, tenant, m, evo, provider, "文本")
        self.assertEqual(provider.calls, 1)


if __name__ == "__main__":
    unittest.main()
