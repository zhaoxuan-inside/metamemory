"""SQLite Store 槽位实现（ADR-004 lite 档必需槽位的真实现；T-024/T-025 lite 侧）。

SqliteDb = 进程内唯一连接（单容器语义）+ 全语句互斥（FUSE/网络盘上跨连接文件锁
不可靠，单连接从根本上消除竞争；个人规模瓶颈在 LLM 不在 SQL）。
本模块只承载框架表（data/sqlite_framework_tables.sql）；核心表由 domain.persist 装载。
infra 技术动作（豁免五段，_00.conventions.signature_exemptions）。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import threading
import time
from array import array
from pathlib import Path

FRAMEWORK_SCHEMA = Path(__file__).resolve().parents[3] / "data" / "sqlite_framework_tables.sql"

_NOW = lambda: time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())  # noqa: E731


class SqliteDb:
    """SQLite 连接管理：单共享连接（check_same_thread=False）+ 全语句互斥 + 幂等建表。"""

    def __init__(self, db_path: Path | str, schemas: list[Path] | None = None) -> None:
        path_str = str(db_path)
        if path_str != ":memory:":  # 测试可传 :memory:（无文件，实例隔离）
            self.db_path = Path(db_path)
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        else:
            self.db_path = Path(":memory:")
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path_str, timeout=30.0, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        for schema in schemas or [FRAMEWORK_SCHEMA]:
            self._conn.executescript(Path(schema).read_text(encoding="utf-8"))
        self._conn.commit()

    def ensure_schema(self, schema_path: Path) -> None:
        """增量装载另一份 DDL（核心表与框架表分离迁移，ADR-002）。"""
        with self._lock:
            self._conn.executescript(Path(schema_path).read_text(encoding="utf-8"))
            self._conn.commit()

    def conn(self) -> sqlite3.Connection:
        return self._conn

    def execute(self, sql: str, params: tuple | list = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def query(self, sql: str, params: tuple | list = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def transaction(self):
        return _WriteTx(self)


class _WriteTx:
    """写事务上下文：显式 BEGIN IMMEDIATE … COMMIT/ROLLBACK（节点级原子提交用）。"""

    def __init__(self, db: SqliteDb) -> None:
        self.db = db

    def __enter__(self) -> sqlite3.Connection:
        self.db._lock.acquire()
        self.db._conn.execute("BEGIN IMMEDIATE")
        return self.db._conn

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if exc_type is None:
                self.db._conn.commit()
            else:
                self.db._conn.rollback()
        finally:
            self.db._lock.release()


def vec_to_blob(vec: list[float]) -> bytes:
    return array("f", vec).tobytes()


def blob_to_vec(blob: bytes | None) -> list[float] | None:
    if not blob:
        return None
    floats = array("f")
    floats.frombytes(blob)
    return floats.tolist()


def _dump(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def _cosine(a: list[float], b: list[float]) -> float:
    a_norm = sum(x * x for x in a) ** 0.5 or 1.0
    b_norm = sum(x * x for x in b) ** 0.5 or 1.0
    return sum(x * y for x, y in zip(a, b)) / (a_norm * b_norm)


class SqliteTaskQueue:
    """任务队列槽位（SQLite CAS 实现）：入队 + 租约认领 + 过期重派（T-024 lite 侧）。

    认领 = 单条 UPDATE 原子完成（等价 PG SKIP LOCKED 的 SQLite CAS 语义）；
    租约过期（lease_expires_at < now）的 claimed 项可被重新认领——worker 死亡自动重派。
    """

    def __init__(self, db: SqliteDb) -> None:
        self.db = db

    def enqueue(self, queue: str, item: dict) -> int:
        cur = self.db.execute(
            "INSERT INTO queue_items(queue, payload, created_at) VALUES(?,?,?)",
            (queue, _dump(item), _NOW()))
        return int(cur.lastrowid or 0)

    def poll(self, queue: str, limit: int = 1, owner: str = "worker",
             lease_seconds: int = 90) -> list[dict]:
        """CAS 认领：pending（或租约过期的 claimed）按序认领并续租。"""
        now = time.time()
        claimed: list[dict] = []
        with self.db.transaction() as conn:
            rows = conn.execute(
                "SELECT id, payload, attempts FROM queue_items WHERE queue=? AND ("
                " status='pending' OR (status='claimed' AND (lease_expires_at IS NULL"
                " OR lease_expires_at<?))) AND (next_retry_at IS NULL OR next_retry_at<=?)"
                " ORDER BY id LIMIT ?",
                (queue, now, _NOW(), limit)).fetchall()
            for row in rows:
                conn.execute(
                    "UPDATE queue_items SET status='claimed', lease_owner=?, lease_expires_at=?,"
                    " attempts=attempts+1 WHERE id=?", (owner, now + lease_seconds, row["id"]))
                claimed.append({**json.loads(row["payload"]), "_id": row["id"],
                                "_attempts": row["attempts"] + 1,
                                "_lease_expires": now + lease_seconds, "_owner": owner})
        return claimed

    def ack(self, item_id: int) -> None:
        self.db.execute("UPDATE queue_items SET status='done' WHERE id=?", (item_id,))

    def nack(self, item_id: int, retry_backoff_seconds: int = 5, dead_after: int = 5) -> None:
        """认领失败：累计失败次数，退避重派；超过 dead_after 次进死态（显式失败，不静默丢弃）。"""
        self.db.execute("UPDATE queue_items SET attempts=attempts+1 WHERE id=?", (item_id,))
        row = self.db.query("SELECT attempts FROM queue_items WHERE id=?", (item_id,))
        if row and int(row[0]["attempts"]) >= dead_after:
            self.db.execute("UPDATE queue_items SET status='dead', lease_owner=NULL WHERE id=?",
                            (item_id,))
            return
        retry_at = time.strftime("%Y-%m-%dT%H:%M:%S+00:00",
                                 time.gmtime(time.time() + retry_backoff_seconds))
        self.db.execute(
            "UPDATE queue_items SET status='pending', lease_owner=NULL, lease_expires_at=NULL,"
            " next_retry_at=? WHERE id=?", (retry_at, item_id))

    def depth(self, queue: str) -> int:
        row = self.db.query(
            "SELECT COUNT(*) AS n FROM queue_items WHERE queue=? AND status IN ('pending','claimed')",
            (queue,))
        return int(row[0]["n"])


class SqliteCallCache:
    """外部调用缓存（T-025）：llm_calls 表持久化——崩溃重启后同键命中，不重复付费。"""

    def __init__(self, db: SqliteDb) -> None:
        self.db = db
        self.hits = 0
        self.misses = 0

    @staticmethod
    def input_hash(payload) -> str:
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(canonical.encode()).hexdigest()[:16]

    def lookup(self, node_id: str, input_hash: str, prompt_version: str) -> dict | None:
        rows = self.db.query(
            "SELECT response FROM llm_calls WHERE node_id=? AND input_hash=? AND prompt_version=?",
            (node_id, input_hash, prompt_version))
        if not rows or rows[0]["response"] is None:
            self.misses += 1
            return None
        self.hits += 1
        return _load(rows[0]["response"], {})

    def store(self, node_id: str, input_hash: str, prompt_version: str, response: dict,
              step: str = "", provider: str = "", model: str = "", cost_tokens: int = 0,
              task_id: str | None = None) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO llm_calls(task_id, node_id, step, input_hash, prompt_version,"
            " provider, model, response, cost_tokens, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (task_id, node_id, step, input_hash, prompt_version, provider, model,
             _dump(response), cost_tokens, _NOW()))


def _load(text: str | None, default):
    if not text:
        return default
    return json.loads(text)


class SqliteFullTextStore:
    """全文槽位（SQLite FTS5 实现；full 档 = PG FTS+zhparser）。"""

    def __init__(self, db: SqliteDb) -> None:
        self.db = db

    def index(self, ns: str, doc_id: str, text: str) -> None:
        # FTS5 unicode61 不切 CJK：索引与查询统一走 bigram 空格串（同构变换保证命中）
        self.db.execute(
            "INSERT OR REPLACE INTO fts_docs(ns, doc_id, text) VALUES(?,?,?)",
            (ns, doc_id, " ".join(_cjk_bigrams(text))))

    def search(self, ns: str, query: str, k: int = 10) -> list[dict]:
        tokens = _cjk_bigrams(query)
        if not tokens:
            return []
        rows = self.db.query(
            "SELECT doc_id, bm25(fts_docs) AS rank FROM fts_docs WHERE fts_docs MATCH ?"
            " AND ns=? ORDER BY rank LIMIT ?", (" OR ".join(tokens), ns, k))
        return [{"id": row["doc_id"], "score": round(1.0 / (1.0 + max(0.0, row["rank"])), 6)}
                for row in rows]


class SqliteGraphStore:
    """图谱槽位（邻接表 + BFS walk；full 档 = 递归 CTE/图库）。"""

    def __init__(self, db: SqliteDb) -> None:
        self.db = db

    def upsert_edge(self, src: str, dst: str, edge_type: str, props: dict) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO graph_edges(src, dst, edge_type, props) VALUES(?,?,?,?)",
            (src, dst, edge_type, _dump(props)))

    def walk(self, src: str, edge_types: list[str], depth: int = 2) -> list[dict]:
        seen: set[str] = {src}
        frontier = [src]
        walked: list[dict] = []
        placeholders = ",".join("?" for _ in edge_types) or "''"
        for _ in range(max(1, depth)):
            if not frontier:
                break
            marks = ",".join("?" for _ in frontier)
            rows = self.db.query(
                f"SELECT src, dst, edge_type, props FROM graph_edges"
                f" WHERE src IN ({marks}) AND edge_type IN ({placeholders})", (*frontier, *edge_types))
            frontier = []
            for row in rows:
                if row["dst"] in seen:
                    continue
                seen.add(row["dst"])
                walked.append({"src": row["src"], "dst": row["dst"],
                               "edge_type": row["edge_type"], **_load(row["props"], {})})
                frontier.append(row["dst"])
        return walked


class SqliteVectorStore:
    """向量槽位（向量落库 + 内存余弦；full 档 = pgvector）。库重启向量不丢。"""

    def __init__(self, db: SqliteDb) -> None:
        self.db = db
        self._cache: dict[str, dict[str, list[float]]] = {}
        self._loaded: set[str] = set()

    def upsert(self, ns: str, vecs: list[tuple[str, list[float]]]) -> None:
        for doc_id, vec in vecs:
            self.db.execute(
                "INSERT OR REPLACE INTO vectors(ns, doc_id, vec) VALUES(?,?,?)",
                (ns, doc_id, vec_to_blob(vec)))
            if ns in self._loaded:
                self._cache.setdefault(ns, {})[doc_id] = vec

    def search(self, ns: str, query: list[float], k: int = 10) -> list[dict]:
        if ns not in self._loaded:
            self._cache[ns] = {
                row["doc_id"]: blob_to_vec(row["vec"])
                for row in self.db.query("SELECT doc_id, vec FROM vectors WHERE ns=?", (ns,))
                if blob_to_vec(row["vec"]) is not None
            }
            self._loaded.add(ns)
        items = self._cache.get(ns, {})
        scored = sorted(((_cosine(query, vec), doc_id) for doc_id, vec in items.items()),
                        reverse=True)[:k]
        return [{"id": doc_id, "score": round(score, 6)} for score, doc_id in scored]


class SqliteLockStore:
    """锁槽位（SQLite CAS：INSERT/DELETE 原子 + TTL 过期）。"""

    def __init__(self, db: SqliteDb) -> None:
        self.db = db

    def acquire(self, key: str, ttl_seconds: int) -> bool:
        now = time.time()
        with self.db.transaction() as conn:
            row = conn.execute("SELECT expires_at FROM locks WHERE key=?", (key,)).fetchone()
            if row is not None and row["expires_at"] > now:
                return False
            conn.execute(
                "INSERT OR REPLACE INTO locks(key, owner, expires_at) VALUES(?,?,?)",
                (key, f"{time.time_ns()}", now + ttl_seconds))
        return True

    def release(self, key: str) -> None:
        self.db.execute("DELETE FROM locks WHERE key=?", (key,))


class FsObjectStore:
    """对象槽位（本地 FS；full 档 = MinIO/S3）。必需槽位——本地目录即真源。"""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def put(self, bucket: str, key: str, data: bytes) -> None:
        path = self._path(bucket, key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def get(self, bucket: str, key: str) -> bytes:
        return self._path(bucket, key).read_bytes()

    def _path(self, bucket: str, key: str) -> Path:
        safe = key.replace("..", "_").lstrip("/")
        return self.root / bucket / safe


def sign_webhook(payload: bytes, secret: str) -> str:
    """Webhook 签名（HMAC-SHA256）：接收方可验来源。"""
    return hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()


def _cjk_bigrams(text: str) -> list[str]:
    """查询词元：ASCII 词整词 + CJK 连串转 bigram（FTS5 unicode61 的中文等价命中）。"""
    tokens: list[str] = []
    for word in text.lower().split():
        cjk_run = "".join(ch for ch in word if "\u4e00" <= ch <= "\u9fa5")
        ascii_run = "".join(ch for ch in word if ch.isascii() and ch.isalnum())
        if len(cjk_run) == 1:
            tokens.append(cjk_run)
        else:
            tokens.extend(a + b for a, b in zip(cjk_run, cjk_run[1:]))
        if ascii_run:
            tokens.append(ascii_run)
    return tokens
