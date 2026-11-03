"""REST 受理面（豁免五段签名，_00.conventions.signature_exemptions）。

路由全量（_06/01）：add(202+task_id) / search（fused|grep NDJSON + verify 精确验证）/
context / manage.{correct,visibility,authz,forget} / intents（declare+confirm-card）/
memories/{id} + history / tasks/{id}/events(SSE) / audit / metering / metrics / mcp。
认证：Authorization: Bearer <api-key>（key 即查询者；零配置引导期放行默认租户）。
FastAPI 为可选依赖；未安装时 create_app 返回 None（lite CLI 模式）。
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

try:
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.responses import PlainTextResponse, Response, StreamingResponse
except ImportError:  # pragma: no cover - 骨架环境允许无 fastapi
    FastAPI = None
    HTTPException = None

from metamemory_framework.container.types import Context, Evolution  # noqa: E402
from metamemory_framework.observe.metrics import MetricsImpl  # noqa: E402
from metamemory_framework.security.context import make_tenant, set_tenant  # noqa: E402


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def build_context(composition: dict, tenant) -> Context:
    return Context(
        deps={"store": composition["store"], "authz": composition["authz"],
              "engine": composition["engine"], "inference": composition["inference"],
              "call_cache": composition["call_cache"], "flows": composition["flows"],
              "audit": composition.get("audit"), "metering": composition.get("metering"),
              "confirm_signer": composition.get("confirm_signer"),
              "fts": composition.get("fts"), "vector_store": composition.get("vector_store"),
              "graph_store": composition.get("graph_store")},
        config=composition.get("config", {}))


def call_protocol(composition: dict, verb: str, payload: dict, headers: dict) -> tuple[dict, dict]:
    """协议分派（REST 与 MCP 共用）：key 即查询者 → 六段实参组装 → 同步执行。"""
    import importlib

    tenant_ids = _tenant_from_headers(composition, headers)
    tenant = make_tenant(*tenant_ids)
    set_tenant(tenant)
    context = build_context(composition, tenant)
    metrics, evolution, output = MetricsImpl(), Evolution(), {}
    if verb in ("add", "search"):
        method = composition["protocol"][verb]
    else:
        module_name = "governance" if verb.startswith("manage_") else "intents"
        module = importlib.import_module(f"metamemory.protocol.{module_name}")
        method = getattr(module, verb)
    ok = method(payload, output, context, tenant, metrics, evolution)
    meta = {"ok": ok, "reason_code": evolution.reason_code,
            "reason": evolution.reason, "retryable": evolution.retryable}
    if not ok:
        output = {"error": evolution.reason_code, "reason": evolution.reason,
                  "cause": evolution.cause}
    return output, meta


def _tenant_from_headers(composition: dict, headers: dict) -> tuple[str, str, str]:
    key = headers.get("authorization", "").removeprefix("Bearer ").strip() \
        or headers.get("x-api-key", "").strip()
    try:
        return composition["keys"].require_tenant(key or None)
    except PermissionError as exc:
        raise AuthError(str(exc)) from exc


class AuthError(Exception):
    """认证失败（AUTH-4010）。"""


def create_app(composition: dict):
    """构建 FastAPI 应用；核心逻辑全部下沉为六段方法，handler 只做适配。"""
    if FastAPI is None:
        return None
    app = FastAPI(title="MetaMemory", version="0.1.0")

    @app.exception_handler(AuthError)
    async def _auth_error(request: Request, exc: AuthError):  # noqa: ANN202
        return _json({"error": "AUTH-4010", "reason": str(exc)}, status=401)

    def _headers(request: Request) -> dict:
        return {"authorization": request.headers.get("authorization", ""),
                "x-api-key": request.headers.get("x-api-key", "")}

    def _json(data, status: int = 200) -> Response:
        from fastapi.responses import JSONResponse

        return JSONResponse(content=data, status_code=status)

    def _run(verb: str, payload: dict, request: Request, ok_status: int = 200):
        out, meta = call_protocol(composition, verb, payload, _headers(request))
        if not meta["ok"]:
            raise HTTPException(status_code=_status(meta["reason_code"]), detail=meta)
        return _json(out, status=ok_status)

    # ---- API-001 add（任务式受理：202 + task_id + SSE；worker 异步执行）----

    @app.post("/v1/memories", status_code=202)
    def add_memory(body: dict, request: Request):
        return _run("add", {"event": body}, request, ok_status=202)

    # ---- API-003 search（fused / grep NDJSON；verify 精确验证）----

    @app.post("/v1/search")
    def search(body: dict, request: Request):
        out, meta = call_protocol(composition, "search", body, _headers(request))
        if not meta["ok"]:
            raise HTTPException(status_code=_status(meta["reason_code"]), detail=meta)
        if body.get("mode") == "grep":  # grep 兼容输出（NDJSON，行结构=稳定契约）
            lines = [json.dumps({k: item.get(k) for k in
                                 ("memory_id", "content", "span", "score", "confidence",
                                  "source_level", "anchor_episode")}, ensure_ascii=False)
                     for item in out.get("items", [])]
            return Response("\n".join(lines) + ("\n" if lines else ""),
                            media_type="application/x-ndjson")
        return _json(out)

    # ---- API-005 context（可选动词）----

    @app.post("/v1/context")
    def context_verb(body: dict, request: Request):
        return _run("search", {"query": body.get("query", ""),
                               "token_budget": int(body.get("token_budget", 2048))}, request)

    # ---- API-006/007 单条读取与双时态历史 ----

    @app.get("/v1/memories/{memory_id}")
    def get_memory(memory_id: str, request: Request):
        _guard(composition, _headers(request))
        entry = composition["store"].entries.get(memory_id)
        if entry is None:
            raise HTTPException(status_code=404, detail="NOT_FOUND")
        return _json({"memory_id": entry.entry_id, "content": entry.object_value,
                      "subject": entry.subject, "predicate": entry.predicate,
                      "confidence": entry.confidence, "source_level": entry.source_level,
                      "visibility": entry.visibility, "strength": entry.strength,
                      "gray_forgotten": entry.gray_forgotten,
                      "anchor": {"episode": entry.anchor_episode, "span": list(entry.anchor_span)},
                      "valid": {"from": entry.valid_from, "to": entry.valid_to},
                      "learned_at": entry.learned_at})

    @app.get("/v1/memories/{memory_id}/history")
    def memory_history(memory_id: str, request: Request):
        _guard(composition, _headers(request))
        store = composition["store"]
        if memory_id not in store.entries:
            raise HTTPException(status_code=404, detail="NOT_FOUND")
        # supersedes 指针指向更旧：先向前找最新版本，再沿链回放（最新在前）
        newest = memory_id
        guard = 0
        while guard < 1000:
            newer = next((e.entry_id for e in store.entries.values()
                          if e.supersedes == newest), None)
            if newer is None:
                break
            newest = newer
            guard += 1
        chain, current = [], newest
        while current:
            entry = store.entries.get(current)
            if entry is None:
                break
            chain.append({"memory_id": entry.entry_id, "content": entry.object_value,
                          "valid_from": entry.valid_from, "valid_to": entry.valid_to,
                          "learned_at": entry.learned_at, "source_level": entry.source_level})
            current = entry.supersedes
        return _json({"memory_id": memory_id, "chain": chain})  # supersedes 链（双时态历史）

    # ---- API-008~011 manage 四子动词 ----

    @app.post("/v1/manage/{action}")
    def manage(action: str, body: dict, request: Request):
        if action not in ("correct", "visibility", "authz", "forget"):
            raise HTTPException(status_code=404, detail="UNKNOWN_MANAGE_ACTION")
        return _run(f"manage_{action}", body, request)

    # ---- API-013/014 意图（声明直生效 / Agent 候选 + 确认卡片）----

    @app.post("/v1/intents")
    def declare_intent(body: dict, request: Request):
        return _run("intent_declare", body, request)

    @app.get("/v1/intents/{intent_id}/confirm-card")
    def confirm_card(intent_id: str, request: Request):
        _guard(composition, _headers(request))
        intent = composition["store"].get_intent(intent_id)
        if intent is None:
            raise HTTPException(status_code=404, detail="NOT_FOUND")
        entry = composition["store"].entries.get(intent.entry_id)
        return _json({"intent_id": intent_id, "state": intent.state,
                      "content": entry.object_value if entry else "",
                      "visibility": intent.visibility, "ttl_expires_at": intent.ttl_expires_at,
                      "origin": intent.origin,
                      "expected_response": intent.expected_response})

    @app.post("/v1/intents/{intent_id}/confirm")
    def confirm_intent(intent_id: str, body: dict, request: Request):
        return _run("intent_confirm", {"intent_id": intent_id,
                                       "decision": body.get("decision", "confirm")}, request)

    # ---- API-012 SSE 进度订阅 ----

    @app.get("/v1/tasks/{task_id}/events")
    def task_events(task_id: str, request: Request):
        import threading as _threading

        bus = composition["notify"]["bus"]

        def stream():
            seen = set()
            for event in reversed(bus.recent(task_id=task_id)):  # 最近窗口补发
                seen.add((event.get("node_id"), event.get("status"), event.get("at")))
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            queue: list[dict] = []
            done = _threading.Event()

            def callback(event: dict):
                if event.get("task_id") == task_id:
                    queue.append(event)
                    if event.get("status") in ("succeeded", "failed", "partial"):
                        done.set()

            bus.subscribe(callback)
            import time as _time

            deadline = _time.time() + 300  # SSE 单连接上限 5min（重连续订）
            while not done.is_set() and _time.time() < deadline:
                if queue:
                    event = queue.pop(0)
                    key = (event.get("node_id"), event.get("status"), event.get("at"))
                    if key in seen:
                        continue
                    seen.add(key)
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                else:
                    _time.sleep(0.1)
            yield f"event: done\ndata: {json.dumps({'task_id': task_id})}\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream")

    # ---- 任务面：运行流程（consolidate/recompute 手动触发）与查询 ----

    @app.post("/v1/tasks")
    def run_pipeline(body: dict, request: Request):
        pipeline_name = body.get("pipeline", "")
        pipeline = composition["flows"].get(pipeline_name)
        if pipeline is None:
            raise HTTPException(status_code=404, detail="UNKNOWN_PIPELINE")
        engine = composition["engine"]
        tenant = make_tenant(*_tenant_from_headers(composition, _headers(request)))
        task_id = engine.submit(pipeline, params=body.get("params") or {},
                                tenant_id=tenant.tenant_id)
        result: dict = {}
        engine.run_flow({"task_id": task_id}, result, build_context(composition, tenant),
                        tenant, MetricsImpl(), Evolution())
        return _json({"task_id": task_id, "status": result.get("task_status"),
                      "outputs": result.get("outputs")})

    @app.get("/v1/tasks/{task_id}")
    def task_detail(task_id: str, request: Request):
        _guard(composition, _headers(request))
        record = composition["journal"].load_task(task_id)
        if record is None:
            raise HTTPException(status_code=404, detail="NOT_FOUND")
        return _json(record)

    # ---- 审计/计量/观测 ----

    @app.get("/v1/audit/verify")
    def audit_verify(request: Request):
        return _json(composition["audit"].verify())

    @app.get("/v1/audit")
    def audit_tail(request: Request, limit: int = 50, action: str | None = None):
        _guard(composition, _headers(request))
        return _json({"records": composition["audit"].tail(limit=limit, action_prefix=action)})

    @app.get("/v1/metering/summary")
    def metering_summary(request: Request, tenant: str | None = None):
        _guard(composition, _headers(request))
        return _json({"summary": composition["metering"].summary(tenant)})

    @app.get("/v1/metrics")
    def metrics_endpoint():
        composition.setdefault("console_metrics", MetricsImpl())
        return PlainTextResponse(composition["console_metrics"].prometheus(),
                                 media_type="text/plain; version=0.0.4")

    # ---- MCP-001 工具面 ----

    from .mcp import handle_mcp

    @app.post("/mcp")
    async def mcp_endpoint(request: Request):
        raw = await request.body()
        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="INVALID_JSON")
        return _json(handle_mcp(composition, body, _headers(request)))

    return app


def _guard(composition: dict, headers: dict) -> None:
    _tenant_from_headers(composition, headers)  # 认证失败抛 AuthError


def _status(reason_code: str) -> int:
    return {"QUOTA_EXCEEDED": 429, "VALID_FAILED": 400, "IDEM_CONFLICT": 409,
            "NOT_FOUND": 404, "TASK_NOT_FOUND": 404, "TASK_LEASED": 409,
            "PROVIDER_MISSING": 503}.get(reason_code, 500)
