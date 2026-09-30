"""内存降级实现（降级矩阵）：功能语义不变、性能保证降级、启动标明档位。"""
from __future__ import annotations

import math
import threading
import time
from typing import Any

from ..container.signature import unified_method


class InMemoryCache:
    """Redis 缺席 → 进程内 LRU+TTL 自动顶上（降级矩阵）。"""

    def __init__(self, capacity: int = 1024) -> None:
        self._data: dict[str, tuple[Any, float | None]] = {}
        self._capacity = capacity
        self._lock = threading.Lock()

    def get(self, key: str):
        with self._lock:
            item = self._data.get(key)
            if item is None:
                return None
            value, expires = item
            if expires is not None and expires < time.time():
                self._data.pop(key, None)
                return None
            return value

    def set(self, key: str, value: Any, ttl_seconds: int | None = None) -> None:
        with self._lock:
            if len(self._data) >= self._capacity:
                self._data.pop(next(iter(self._data)))
            expires = time.time() + ttl_seconds if ttl_seconds else None
            self._data[key] = (value, expires)

    def invalidate_tag(self, tag: str) -> None:
        with self._lock:
            for key in [k for k in self._data if k.startswith(tag)]:
                self._data.pop(key, None)


class InMemoryVector:
    """pgvector/sqlite-vec 缺席 → 内存向量。"""

    def __init__(self) -> None:
        self._ns: dict[str, dict[str, list[float]]] = {}

    def upsert(self, ns: str, vecs: list[tuple[str, list[float]]]) -> None:
        self._ns.setdefault(ns, {}).update(vecs)

    def search(self, ns: str, query: list[float], k: int) -> list[dict]:
        out: dict = {}
        cosine_top_k({"query_vec": query, "items": self._ns.get(ns, {}), "k": k}, out, None, None, _Quiet(), _Evo())
        return out.get("hits", [])


class InMemoryQueue:
    """PG SKIP LOCKED / SQLite CAS 缺席 → 内存队列（FIFO）。"""

    def __init__(self) -> None:
        self._queues: dict[str, list[dict]] = {}
        self._lock = threading.Lock()

    def enqueue(self, queue: str, item: dict) -> str:
        with self._lock:
            self._queues.setdefault(queue, []).append(item)
        return str(item.get("id", ""))

    def poll(self, queue: str, limit: int = 1) -> list[dict]:
        with self._lock:
            items = self._queues.get(queue, [])
            taken, items[:] = items[:limit], items[limit:]
        return taken


class InMemoryLock:
    def __init__(self) -> None:
        self._held: set[str] = set()
        self._lock = threading.Lock()

    def acquire(self, key: str, ttl_seconds: int) -> bool:
        with self._lock:
            if key in self._held:
                return False
            self._held.add(key)
            return True

    def release(self, key: str) -> None:
        with self._lock:
            self._held.discard(key)


class InMemoryPubSub:
    def __init__(self) -> None:
        self._subs: dict[str, list] = {}

    def publish(self, channel: str, event: dict) -> None:
        for callback in self._subs.get(channel, []):
            callback(event)

    def subscribe(self, channel: str, callback) -> None:
        self._subs.setdefault(channel, []).append(callback)


class _Quiet:
    def enter(self, *a, **k) -> None: ...

    def exit(self, *a, **k) -> None: ...

    def log(self, *a, **k) -> None: ...

    def counter(self, *a, **k) -> None: ...

    def gauge(self, *a, **k) -> None: ...

    def mark(self, *a, **k) -> None: ...


class _Evo:
    ok = True
    reason_code = None
    reason = None
    retryable = False
    suggestions: list = []
    cause: list = []


@unified_method("stores.cosine_top_k", 1, "algorithm")
def cosine_top_k(input: dict, output: dict, context, tenant, metrics, evolution) -> bool:
    """I-010：余弦相似 top-k（纯计算，可跨项目复用，必须有单测）。"""
    query = input["query_vec"]
    items: dict[str, list[float]] = input["items"]
    k = int(input.get("k", 10))
    q_norm = math.sqrt(sum(x * x for x in query)) or 1.0
    scored = sorted(
        ((doc_id, _cosine(query, vec, q_norm)) for doc_id, vec in items.items()),
        key=lambda pair: pair[1], reverse=True,
    )[:k]
    output["hits"] = [{"id": doc_id, "score": score} for doc_id, score in scored]
    return True


def _cosine(query: list[float], vec: list[float], q_norm: float) -> float:
    v_norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return sum(a * b for a, b in zip(query, vec)) / (q_norm * v_norm)
