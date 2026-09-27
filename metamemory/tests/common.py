"""测试公共件：sys.path 引导 + 五段支撑类型快捷构造。"""
from __future__ import annotations

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
CODE_ROOT = Path(__file__).resolve().parent.parent
for path in (str(SRC), str(CODE_ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

from metamemory_framework.container.types import Context, Evolution, TenantCtx  # noqa: E402
from metamemory_framework.observe.metrics import MetricsImpl  # noqa: E402
from metamemory.domain.store import MemoryStore  # noqa: E402
from metamemory.protocol.authorization import AuthzRegistry  # noqa: E402


def make_context(store: MemoryStore | None = None, deps: dict | None = None) -> Context:
    tenant = TenantCtx(tenant_id="t1", domain_id="d1", actor="alice", trace_id="trace-1")
    base = {"store": store or MemoryStore(), "authz": None}
    if deps:
        base.update(deps)
    if base["authz"] is None:
        base["authz"] = AuthzRegistry(base["store"])
    return Context(deps=base, config={}, tenant=tenant)


def stubs(store: MemoryStore | None = None, deps: dict | None = None):
    return make_context(store, deps), MetricsImpl(), Evolution()
