"""Metrics 门面的默认实现（观测即功能，业务零打点代码）。

标准指标族（T-027，全量观测口径；OTel 接入时以本门面为落点换实现）：
- fw_step_duration{step}        节点进出入计时（AOP 自动写）
- fw_errors_total               类型化异常总数
- fw_retry_total / fw_dead_letter_total / fw_abstain_total / fw_default_path_total
- queue_depth{queue}            队列深度（gauge，组合根周期刷新）
- task_e2e_ms                   任务端到端时延（submit→终态，mark）
- llm_cost_tokens               LLM token 成本（provider 返回 tokens 累计）
"""
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

    # ---- 快照与导出（T-027：全指标视图；/v1/metrics 与控制台消费）----

    def snapshot(self) -> dict:
        return {
            "counters": dict(sorted(self.counters.items())),
            "gauges": dict(sorted(self.gauges.items())),
            "avg_duration_ms": {k: round(sum(v) / len(v) * 1000, 1)
                                for k, v in self.durations.items() if v},
            "p95_duration_ms": {k: round(_percentile(v, 0.95) * 1000, 1)
                                for k, v in self.durations.items() if v},
            "marks": len(self.marks),
            "logs": len(self.logs),
        }

    def prometheus(self) -> str:
        """Prometheus 文本格式导出（lite 档自省面；full 档换 OTel/Prometheus 栈）。"""
        lines: list[str] = []
        for name, value in sorted(self.counters.items()):
            lines.append(f"# TYPE {name} counter")
            lines.append(f"{name} {value}")
        for name, value in sorted(self.gauges.items()):
            lines.append(f"# TYPE {name} gauge")
            lines.append(f"{name} {value}")
        for name, values in sorted(self.durations.items()):
            if values:
                lines.append(f"# TYPE fw_step_duration_ms gauge")
                lines.append(f'fw_step_duration_ms{{step="{name}"}} '
                             f"{round(_percentile(values, 0.95) * 1000, 1)}")
        return "\n".join(lines) + ("\n" if lines else "")


def _percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
    return ordered[index]
