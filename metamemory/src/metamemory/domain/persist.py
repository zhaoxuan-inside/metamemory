"""SQLite 持久化仓储（三分离·数据侧，ADR-004 lite 档必需槽位的真实现）。

真源 = 数据目录下的 SQLite 文件（WAL）；内存 dict 只是启动时加载的索引/缓存——
每次变更写穿（write-through），重启后完整重建，进程内存不是数据承载（区别于单测用
store.MemoryStore）。表结构见 data/sqlite_schema.sql（与 PG DDL 对齐）。

infra 技术动作（豁免五段，_00.conventions.signature_exemptions）。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from metamemory_framework.stores.sqlite_impl import (
    SqliteDb,
    blob_to_vec,
    vec_to_blob,
)

from .models import (
    AssociationEdge,
    AuthorizationRecord,
    IntentRecord,
    MemoryEntry,
    MemoryEpisode,
    PublishedItem,
    Segment,
)
from .store import MemoryStore

CORE_SCHEMA = Path(__file__).resolve().parents[3] / "data" / "sqlite_core_tables.sql"

_NOW = lambda: time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())  # noqa: E731


def _dump(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def _load(text: str | None, default):
    if not text:
        return default
    return json.loads(text)


class PersistentMemoryStore(MemoryStore):
    """写穿式核心仓储：接口与 MemoryStore 完全一致，真源在 SQLite 文件。"""

    def __init__(self, db_path: Path | str | SqliteDb) -> None:
        super().__init__()
        if isinstance(db_path, SqliteDb):
            self.db = db_path
            self.db.ensure_schema(CORE_SCHEMA)  # 核心表分离装载（ADR-002）
        else:
            self.db = SqliteDb(db_path, schemas=[])  # 仅建连接；核心表随后装载
            self.db.ensure_schema(CORE_SCHEMA)
        self._load_all()

    # ---- 装载：启动时从真源重建内存索引 ----

    def _load_all(self) -> None:
        for row in self.db.query("SELECT * FROM memory_episodes ORDER BY rowid"):
            episode = MemoryEpisode(
                episode_id=row["episode_id"], tenant_id=row["tenant_id"], domain_id=row["domain_id"],
                source_uri=row["source_uri"], content=row["content"], actor=row["actor"],
                occurred_at=row["occurred_at"], topics=_load(row["topics"], []),
            )
            super().add_episode(episode)
        for row in self.db.query("SELECT * FROM segments ORDER BY rowid"):
            super().add_segment(Segment(
                segment_id=row["segment_id"], episode_id=row["episode_id"], text=row["text"],
                span=(row["span_start"], row["span_end"]), basis=row["basis"],
                param_version=row["param_version"],
            ))
        for row in self.db.query("SELECT * FROM memory_entries ORDER BY rowid"):
            super().add_entry(MemoryEntry(
                entry_id=row["entry_id"], tenant_id=row["tenant_id"], domain_id=row["domain_id"],
                subject=row["subject"] or "", predicate=row["predicate"] or "",
                object_value=row["object_value"], entry_type=row["entry_type"],
                valid_from=row["valid_from"], valid_to=row["valid_to"],
                learned_at=row["learned_at"], supersedes=row["supersedes"],
                anchor_episode=row["anchor_episode"] or "",
                anchor_span=(row["anchor_start"], row["anchor_end"]),
                source_level=row["source_level"], confidence=row["confidence"],
                visibility=row["visibility"], strength=row["strength"],
                last_recall_at=row["last_recall_at"],
                embedding=blob_to_vec(row["embedding"]), embedding_model=row["embedding_model"] or "",
                injected_suspect=bool(row["injected_suspect"]),
                gray_forgotten=bool(row["gray_forgotten"]), stale=bool(row["stale"]),
            ))
        for row in self.db.query("SELECT * FROM association_edges ORDER BY rowid"):
            self.edges.append(AssociationEdge(
                edge_id=row["edge_id"], edge_type=row["edge_type"], src_id=row["src_id"],
                dst_id=row["dst_id"], weight=row["weight"], confidence=row["confidence"],
                evidence=_load(row["evidence"], {}),
            ))
        for row in self.db.query("SELECT * FROM authorization_records ORDER BY rowid"):
            self.authz[row["auth_id"]] = AuthorizationRecord(
                auth_id=row["auth_id"], grantee=row["grantee"], object_ref=row["object_ref"],
                level=row["level"], purpose=row["purpose"],
                allowed_purposes=_load(row["allowed_purposes"], []),
                expires_at=row["expires_at"], status=row["status"],
                tenant_id=row["tenant_id"] or "", domain_id=row["domain_id"] or "",
            )
        for row in self.db.query("SELECT * FROM published_items ORDER BY rowid"):
            self.published[row["item_id"]] = PublishedItem(
                item_id=row["item_id"], entry_id=row["entry_id"], level=row["level"],
                allowed_purposes=_load(row["allowed_purposes"], []),
                review_status=row["review_status"], published_at=row["published_at"],
                withdraw_deadline=row["withdraw_deadline"], summary=row["summary"] or "",
            )
        for row in self.db.query("SELECT * FROM intent_details ORDER BY rowid"):
            self.intents[row["entry_id"]] = IntentRecord(
                intent_id=f"intent_{row['entry_id']}", entry_id=row["entry_id"],
                state=row["state"], ttl_expires_at=row["ttl_expires_at"],
                visibility=row["visibility"], expected_response=row["expected_response"],
                origin=row["origin"],
            )
        counter = self.db.query("SELECT value FROM kv_counters WHERE name='store.seq'")
        self.seq = int(counter[0]["value"]) if counter else 0

    # ---- 写穿：所有变体方法先持久化、后进内存索引 ----

    def next_id(self, prefix: str) -> str:
        self.seq += 1
        self.db.execute(
            "INSERT INTO kv_counters(name, value) VALUES('store.seq', ?) "
            "ON CONFLICT(name) DO UPDATE SET value=excluded.value", (self.seq,))
        return f"{prefix}_{self.seq:06d}"

    def add_episode(self, episode: MemoryEpisode) -> MemoryEpisode:
        self.db.execute(
            "INSERT OR REPLACE INTO memory_episodes(episode_id, tenant_id, domain_id, source_uri,"
            " content, actor, occurred_at, topics, content_hash, created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (episode.episode_id, episode.tenant_id, episode.domain_id, episode.source_uri,
             episode.content, episode.actor, episode.occurred_at, _dump(episode.topics),
             episode.content_hash(), _NOW()))
        return super().add_episode(episode)

    def add_segment(self, segment: Segment) -> Segment:
        self.db.execute(
            "INSERT OR REPLACE INTO segments(segment_id, episode_id, text, span_start, span_end,"
            " basis, param_version) VALUES(?,?,?,?,?,?,?)",
            (segment.segment_id, segment.episode_id, segment.text,
             segment.span[0], segment.span[1], segment.basis, segment.param_version))
        return super().add_segment(segment)

    def add_entry(self, entry: MemoryEntry) -> MemoryEntry:
        self.db.execute(
            "INSERT OR REPLACE INTO memory_entries(entry_id, tenant_id, domain_id, subject, predicate,"
            " object_value, entry_type, valid_from, valid_to, learned_at, supersedes, anchor_episode,"
            " anchor_start, anchor_end, source_level, confidence, visibility, strength, last_recall_at,"
            " embedding, embedding_model, injected_suspect, gray_forgotten, stale, created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (entry.entry_id, entry.tenant_id, entry.domain_id, entry.subject, entry.predicate,
             entry.object_value, entry.entry_type, entry.valid_from, entry.valid_to,
             entry.learned_at, entry.supersedes, entry.anchor_episode or None,
             entry.anchor_span[0], entry.anchor_span[1], entry.source_level, entry.confidence,
             entry.visibility, entry.strength, entry.last_recall_at,
             vec_to_blob(entry.embedding) if entry.embedding is not None else None,
             entry.embedding_model, int(entry.injected_suspect), int(entry.gray_forgotten),
             int(entry.stale), _NOW()))
        return super().add_entry(entry)

    def add_edge(self, edge: AssociationEdge) -> AssociationEdge:
        src = self.entries.get(edge.src_id)
        self.db.execute(
            "INSERT OR REPLACE INTO association_edges(edge_id, tenant_id, edge_type, src_id, dst_id,"
            " weight, confidence, evidence, valid_from, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (edge.edge_id, src.tenant_id if src else "", edge.edge_type, edge.src_id,
             edge.dst_id, edge.weight, edge.confidence, _dump(edge.evidence), _NOW(), _NOW()))
        return super().add_edge(edge)

    def supersede(self, old: MemoryEntry, new: MemoryEntry) -> MemoryEntry:
        result = super().supersede(old, new)
        self.db.execute("UPDATE memory_entries SET valid_to=? WHERE entry_id=?",
                        (old.valid_to, old.entry_id))
        return result

    # ---- 治理动作（S2 manage 动词的存储面；调用方负责审计留痕）----

    def touch_entry(self, entry: MemoryEntry) -> None:
        """检索命中：重置再巩固窗口锚点（约束 11），写穿。"""
        self.db.execute("UPDATE memory_entries SET last_recall_at=? WHERE entry_id=?",
                        (entry.last_recall_at, entry.entry_id))

    def set_entry_visibility(self, entry: MemoryEntry) -> None:
        self.db.execute("UPDATE memory_entries SET visibility=? WHERE entry_id=?",
                        (entry.visibility, entry.entry_id))

    def set_entry_embedding(self, entry: MemoryEntry) -> None:
        self.db.execute("UPDATE memory_entries SET embedding=?, embedding_model=? WHERE entry_id=?",
                        (vec_to_blob(entry.embedding) if entry.embedding is not None else None,
                         entry.embedding_model, entry.entry_id))

    def set_entry_stale(self, entry: MemoryEntry) -> None:
        """血缘回溯标记写穿（recompute 管道 collect/clear 消费）。"""
        self.db.execute("UPDATE memory_entries SET stale=? WHERE entry_id=?",
                        (int(entry.stale), entry.entry_id))

    def gray_forget(self, entry_id: str) -> bool:
        """灰色遗忘：降权可恢复（strength=0 + 标记；检索路排除）。"""
        entry = self.entries.get(entry_id)
        if entry is None:
            return False
        entry.gray_forgotten = True
        entry.strength = 0.0
        self.db.execute("UPDATE memory_entries SET gray_forgotten=1, strength=0 WHERE entry_id=?",
                        (entry_id,))
        return True

    def restore_gray(self, entry_id: str) -> bool:
        entry = self.entries.get(entry_id)
        if entry is None or not entry.gray_forgotten:
            return False
        entry.gray_forgotten = False
        entry.strength = 1.0
        self.db.execute("UPDATE memory_entries SET gray_forgotten=0, strength=1 WHERE entry_id=?",
                        (entry_id,))
        return True

    def hard_forget(self, entry_id: str) -> dict:
        """硬遗忘（唯一真删除）：条目 + 双向边 + 指向它的 supersedes 引用级联清除。"""
        removed = {"edges": 0, "supersedes_refs": 0}
        with self.db.transaction() as conn:
            removed["edges"] = conn.execute(
                "DELETE FROM association_edges WHERE src_id=? OR dst_id=?", (entry_id, entry_id)).rowcount
            removed["supersedes_refs"] = conn.execute(
                "UPDATE memory_entries SET supersedes=NULL WHERE supersedes=?", (entry_id,)).rowcount
            conn.execute("DELETE FROM memory_entries WHERE entry_id=?", (entry_id,))
        self.edges = [e for e in self.edges if e.src_id != entry_id and e.dst_id != entry_id]
        self.entries.pop(entry_id, None)
        for other in self.entries.values():
            if other.supersedes == entry_id:
                other.supersedes = None
        return removed

    def add_visibility_event(self, entry_id: str, domain_id: str, from_level: str | None,
                             to_level: str, confirm_ref: str | None) -> int:
        cur = self.db.execute(
            "INSERT INTO visibility_events(entry_id, domain_id, from_level, to_level, confirm_ref,"
            " created_at) VALUES(?,?,?,?,?,?)",
            (entry_id, domain_id, from_level, to_level, confirm_ref, _NOW()))
        return int(cur.lastrowid or 0)

    # ---- 授权 / 发布 / 意图 ----

    def save_authz(self, record: AuthorizationRecord) -> AuthorizationRecord:
        self.authz[record.auth_id] = record
        self.db.execute(
            "INSERT OR REPLACE INTO authorization_records(auth_id, tenant_id, domain_id, grantee,"
            " object_ref, level, purpose, allowed_purposes, expires_at, status, created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (record.auth_id, record.tenant_id, record.domain_id, record.grantee, record.object_ref,
             record.level, record.purpose, _dump(record.allowed_purposes), record.expires_at,
             record.status, _NOW()))
        return record

    def save_published(self, item: PublishedItem) -> PublishedItem:
        self.published[item.item_id] = item
        self.db.execute(
            "INSERT OR REPLACE INTO published_items(item_id, entry_id, level, allowed_purposes,"
            " review_status, summary, published_at, withdraw_deadline) VALUES(?,?,?,?,?,?,?,?)",
            (item.item_id, item.entry_id, item.level, _dump(item.allowed_purposes),
             item.review_status, item.summary, item.published_at, item.withdraw_deadline))
        return item

    def save_intent(self, intent: IntentRecord) -> IntentRecord:
        if not intent.intent_id:
            intent.intent_id = f"intent_{intent.entry_id}"
        self.intents[intent.entry_id] = intent
        self.db.execute(
            "INSERT OR REPLACE INTO intent_details(entry_id, state, ttl_expires_at, visibility,"
            " expected_response, origin, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (intent.entry_id, intent.state, intent.ttl_expires_at, intent.visibility,
             intent.expected_response, intent.origin,
             time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(intent.created_at)),
             time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(intent.updated_at))))
        return intent
