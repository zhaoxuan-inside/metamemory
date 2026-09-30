"""外部调用缓存（A7 崩溃恢复三件套之三 / 框架详细设计 §4.4）。

键 = (node_id, input_hash, prompt_version)：命中直接回填、不重复付费；
full 档落 llm_calls 表（UNIQUE(node_id, input_hash, prompt_version)），lite 档进程内。
"""
from __future__ import annotations

import hashlib
import json
import time
from typing import Any


class CallCache:
    """进程内调用缓存（infra 技术动作，豁免五段；llm_calls 表的 lite 等价物）。"""

    def __init__(self, capacity: int = 4096) -> None:
        self._store: dict[tuple[str, str, str], dict[str, Any]] = {}
        self._capacity = capacity
        self.hits = 0
        self.misses = 0

    @staticmethod
    def input_hash(payload: Any) -> str:
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(canonical.encode()).hexdigest()[:16]

    def key(self, node_id: str, input_hash: str, prompt_version: str) -> tuple[str, str, str]:
        return (node_id, input_hash, prompt_version)

    def lookup(self, node_id: str, input_hash: str, prompt_version: str) -> dict | None:
        hit = self._store.get(self.key(node_id, input_hash, prompt_version))
        if hit is None:
            self.misses += 1
            return None
        self.hits += 1
        return hit

    def store(self, node_id: str, input_hash: str, prompt_version: str, response: dict) -> None:
        if len(self._store) >= self._capacity:
            self._store.pop(next(iter(self._store)))
        self._store[self.key(node_id, input_hash, prompt_version)] = {**response, "cached_at": time.time()}
