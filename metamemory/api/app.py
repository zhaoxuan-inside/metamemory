"""REST 受理面（豁免五段签名，_00.conventions.signature_exemptions）：

POST /v1/memories（202+task_id）/search /context、GET /v1/memories/{id}、
manage 子动作路由。FastAPI 为可选依赖；未安装时 create_app 返回 None（lite CLI 模式）。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from metamemory_framework.security.context import make_tenant, set_tenant  # noqa: E402

try:
    from fastapi import FastAPI, HTTPException
except ImportError:  # pragma: no cover - 骨架环境允许无 fastapi
    FastAPI = None
    HTTPException = None


def create_app(composition: dict):
    """构建 FastAPI 应用；核心逻辑全部下沉为五段方法，handler 只做适配。"""
    if FastAPI is None:
        return None
    app = FastAPI(title="MetaMemory", version="0.1.0")
    protocol = composition["protocol"]

    @app.post("/v1/memories", status_code=202)
    def add_memory(body: dict):
        out, evo = _call(protocol["add"], {"event": body}, composition, body)
        if not out.get("task_id") and evo.get("reason_code"):
            raise HTTPException(status_code=_status(evo["reason_code"]), detail=evo)
        return out

    @app.post("/v1/search")
    def search(body: dict):
        out, evo = _call(protocol["search"], body, composition, body)
        if evo.get("reason_code"):
            raise HTTPException(status_code=_status(evo["reason_code"]), detail=evo)
        return out

    @app.get("/v1/memories/{memory_id}")
    def get_memory(memory_id: str):
        entry = composition["store"].entries.get(memory_id)
        if entry is None:
            raise HTTPException(status_code=404, detail="NOT_FOUND")
        return {"memory_id": entry.entry_id, "content": entry.object_value,
                "confidence": entry.confidence, "source_level": entry.source_level,
                "visibility": entry.visibility, "anchor": {"episode": entry.anchor_episode,
                                                           "span": list(entry.anchor_span)}}

    @app.post("/v1/manage/{action}")
    def manage(action: str, body: dict):
        handler = protocol.get(f"manage_{action}")
        if handler is None:
            raise HTTPException(status_code=404, detail="UNKNOWN_MANAGE_ACTION")
        out, evo = _call(handler, body, composition, body)
        if evo.get("reason_code"):
            raise HTTPException(status_code=_status(evo["reason_code"]), detail=evo)
        return out or {"accepted": True}

    composition["protocol"] = protocol
    return app


def _call(method, payload: dict, composition: dict, headers_src: dict):
    """组装五段实参并同步执行（full 档受理改异步任务式，契约不变）。"""
    from metamemory_framework.container.types import Evolution
    from metamemory_framework.observe.metrics import MetricsImpl

    tenant = make_tenant(headers_src.get("tenant_id", "t_default"),
                         headers_src.get("domain_id", "d_default"),
                         headers_src.get("actor", "anonymous"))
    set_tenant(tenant)
    context = _context(composition, tenant)
    metrics, evolution, output = MetricsImpl(), Evolution(), {}
    ok = method(payload, output, context, metrics, evolution)
    return (output if ok else {}) | ({} if ok else {"error": evolution.reason_code}), {
        "ok": ok, "reason_code": evolution.reason_code, "reason": evolution.reason,
    }


def _context(composition: dict, tenant):
    from metamemory_framework.container.types import Context

    return Context(
        deps={"store": composition["store"], "authz": composition["authz"],
              "engine": composition["engine"], "inference": composition["inference"],
              "flows": composition["flows"]},
        config=composition.get("config", {}), tenant=tenant,
    )


def _status(reason_code: str) -> int:
    return {"QUOTA_EXCEEDED": 429, "VALID_FAILED": 400, "IDEM_CONFLICT": 409}.get(reason_code, 500)
