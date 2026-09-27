"""五段签名支撑类型（ADR-001）。

统一签名：boolean method_name(input, output, context, metrics, evolution)
- input 只读；output 由方法填充（业务结果从 output 返回，返回值只表示执行成败）；
- context 运行环境（tenant 由 TenantCtx 承载，随 contextvar 贯穿）；
- metrics 系统观测门面（进出入计时/日志由框架 AOP 自动写，业务只写业务指标）；
- evolution 反馈与优化（方法结束前必须回填；false 时 reason_code 必填）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class TenantCtx:
    """数据主权硬边界：个人域 1:1 租户，组织实例 = 组织租户（A5）。"""

    tenant_id: str
    domain_id: str
    actor: str
    trace_id: str


@dataclass
class Context:
    """运行环境（方法内只读）：会话/配置/依赖句柄/事务/租户。"""

    session: dict[str, Any] = field(default_factory=dict)
    config: dict[str, Any] = field(default_factory=dict)
    deps: dict[str, Any] = field(default_factory=dict)
    tenant: TenantCtx | None = None
    tx: Any = None
    locale: dict[str, Any] = field(default_factory=dict)


class Metrics(Protocol):
    """系统观测门面：封装日志收集、进出入时间、计数等一切观测。

    enter/exit/log 由框架（unified_method AOP）自动调用，业务方法禁止手写；
    业务方法只写业务指标（counter/gauge/mark）。
    """

    def enter(self, method: str, at: float) -> None: ...

    def exit(self, method: str, at: float, ok: bool) -> None: ...

    def log(self, level: str, msg: str, fields: dict | None = None) -> None: ...

    def counter(self, name: str, n: int = 1) -> None: ...

    def gauge(self, name: str, value: float) -> None: ...

    def mark(self, key: str, value: Any) -> None: ...


@dataclass
class Evolution:
    """反馈与优化：方法结束前必须回填；跨层透传同一实例，cause 逐层合并。"""

    ok: bool = True
    reason_code: str | None = None
    reason: str | None = None
    retryable: bool = False
    suggestions: list[str] = field(default_factory=list)
    cause: list[dict[str, Any]] = field(default_factory=list)

    def fail(self, reason_code: str, reason: str, retryable: bool = False) -> None:
        self.ok = False
        self.reason_code = reason_code
        self.reason = reason
        self.retryable = retryable

    def push_cause(self, step: str, evolution: "Evolution") -> None:
        self.cause.append({"step": step, "ok": evolution.ok, "reason_code": evolution.reason_code})
