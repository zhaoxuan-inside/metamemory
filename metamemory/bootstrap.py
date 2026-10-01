"""组合根（豁免五段签名）：装配框架 → 注册容器 → 装载流程 → serve。

lite 档：进程内 Store 降级实现 + 内嵌执行器；full 档替换槽位实现，接口不变。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from metamemory_framework.adapters.inference import load_provider
from metamemory_framework.engine.executor import GraphExecutor
from metamemory_framework.engine.graph import pipeline_from_dict, validate_graph
from metamemory_framework.recovery.cache import CallCache
from metamemory_framework.registry.registry import REGISTRY

# 注册核心层步骤（import 即注册：unified_method 装饰器副作用）
from metamemory.ingest import extract, fastpath, organize, steps  # noqa: F401,E402
from metamemory.protocol import arbiter, facade  # noqa: F401,E402
from metamemory.query import express, fusion, recall  # noqa: F401,E402
from metamemory.config import MetaMemoryConfig, load_config  # noqa: E402
from metamemory.domain.store import MemoryStore  # noqa: E402
from metamemory.protocol.authorization import AuthzRegistry  # noqa: E402
from metamemory.protocol.facade import protocol_add, protocol_search  # noqa: E402

FLOWS_DIR = Path(__file__).parent / "flows" / "core"


def load_flows() -> dict:
    flows = {}
    for path in sorted(FLOWS_DIR.glob("*.json")):
        flows[path.stem] = pipeline_from_dict(json.loads(path.read_text(encoding="utf-8")))
    return flows


def build_composition(profile: str | None = None, config: MetaMemoryConfig | None = None) -> dict:
    """装配组合根：lite 档单进程全闭环（US-8）；配置默认经 load_config（环境变量可覆盖）。"""
    cfg = config or load_config()
    if profile is not None:
        cfg.profile = profile
    store = MemoryStore()
    flows = load_flows()
    engine = GraphExecutor(REGISTRY)
    composition = {
        "profile": cfg.profile,
        "store": store,
        "authz": AuthzRegistry(store),
        "engine": engine,
        "inference": load_provider(cfg.to_dict()["inference"]),
        "call_cache": CallCache(),
        "flows": flows,
        "registry": REGISTRY,
        "protocol": {"add": protocol_add, "search": protocol_search},
        "config": cfg.to_dict(),
    }
    return composition


def validate_flows(composition: dict) -> dict:
    """编译期校验（ingest 随注册步骤过注册检查；S1/S2 流程仅结构校验）。"""
    results = {}
    for name, pipeline in composition["flows"].items():
        out: dict = {}
        validate_graph({"pipeline": pipeline}, out, None, None, _Quiet(), _QuietEvo())
        results[name] = out
    return results


class _Quiet:
    def enter(self, *a, **k) -> None: ...

    def exit(self, *a, **k) -> None: ...

    def log(self, *a, **k) -> None: ...

    def counter(self, *a, **k) -> None: ...

    def gauge(self, *a, **k) -> None: ...

    def mark(self, *a, **k) -> None: ...


class _QuietEvo:
    ok = True
    reason_code = None
    reason = None
    retryable = False
    suggestions: list = []
    cause: list = []


def main() -> None:  # pragma: no cover
    composition = build_composition()
    results = validate_flows(composition)
    for name, result in results.items():
        print(f"flow {name}: {'ok' if result.get('ok') else result.get('errors')}")
    try:
        from api.app import create_app

        app = create_app(composition)
        if app is not None:
            import uvicorn

            uvicorn.run(app, host="127.0.0.1", port=8000)
            return
    except ImportError:
        pass
    print("fastapi 未安装：lite 内嵌模式（组合根装配完成，协议面见 _06/01）")


if __name__ == "__main__":
    main()
