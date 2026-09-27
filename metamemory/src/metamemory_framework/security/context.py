"""租户上下文贯穿（A5/§九）：TenantCtx 存 contextvar，HTTP 中间件/任务参数双入口注入。"""
from __future__ import annotations

import contextvars
import uuid

from ..container.types import Context, TenantCtx

_current_tenant: contextvars.ContextVar[TenantCtx | None] = contextvars.ContextVar("tenant", default=None)


def set_tenant(tenant: TenantCtx) -> None:
    _current_tenant.set(tenant)


def get_tenant() -> TenantCtx | None:
    return _current_tenant.get()


def make_tenant(tenant_id: str, domain_id: str, actor: str) -> TenantCtx:
    return TenantCtx(tenant_id=tenant_id, domain_id=domain_id, actor=actor, trace_id=uuid.uuid4().hex)


def build_context(deps: dict | None = None, config: dict | None = None) -> Context:
    """组装 Context（框架入口豁免件）：tenant 取自 contextvar。"""
    return Context(deps=deps or {}, config=config or {}, tenant=get_tenant())
