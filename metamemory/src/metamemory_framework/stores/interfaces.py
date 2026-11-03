"""Store 接口族 9 槽位（A10 / ADR-004）：一切外部组件的唯一接口面。

部署档位 = 槽位预设；允许缺失降级（DataStore/FullText/Graph/Object 为必需槽位）。
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class DataStore(Protocol):
    """必需槽位：PG（full）/ SQLite（lite）。"""

    def execute(self, sql: str, params: dict | None = None) -> list[dict]: ...

    def transaction(self) -> Any: ...


@runtime_checkable
class VectorStore(Protocol):
    def upsert(self, ns: str, vecs: list[tuple[str, list[float]]]) -> None: ...

    def search(self, ns: str, query: list[float], k: int) -> list[dict]: ...


@runtime_checkable
class FullTextStore(Protocol):
    """必需槽位：PG FTS+zhparser / SQLite FTS5。"""

    def index(self, ns: str, doc_id: str, text: str) -> None: ...

    def search(self, ns: str, query: str, k: int) -> list[dict]: ...


@runtime_checkable
class GraphStore(Protocol):
    """必需槽位：邻接表 + 递归 CTE。"""

    def upsert_edge(self, src: str, dst: str, edge_type: str, props: dict) -> None: ...

    def walk(self, src: str, edge_types: list[str], depth: int) -> list[dict]: ...


@runtime_checkable
class TaskQueue(Protocol):
    def enqueue(self, queue: str, item: dict) -> str: ...

    def poll(self, queue: str, limit: int = 1) -> list[dict]: ...


@runtime_checkable
class CacheStore(Protocol):
    def get(self, key: str) -> Any: ...

    def set(self, key: str, value: Any, ttl_seconds: int | None = None) -> None: ...

    def invalidate_tag(self, tag: str) -> None: ...


@runtime_checkable
class LockStore(Protocol):
    def acquire(self, key: str, ttl_seconds: int) -> bool: ...

    def release(self, key: str) -> None: ...


@runtime_checkable
class ObjectStore(Protocol):
    """必需槽位：MinIO / 本地 FS。"""

    def put(self, bucket: str, key: str, data: bytes) -> None: ...

    def get(self, bucket: str, key: str) -> bytes: ...


@runtime_checkable
class PubSubStore(Protocol):
    def publish(self, channel: str, event: dict) -> None: ...

    def subscribe(self, channel: str, callback) -> None: ...
