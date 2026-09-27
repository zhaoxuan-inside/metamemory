"""unified_method 装饰器：五段签名的 AOP 落地点（ADR-001 / 选型 #7）。

所有五段方法经此注册：框架在统一入口完成进出入计时与日志收集（系统观测全部
封装在 metrics，业务方法零埋点）；类型化策略异常（policy.py）原样上抛，由
容器 runner 的异常策略引擎处置，AOP 只负责记录后重抛。
"""
from __future__ import annotations

import functools
import time
from typing import Callable, TypeVar

from ..registry.registry import REGISTRY

F = TypeVar("F", bound=Callable[..., bool])


def unified_method(name: str, version: int = 1, category: str = "data") -> Callable[[F], F]:
    """注册五段步骤：boolean step(input, output, context, metrics, evolution)。"""

    def decorator(fn: F) -> F:
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            if len(args) == 5:  # 函数形态：fn(input, output, context, metrics, evolution)
                context, metrics, evolution = args[2], args[3], args[4]
            elif len(args) == 6:  # 绑定方法形态：fn(self, input, output, context, metrics, evolution)
                context, metrics, evolution = args[3], args[4], args[5]
            else:  # 关键字调用形态（五段语义不变）
                context = kwargs.get("context")
                metrics = kwargs.get("metrics")
                evolution = kwargs.get("evolution")
            metrics.enter(name)
            trace = getattr(getattr(context, "tenant", None), "trace_id", None)
            metrics.log("debug", f"{name} enter", {"trace_id": trace})
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
