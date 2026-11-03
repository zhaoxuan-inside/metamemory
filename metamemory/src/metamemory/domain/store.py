"""核心层内存仓储（lite 档 infra）：episodes/segments/entries/edges/authz/published。

full 档由 DataStore/VectorStore/GraphStore 槽位实现替换，仓储接口不变。
本模块为 infra 技术动作（豁免五段，见 _00.conventions.signature_exemptions）。
"""
from __future__ import annotations

from .models import (
    AssociationEdge,
    AuthorizationRecord,
    IntentRecord,
    MemoryEntry,
    MemoryEpisode,
    PublishedItem,
    Segment,
)


class MemoryStore:
    """核心层数据资产容器（进程内真源；接口与 full 档仓储对齐）。

    lite 运行档由 persist.PersistentMemoryStore（SQLite 写穿 + 内存索引）承载同一接口；
    本进程内实现供单测与纯内存场景——不是运行档数据面（三分离：数据只落数据目录）。
    """

    def __init__(self) -> None:
        self.episodes: dict[str, MemoryEpisode] = {}
        self.segments: dict[str, Segment] = {}
        self.entries: dict[str, MemoryEntry] = {}
        self.edges: list[AssociationEdge] = []
        self.authz: dict[str, AuthorizationRecord] = {}
        self.published: dict[str, PublishedItem] = {}
        self.intents: dict[str, IntentRecord] = {}
        self.by_uri: dict[str, str] = {}
        self.dedupe: dict[str, list[str]] = {}
        self.seq = 0

    def next_id(self, prefix: str) -> str:
        self.seq += 1
        return f"{prefix}_{self.seq:06d}"

    def add_episode(self, episode: MemoryEpisode) -> MemoryEpisode:
        self.episodes[episode.episode_id] = episode
        self.by_uri[episode.source_uri] = episode.episode_id
        self.dedupe.setdefault(episode.content_hash(), []).append(episode.episode_id)  # 查重短路索引
        return episode

    def add_segment(self, segment: Segment) -> Segment:
        self.segments[segment.segment_id] = segment
        return segment

    def add_entry(self, entry: MemoryEntry) -> MemoryEntry:
        self.entries[entry.entry_id] = entry
        return entry

    def add_edge(self, edge: AssociationEdge) -> AssociationEdge:
        self.edges.append(edge)
        return edge

    def find_entries(self, subject: str | None = None, predicate: str | None = None) -> list[MemoryEntry]:
        return [
            e for e in self.entries.values()
            if (subject is None or e.subject == subject) and (predicate is None or e.predicate == predicate)
        ]

    def supersede(self, old: MemoryEntry, new: MemoryEntry) -> MemoryEntry:
        """更新 = 关闭旧原子 + supersedes 指针，不删除（双时态模型）。"""
        old.valid_to = new.valid_from
        new.supersedes = old.entry_id
        self.add_edge(AssociationEdge(
            edge_id=self.next_id("edge"), edge_type="supersedes",
            src_id=new.entry_id, dst_id=old.entry_id, evidence={"rule": "same_subject_predicate"},
        ))
        return self.add_entry(new)

    def entries_of(self, episode_id: str) -> list[MemoryEntry]:
        return [e for e in self.entries.values() if e.anchor_episode == episode_id]

    # ---- 授权/发布/意图（写面统一走 save_*：内存实现就地落 dict，持久实现写穿 SQLite）----

    def save_authz(self, record: AuthorizationRecord) -> AuthorizationRecord:
        self.authz[record.auth_id] = record
        return record

    def save_published(self, item: PublishedItem) -> PublishedItem:
        self.published[item.item_id] = item
        return item

    def save_intent(self, intent: IntentRecord) -> IntentRecord:
        if not intent.intent_id:
            intent.intent_id = f"intent_{intent.entry_id}"
        self.intents[intent.entry_id] = intent
        return intent

    def get_intent(self, entry_id: str) -> IntentRecord | None:
        return self.intents.get(entry_id)
