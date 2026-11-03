"""S1 三分离·数据侧验收：SQLite 持久化仓储——重启往返/写穿/遗忘/并发（T-034）。"""
import threading
import tempfile
import time
import unittest
from pathlib import Path

from common import make_tenant as mk_tenant  # noqa: F401  sys.path 引导
from metamemory.domain.models import (
    AssociationEdge,
    AuthorizationRecord,
    IntentRecord,
    MemoryEntry,
    MemoryEpisode,
    PublishedItem,
    Segment,
)
from metamemory.domain.persist import PersistentMemoryStore
from metamemory.domain.store import MemoryStore


def _entry(entry_id: str, text: str, **kw) -> MemoryEntry:
    return MemoryEntry(entry_id=entry_id, tenant_id="t1", domain_id="d1", subject="user",
                       predicate="mentioned", object_value=text, entry_type="fact",
                       valid_from="2026-09-27T10:00:00+08:00", anchor_episode="ep_1", **kw)


class TestPersistentStore(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "metamemory.db"

    def tearDown(self):
        self._tmp.cleanup()

    def _new_store(self) -> PersistentMemoryStore:
        return PersistentMemoryStore(self.db_path)

    def test_restart_roundtrip(self):
        """真源在文件：进程重启（新实例）后全量重建——episodes/segments/entries/edges/authz。"""
        store = self._new_store()
        episode = MemoryEpisode(episode_id="ep_1", tenant_id="t1", domain_id="d1",
                                source_uri="conv://a", content="我一定要在周五前交付重要方案",
                                actor="alice", occurred_at="2026-09-27T10:00:00+08:00", topics=["工作"])
        store.add_episode(episode)
        store.add_segment(Segment(segment_id="seg_1", episode_id="ep_1", text="我一定要在周五前交付重要方案",
                                  span=(0, 15), basis="paragraph_split", param_version="seg-v0"))
        entry = _entry("mem_1", "我一定要在周五前交付重要方案")
        store.add_entry(entry)
        store.add_edge(AssociationEdge(edge_id="edge_1", edge_type="contextual",
                                       src_id="mem_1", dst_id="mem_1", evidence={"episode": "ep_1"}))
        record = AuthorizationRecord(auth_id="auth_1", grantee="bob", object_ref="mem_1",
                                     level="L2", purpose="assist", allowed_purposes=["assist"],
                                     tenant_id="t1", domain_id="d1")
        store.save_authz(record)
        intent = IntentRecord(intent_id="intent_mem_1", entry_id="mem_1", state="active", visibility="L3")
        store.save_intent(intent)
        item = PublishedItem(item_id="pub_1", entry_id="mem_1", level="L4", allowed_purposes=["match"])
        store.save_published(item)
        last_seq = store.seq

        reopened = self._new_store()
        self.assertEqual(len(reopened.episodes), 1)
        self.assertEqual(reopened.episodes["ep_1"].topics, ["工作"])
        self.assertEqual(len(reopened.segments), 1)
        self.assertEqual(reopened.segments["seg_1"].span, (0, 15))
        self.assertIn("mem_1", reopened.entries)
        self.assertEqual(reopened.by_uri["conv://a"], "ep_1")
        self.assertEqual(reopened.dedupe[episode.content_hash()], ["ep_1"])
        self.assertEqual(len(reopened.edges), 1)
        self.assertIn("auth_1", reopened.authz)
        self.assertIn("mem_1", reopened.intents)
        self.assertIn("pub_1", reopened.published)
        # id 序号持久：重启后不撞号
        self.assertEqual(reopened.next_id("mem"), f"mem_{last_seq + 1:06d}")

    def test_supersede_persists(self):
        """更新=关闭旧原子+指针：valid_to/supersedes/边全部写穿。"""
        store = self._new_store()
        old = _entry("mem_1", "我在北京工作")
        new = _entry("mem_2", "我搬到上海工作了")
        new.valid_from = "2026-10-01T00:00:00+08:00"
        store.add_entry(old)
        store.supersede(old, new)
        reopened = self._new_store()
        self.assertEqual(reopened.entries["mem_1"].valid_to, "2026-10-01T00:00:00+08:00")
        self.assertEqual(reopened.entries["mem_2"].supersedes, "mem_1")
        supersede_edges = [e for e in reopened.edges if e.edge_type == "supersedes"]
        self.assertEqual(len(supersede_edges), 1)

    def test_gray_and_hard_forget(self):
        """遗忘双轨：gray 降权可恢复；hard 物理删除+级联（唯一真删除）。"""
        store = self._new_store()
        keep = _entry("mem_1", "保留的记忆")
        doomed = _entry("mem_2", "要硬删的记忆")
        downstream = _entry("mem_3", "派生下游", supersedes="mem_2")
        store.add_entry(keep)
        store.add_entry(doomed)
        store.add_entry(downstream)
        store.add_edge(AssociationEdge(edge_id="edge_1", edge_type="semantic",
                                       src_id="mem_2", dst_id="mem_1", evidence={"rule": "test"}))
        # gray：降权 + 退出检索标记，可恢复
        self.assertTrue(store.gray_forget("mem_1"))
        self.assertTrue(store.entries["mem_1"].gray_forgotten)
        self.assertEqual(store.entries["mem_1"].strength, 0.0)
        self.assertTrue(store.restore_gray("mem_1"))
        self.assertEqual(store.entries["mem_1"].strength, 1.0)
        # hard：条目+双向边删除，指向它的 supersedes 引用清空
        removed = store.hard_forget("mem_2")
        self.assertEqual(removed["edges"], 1)
        self.assertNotIn("mem_2", store.entries)
        self.assertIsNone(store.entries["mem_3"].supersedes)
        reopened = self._new_store()
        self.assertNotIn("mem_2", reopened.entries)
        self.assertEqual([e for e in reopened.edges if "mem_2" in (e.src_id, e.dst_id)], [])
        self.assertIsNone(reopened.entries["mem_3"].supersedes)
        self.assertIn("mem_1", reopened.entries)  # 无关条目不受影响

    def test_touch_entry_persists(self):
        """检索命中重置再巩固锚点：last_recall_at 写穿（数据流不变式⑤）。"""
        store = self._new_store()
        entry = _entry("mem_1", "会被唤起的记忆")
        store.add_entry(entry)
        entry.last_recall_at = time.time()
        store.touch_entry(entry)
        reopened = self._new_store()
        self.assertIsNotNone(reopened.entries["mem_1"].last_recall_at)

    def test_concurrent_writes_single_connection(self):
        """并发写穿（线程池/HTTP handler 场景）：单连接互斥，零锁死零丢失。"""
        store = self._new_store()
        errors: list[str] = []

        def worker(n: int) -> None:
            try:
                for i in range(10):
                    store.add_entry(_entry(f"mem_{n * 100 + i:04d}", f"并发记忆{n}-{i}"))
            except Exception as exc:  # pragma: no cover - 失败即断言失败
                errors.append(f"{type(exc).__name__}: {exc}")

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        reopened = self._new_store()
        self.assertEqual(len(reopened.entries), 60)

    def test_memory_store_is_inmemory_only(self):
        """进程内 MemoryStore 不是运行档数据面：仅单测/纯内存场景（三分离语义锚）。"""
        store = MemoryStore()
        store.add_entry(_entry("mem_1", "内存态"))
        self.assertIn("mem_1", store.entries)
        self.assertFalse(hasattr(store, "db"))


if __name__ == "__main__":
    unittest.main()
