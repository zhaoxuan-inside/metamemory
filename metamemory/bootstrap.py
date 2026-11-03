"""组合根（豁免五段签名）：装配框架 → 注册容器 → 装载流程 → serve。

lite 档：SQLite 持久化数据面（三分离·数据侧，var/ 目录）+ 内嵌执行器；
full 档替换槽位实现（PG 派发/RLS），接口不变。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from metamemory_framework.adapters.inference import load_provider
from metamemory_framework.engine.executor import GraphExecutor
from metamemory_framework.engine.graph import pipeline_from_dict, validate_graph
from metamemory_framework.registry.registry import REGISTRY

# 注册核心层步骤（import 即注册：unified_method 装饰器副作用）
from metamemory.ingest import extract, fastpath, organize, steps  # noqa: F401,E402
from metamemory.protocol import arbiter, facade  # noqa: F401,E402
from metamemory.query import express, fusion, recall  # noqa: F401,E402
from metamemory.config import MetaMemoryConfig, load_config  # noqa: E402
from metamemory.protocol.authorization import AuthzRegistry  # noqa: E402
from metamemory.protocol.facade import protocol_add, protocol_search  # noqa: E402

FLOWS_DIR = Path(__file__).parent / "flows" / "core"


def load_flows() -> dict:
    flows = {}
    for path in sorted(FLOWS_DIR.glob("*.json")):
        flows[path.stem] = pipeline_from_dict(json.loads(path.read_text(encoding="utf-8")))
    return flows


def build_composition(profile: str | None = None, config: MetaMemoryConfig | None = None,
                      db_path: str | None = None) -> dict:
    """装配组合根：lite 档单进程全闭环（US-8）。

    三分离接线：数据落 SQLite（db_path 可覆盖，测试传 ':memory:'）；框架槽位
    （journal/queue/call_cache/fts/graph/vector/objects）与核心仓储共享同一 SqliteDb；
    通知三路扇出（advance 落库 / progress 进程内 / webhook 按配置）。
    """
    cfg = config or load_config()
    if profile is not None:
        cfg.profile = profile
    from metamemory.domain.persist import PersistentMemoryStore
    from metamemory_framework.notify.notify import (
        NotifyFanout,
        OutboxNotifier,
        ProgressBus,
        WebhookDispatcher,
    )
    from metamemory_framework.queue.dispatch import SqliteTaskJournal
    from metamemory_framework.stores.sqlite_impl import (
        FsObjectStore,
        SqliteCallCache,
        SqliteDb,
        SqliteFullTextStore,
        SqliteGraphStore,
        SqliteLockStore,
        SqliteTaskQueue,
        SqliteVectorStore,
    )

    if db_path is None:
        db_path = str(cfg.storage.resolve_data_dir() / cfg.storage.db_filename)
    db = SqliteDb(db_path)  # 框架表；核心表由 PersistentMemoryStore 增量装载
    store = PersistentMemoryStore(db)
    progress_bus = ProgressBus()
    notify_cfg = cfg.to_dict().get("notify", {})
    webhook = WebhookDispatcher(db, url=notify_cfg.get("webhook_url", ""),
                                secret=notify_cfg.get("webhook_secret", ""),
                                max_attempts=int(notify_cfg.get("webhook_max_attempts", 5)),
                                timeout_seconds=int(notify_cfg.get("webhook_timeout_seconds", 10)))
    engine = GraphExecutor(REGISTRY, journal=SqliteTaskJournal(db),
                           notify=NotifyFanout(progress_bus, OutboxNotifier(db), webhook),
                           flow_provider=_flow_provider(cfg))
    composition = {
        "profile": cfg.profile,
        "db": db,
        "store": store,
        "authz": AuthzRegistry(store),
        "engine": engine,
        "inference": load_provider(cfg.to_dict()["inference"]),
        "call_cache": SqliteCallCache(db),
        "journal": engine.journal,
        "notify": {"bus": progress_bus, "outbox": engine.notify.sinks[1], "webhook": webhook},
        "queue": SqliteTaskQueue(db),
        "fts": SqliteFullTextStore(db),
        "graph_store": SqliteGraphStore(db),
        "vector_store": SqliteVectorStore(db),
        "lock_store": SqliteLockStore(db),
        "object_store": FsObjectStore(cfg.storage.resolve_data_dir() / "objects"),
        "flows": load_flows(),
        "registry": REGISTRY,
        "protocol": {"add": protocol_add, "search": protocol_search},
        "config": cfg.to_dict(),
        "db_path": db_path,
    }
    return composition


def _flow_provider(cfg: MetaMemoryConfig):
    """(name, version) → Pipeline：崩溃恢复时按日志记录重建流程对象。"""
    flows = load_flows()

    def provider(name: str, version: int):
        pipeline = flows.get(name)
        if pipeline is not None and pipeline.version == version:
            return pipeline
        return pipeline  # 版本不一致时回退当前版（流水版本升级的恢复语义，记审计）
        # 严格版：version 不匹配返回 None（任务挂起待人裁）——S2 治理切片引入

    return provider


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
