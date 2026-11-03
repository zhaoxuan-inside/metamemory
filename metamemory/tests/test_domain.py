"""T-010：领域数据模型与双时态仓储（supersedes 关闭不删除）。"""
import unittest

from common import make_tenant, stubs

from metamemory.domain.models import MemoryEntry, MemoryEpisode
from metamemory.domain.store import MemoryStore


def entry(store: MemoryStore, **kw):
    base = dict(entry_id=store.next_id("mem"), tenant_id="t1", domain_id="d1",
                subject="user", predicate="lives_in", object_value="北京",
                entry_type="fact", valid_from="2024-01-01T00:00:00+08:00")
    base.update(kw)
    return MemoryEntry(**base)


class TestBitemporalSupersede(unittest.TestCase):
    def test_supersede_closes_old_and_links(self):
        store = MemoryStore()
        old = store.add_entry(entry(store))
        new = store.supersede(old, entry(store, object_value="上海",
                                         valid_from="2026-09-01T00:00:00+08:00"))
        self.assertEqual(old.valid_to, "2026-09-01T00:00:00+08:00")  # 世界时间关闭
        self.assertEqual(new.supersedes, old.entry_id)
        edge = [e for e in store.edges if e.edge_type == "supersedes"][0]
        self.assertEqual(edge.src_id, new.entry_id)
        self.assertEqual(edge.dst_id, old.entry_id)
        self.assertTrue(edge.evidence)  # 无依据不建边

    def test_find_entries_by_predicate(self):
        store = MemoryStore()
        store.add_entry(entry(store))
        store.add_entry(entry(store, predicate="prefers", object_value="dark"))
        self.assertEqual(len(store.find_entries(subject="user", predicate="lives_in")), 1)


class TestEpisode(unittest.TestCase):
    def test_content_hash_stable(self):
        ep = MemoryEpisode(episode_id="ep_1", tenant_id="t1", domain_id="d1",
                           source_uri="conv://1", content="内容", actor="a",
                           occurred_at="2026-09-27T10:00:00+08:00")
        self.assertEqual(ep.content_hash(), ep.content_hash())

    def test_store_uri_index(self):
        store = MemoryStore()
        ep = MemoryEpisode(episode_id="ep_1", tenant_id="t1", domain_id="d1",
                           source_uri="conv://1", content="内容", actor="a",
                           occurred_at="2026-09-27T10:00:00+08:00")
        store.add_episode(ep)
        self.assertEqual(store.by_uri["conv://1"], "ep_1")
        self.assertEqual(store.dedupe[ep.content_hash()], ["ep_1"])


if __name__ == "__main__":
    unittest.main()
