"""unified_method 装饰器：六段签名的 AOP 落地点（ADR-001 v2 / 选型 #7）。

统一签名：boolean step(input, output, context, tenant, metrics, evolution)
——框架层 tenant 入列（用户裁定 2026-09-27）与 SOP evolution 契约融合。
所有五段/六段方法经此注册：进出入计时与日志收集由框架统一完成（业务零埋点）；
类型化策略异常（policy.py）原样上抛，由容器 runner 的异常策略引擎处置。
"""
from __future__ import annotations

import functools
import time
from typing import Callable, TypeVar

from ..registry.registry import REGISTRY

F = TypeVar("F", bound=Callable[..., bool])


def unified_method(name: str, version: int = 1, category: str = "data") -> Callable[[F], F]:
    """注册六段步骤：boolean step(input, output, context, tenant, metrics, evolution)。"""

    def decorator(fn: F) -> F:
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            tenant, metrics, evolution = _segments(args, kwargs)
            metrics.enter(name)
            metrics.log("debug", f"{name} enter", {"trace_id": getattr(tenant, "trace_id", None)})
            try:
                ok = fn(*args, **kwargs)
            except BaseException as exc:
                metrics.exit(name, time.time(), False)
                metrics.log("error", f"{name} raise", {"error": type(exc).__name__})
                raise
            metrics.exit(name, time.time(), bool(ok))
            level = "info" if ok else "warn"
            metrics.log(level, f"{name} exit", {"ok": bool(ok), "reason": evolution.reason_code})
            return bool(ok)

        wrapper.step_name = name  # type: ignore[attr-defined]
        wrapper.step_version = version  # type: ignore[attr-defined]
        wrapper.step_category = category  # type: ignore[attr-defined]
        REGISTRY.register(wrapper)
        return wrapper  # type: ignore[return-value]

    return decorator


def _segments(args: tuple, kwargs: dict):
    """从调用形态提取 (tenant, metrics, evolution)。

    函数形态 6 参：(input, output, context, tenant, metrics, evolution)；
    绑定方法形态 7 参（首参 self）；其余按关键字段取（六段语义不变）。
    """
    if len(args) == 6:
        return args[3], args[4], args[5]
    if len(args) == 7:
        return args[4], args[5], args[6]
    return kwargs.get("tenant"), kwargs.get("metrics"), kwargs.get("evolution")
