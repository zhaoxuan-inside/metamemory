"""Metrics 门面的默认实现（观测即功能，业务零打点代码）。"""
from __future__ import annotations

import time
from typing import Any

from ..container.types import Metrics


class MetricsImpl:
    """进程内 Metrics：记录进出入计时/日志/计数，供测试与 lite 档检视。"""

    def __init__(self) -> None:
        self._enter_at: dict[str, float] = {}
        self.durations: dict[str, list[float]] = {}
        self.logs: list[dict[str, Any]] = []
        self.counters: dict[str, int] = {}
        self.gauges: dict[str, float] = {}
        self.marks: list[dict[str, Any]] = []

    def enter(self, method: str, at: float | None = None) -> None:
        self._enter_at[method] = at if at is not None else time.time()

    def exit(self, method: str, at: float | None = None, ok: bool = True) -> None:
        start = self._enter_at.pop(method, None)
        elapsed = (at if at is not None else time.time()) - (start or (at if at is not None else time.time()))
        self.durations.setdefault(method, []).append(max(elapsed, 0.0))

    def log(self, level: str, msg: str, fields: dict | None = None) -> None:
        self.logs.append({"level": level, "msg": msg, "fields": fields or {}, "at": time.time()})

    def counter(self, name: str, n: int = 1) -> None:
        self.counters[name] = self.counters.get(name, 0) + n

    def gauge(self, name: str, value: float) -> None:
        self.gauges[name] = value

    def mark(self, key: str, value: Any) -> None:
        self.marks.append({"key": key, "value": value, "at": time.time()})

    def duration_of(self, method: str) -> float:
        values = self.durations.get(method) or [0.0]
        return values[-1]
