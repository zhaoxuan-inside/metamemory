"""步骤注册表：name + version 注册，多版本并存（框架职责 1/7，A13）。"""
from __future__ import annotations

from typing import Any, Callable

FiveSeg = Callable[..., bool]


class StepRegistry:
    """组件容器注册表：key=(name, version)，latest 指向最高版本。"""

    def __init__(self) -> None:
        self._steps: dict[tuple[str, int], FiveSeg] = {}
        self._latest: dict[str, int] = {}

    def register(self, fn: FiveSeg) -> FiveSeg:
        name = getattr(fn, "step_name", None)
        version = getattr(fn, "step_version", 1)
        if not name:
            raise ValueError("register() 需要 unified_method 装饰的步骤")
        key = (name, version)
        if key in self._steps and self._steps[key] is not fn:
            # 同键重注册（热更新/测试重定义）：新实现覆盖旧实现
            self._steps[f"{name}@{version}.deprecated:{id(self._steps[key])}"] = self._steps[key]
        self._steps[key] = fn
        self._latest[name] = max(version, self._latest.get(name, 0))
        return fn

    def lookup(self, name: str, version: int | None = None) -> FiveSeg | None:
        resolved = version if version is not None else self._latest.get(name)
        if resolved is None:
            return None
        return self._steps.get((name, resolved))

    def names(self) -> list[str]:
        return sorted(self._latest)

    def entries(self) -> list[tuple[str, int, str, str]]:
        """活注册项：(step_name, version, category, func_name)——供索引与审计。"""
        return [
            (name, version, getattr(fn, "step_category", "data"), fn.__name__)
            for (name, version), fn in self._steps.items()
        ]

    def dump(self) -> dict[str, Any]:
        live = {key: fn for key, fn in self._steps.items() if isinstance(key, tuple)}
        return {
            f"{name}@{version}": getattr(fn, "step_category", "data")
            for (name, version), fn in sorted(live.items())
        }


REGISTRY = StepRegistry()
