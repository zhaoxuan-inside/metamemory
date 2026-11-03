"""配置面测试：默认值、JSON 装载、环境变量覆盖、档位校验（问题 2/3 的载体）。"""
import json
import os
import tempfile
import unittest

from metamemory.config import MetaMemoryConfig, load_config


class TestDefaults(unittest.TestCase):
    def test_documented_defaults(self):
        cfg = MetaMemoryConfig()
        self.assertEqual(cfg.profile, "lite")                       # 推荐默认：本地全闭环
        self.assertEqual(cfg.memory.reconsolidation_window_hours, 72)   # 约束 11
        self.assertEqual(cfg.recall.rrf_k, 60)                      # 技术词典
        self.assertEqual(cfg.recall.top_k, 10)
        self.assertEqual(cfg.recall.token_budget, 2048)
        self.assertEqual(cfg.inference.prompt_version, 'v1')        # 网关幂等键组成（ADR-006 唯一路径）
        self.assertEqual(cfg.quota.discovery_slots_free, 3)         # G3 口径
        self.assertTrue(cfg.governance.purpose_gate_enforced)       # 约束 16 本期必做
        self.assertEqual(cfg.governance.audit_retention_domain_days, 180)

    def test_to_dict_roundtrip(self):
        cfg = MetaMemoryConfig.from_dict(MetaMemoryConfig().to_dict())
        self.assertEqual(cfg.memory.reconsolidation_window_hours, 72)

    def test_invalid_profile_rejected(self):
        with self.assertRaises(ValueError):
            MetaMemoryConfig(profile="staging")


class TestLoadConfig(unittest.TestCase):
    def test_json_file(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump({"profile": "full", "recall": {"top_k": 25}}, fh)
            path = fh.name
        try:
            cfg = load_config(path)
            self.assertEqual(cfg.profile, "full")
            self.assertEqual(cfg.recall.top_k, 25)
            self.assertEqual(cfg.recall.rrf_k, 60)  # 未覆盖项保持默认
        finally:
            os.unlink(path)

    def test_env_override(self):
        env = {"METAMEMORY_PROFILE": "minimal",
               "METAMEMORY_RECALL_TOP_K": "30",
               "METAMEMORY_MEMORY_RECONSOLIDATION_WINDOW_HOURS": "48"}
        saved = {k: os.environ.pop(k, None) for k in env}
        os.environ.update(env)
        try:
            cfg = load_config()
            self.assertEqual(cfg.profile, "minimal")
            self.assertEqual(cfg.recall.top_k, 30)
            self.assertEqual(cfg.memory.reconsolidation_window_hours, 48)
        finally:
            for k, v in saved.items():
                os.environ.pop(k, None)
                if v is not None:
                    os.environ[k] = v


if __name__ == "__main__":
    unittest.main()
