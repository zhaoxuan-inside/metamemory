"""MetaMemory 管理控制台（同级行项目，stdlib 唯一依赖）。

可视化配置：读写运行时配置（profile/quota/inference/recall/memory/governance），推理网关改配置即生效；
可视化追踪：任务/节点级状态机与输出、复核队列（abstain 汇口）、指标、检索验证、评测基线。
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

CONSOLE_ROOT = Path(__file__).resolve().parent
REPO = CONSOLE_ROOT.parent
for p in (str(REPO / "metamemory"), str(REPO / "metamemory" / "src"),
          str(REPO / "metamemory" / "tests")):
    if p not in sys.path:
        sys.path.insert(0, p)

import bootstrap  # noqa: E402
from metamemory.config import CONFIG_FILE, MetaMemoryConfig, discover_config_file, load_config  # noqa: E402
from metamemory_framework.adapters.inference import load_provider  # noqa: E402
from metamemory_framework.container.types import Evolution  # noqa: E402
from metamemory_framework.observe.metrics import MetricsImpl  # noqa: E402
from metamemory_framework.security.context import make_tenant, set_tenant  # noqa: E402


def _now_iso() -> str:
    """受理时间缺省=当下（世界时间锚点，ISO8601 带时区）。"""
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class ConsoleApp:
    """控制台应用：持有一个组合根实例，全部 API 围绕它读写。

    配置持久化落 config 文件（三分离·配置侧：与 load_config 的发现路径同一文件），
    数据面经组合根落 SQLite 数据目录（三分离·数据侧）；代码目录零数据零配置。
    """

    def __init__(self, config: MetaMemoryConfig | None = None,
                 config_file: Path | None = None, db_path: str | None = None) -> None:
        self.config_file = Path(config_file) if config_file else (
            discover_config_file() or CONFIG_FILE)
        self.composition = bootstrap.build_composition(config=config, db_path=db_path)
        self.config = self.composition_config()
        saved = self._load_saved_config()
        if saved:
            self.apply_config(saved, persist=False)

    def composition_config(self) -> MetaMemoryConfig:
        return MetaMemoryConfig.from_dict(self.composition["config"])

    def _load_saved_config(self) -> dict | None:
        if not self.config_file.is_file():
            return None
        try:
            return json.loads(self.config_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None

    def apply_config(self, data: dict, persist: bool = True) -> dict:
        """可视化配置落点：更新运行时配置与推理网关（改 base_url/model 即生效）。"""
        self.config = MetaMemoryConfig.from_dict({**self.config.to_dict(), **data})
        self.composition["config"] = self.config.to_dict()
        self.composition["inference"] = load_provider(self.config.to_dict()["inference"])
        if persist:
            self.config_file.parent.mkdir(parents=True, exist_ok=True)
            self.config_file.write_text(json.dumps(self.config.to_dict(), ensure_ascii=False, indent=2),
                                        encoding="utf-8")
        return self.config.to_dict()

    def state(self) -> dict:
        comp = self.composition
        store = comp["store"]
        engine = comp["engine"]
        m: MetricsImpl = comp.get("console_metrics") or MetricsImpl()
        durations = {k: round(sum(v) / len(v) * 1000, 1) for k, v in m.durations.items() if v}
        report = self._eval_report()
        return {
            "profile": comp["profile"],
            "config": self.config.to_dict(),
            "inference_configured": bool(self.config.inference.base_url and self.config.inference.model),
            "steps": [{"step": s, "version": v, "category": c} for s, v, c, _ in comp["registry"].entries()],
            "flows": {name: {"entry": p.entry, "nodes": len(p.nodes)} for name, p in comp["flows"].items()},
            "store": {"episodes": len(store.episodes), "entries": len(store.entries),
                      "edges": len(store.edges), "authz": len(store.authz),
                      "intents": len(store.intents),
                      "published": len(getattr(store, "published", {}))},
            "governance": {
                "audit_chain_ok": (self.composition["audit"].verify()["ok"]
                                   if self.composition.get("audit") else None),
                "metering": (self.composition["metering"].summary()
                             if self.composition.get("metering") else []),
                "intents_by_state": self._intents_by_state(store),
                "review_queue_journal": (self.composition["journal"].review_queue(limit=20)
                                         if self.composition.get("journal") else []),},
            "tasks": [{"task_id": tid, "status": t["status"],
                       "nodes": {n: s for n, s in t["cursor"].items()}}
                      for tid, t in list(engine.tasks.items())[-50:]],
            "review_queue": engine.review_queue[-50:],
            "metrics": {"counters": dict(sorted(m.counters.items())),
                        "avg_duration_ms": dict(sorted(durations.items()))},
            "eval": report,
        }

    @staticmethod
    def _intents_by_state(store) -> dict:
        counts: dict[str, int] = {}
        for intent in getattr(store, "intents", {}).values():
            counts[intent.state] = counts.get(intent.state, 0) + 1
        return counts

    @staticmethod
    def _eval_report() -> dict | None:
        path = REPO / "metamemory-eval" / "reports" / "last_report.json"
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None
        return {
            "generated_at": raw.get("generated_at"),
            "route_accuracy": raw.get("golden", {}).get("route_accuracy"),
            "recall_at5": raw.get("recall", {}).get("recall_at5"),
            "mrr": raw.get("recall", {}).get("mrr"),
            "no_answer_accuracy": raw.get("recall", {}).get("no_answer_accuracy"),
            "injection_block_rate": raw.get("injection", {}).get("block_rate"),
        }

    def _session(self):
        from api.app import build_context

        tenant = make_tenant("t_console", "d_console", "console")
        set_tenant(tenant)
        return (build_context(self.composition, tenant), tenant, MetricsImpl(), Evolution())

    def add_memory(self, body: dict) -> dict:
        """可视化追踪的数据入口：受理 → 同步驱动 ingest 流程 → 返回任务与产物。"""
        context, tenant, metrics, evolution = self._session()
        comp = self.composition
        out: dict = {}
        event = {"source_uri": body.get("source_uri") or f"console://{body.get('content', '')[:32]}",
                 "content": body.get("content", ""), "actor": body.get("actor", "console"),
                 "occurred_at": body.get("occurred_at") or _now_iso()}
        if not comp["protocol"]["add"]({"event": event}, out, context, tenant, metrics, evolution):
            return {"ok": False, "error": evolution.reason_code, "reason": evolution.reason}
        task_out: dict = {}
        comp["engine"].run_flow({"task_id": out["task_id"]}, task_out, context, tenant, metrics, evolution)
        return {"ok": True, "task_id": out["task_id"], "task_status": task_out.get("task_status"),
                "cursor": task_out.get("cursor"), "entries": len(comp["store"].entries)}

    def search(self, params: dict) -> dict:
        context, tenant, metrics, evolution = self._session()
        out: dict = {}
        ok = self.composition["protocol"]["search"](
            {"query": params.get("q", ""), "purpose": params.get("purpose"),
             "top_k": int(params.get("top_k", 10)), "intent": params.get("intent", "fact")},
            out, context, tenant, metrics, evolution)
        return {"ok": ok, **out}

    def manage(self, action: str, payload: dict) -> dict:
        """治理动作面板入口：与 REST 同一协议分派（correct/visibility/authz/forget）。"""
        from api.app import call_protocol

        out, meta = call_protocol(self.composition, f"manage_{action}", payload,
                                  {"authorization": ""})
        return {**meta, **out}

    def run_pipeline(self, name: str) -> dict:
        """巩固/重算流程触发（consolidate/recompute）。"""
        pipeline = self.composition["flows"].get(name)
        if pipeline is None:
            return {"ok": False, "error": "UNKNOWN_PIPELINE"}
        context, tenant, metrics, evolution = self._session()
        engine = self.composition["engine"]
        task_id = engine.submit(pipeline, params={}, tenant_id=tenant.tenant_id)
        result: dict = {}
        engine.run_flow({"task_id": task_id}, result, context, tenant, metrics, evolution)
        return {"ok": True, "task_id": task_id, "status": result.get("task_status")}

    def task_detail(self, task_id: str) -> dict:
        task = self.composition["engine"].tasks.get(task_id)
        if task is None:
            return {"error": "TASK_NOT_FOUND"}
        return {"task_id": task_id, "status": task["status"], "pipeline": task["pipeline"].name,
                "cursor": task["cursor"], "outputs": task["outputs"], "steps": task["steps"]}


def make_handler(app: ConsoleApp):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, data: dict | list, ctype: str = "application/json") -> None:
            body = json.dumps(data, ensure_ascii=False).encode("utf-8") if ctype == "application/json" else data
            self.send_response(code)
            self.send_header("Content-Type", f"{ctype}; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path in ("/", "/index.html"):
                return self._send(200, (CONSOLE_ROOT / "static" / "index.html").read_bytes(), "text/html")
            if parsed.path == "/api/state":
                return self._send(200, app.state())
            if parsed.path.startswith("/api/tasks/"):
                return self._send(200, app.task_detail(parsed.path.rsplit("/", 1)[-1]))
            if parsed.path == "/api/search":
                return self._send(200, app.search({k: v[0] for k, v in parse_qs(parsed.query).items()}))
            return self._send(404, {"error": "NOT_FOUND"})

        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            if self.path == "/api/config":
                return self._send(200, {"applied": True, "config": app.apply_config(body)})
            if self.path == "/api/memories":
                return self._send(200, app.add_memory(body))
            if self.path == "/api/manage":
                return self._send(200, app.manage(body.get("action", ""),
                                                  body.get("payload", {})))
            if self.path == "/api/pipelines":
                return self._send(200, app.run_pipeline(body.get("pipeline", "")))
            return self._send(404, {"error": "NOT_FOUND"})

        def log_message(self, *args):  # 静默访问日志
            pass

    return Handler


def serve(host: str = "127.0.0.1", port: int = 8100) -> ThreadingHTTPServer:
    app = ConsoleApp()
    server = ThreadingHTTPServer((host, port), make_handler(app))
    print(f"MetaMemory Console: http://{host}:{port} （profile={app.composition['profile']}，"
          f"推理网关{'已配置' if app.state()['inference_configured'] else '未配置——深提取将显式失败，请在「配置」页填写'}）")
    return server


def main() -> None:  # pragma: no cover
    import argparse

    parser = argparse.ArgumentParser(description="MetaMemory 管理控制台")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8100)
    args = parser.parse_args()
    serve(args.host, args.port).serve_forever()


if __name__ == "__main__":
    main()
