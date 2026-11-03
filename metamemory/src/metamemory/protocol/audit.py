"""审计链（US-7）：append-only + prev_hash/record_hash 链式防篡改，全量不采样。

一切读取裁决（VisionArbiter 放行/拒绝）、治理动作（manage 四子动词）、授权变更、
遗忘级联都落本链；审计自身治理=链可验证（verify 全链重算）。append-only：
不提供 UPDATE/DELETE 面（真删除只发生在记忆资产，审计仅追加撤回/遗忘记录）。
"""
from __future__ import annotations

import hashlib
import json
import time

_GENESIS = "0" * 16


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())


def _canonical(record: dict) -> str:
    return json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class AuditLog:
    """审计日志（SQLite audit_log 表；PG 档月分区 + 同链语义）。"""

    def __init__(self, db) -> None:
        self.db = db

    def append(self, who: str, action: str, object_ref: str, tenant_id: str | None = None,
               authorization_ref: str | None = None, detail: dict | None = None) -> dict:
        with self.db.transaction() as conn:
            last = conn.execute(
                "SELECT record_hash FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
            prev_hash = last["record_hash"] if last else _GENESIS
            record = {
                "logged_at": _now(), "tenant_id": tenant_id, "who": who, "action": action,
                "object_ref": object_ref, "authorization_ref": authorization_ref,
                "detail": detail or {},
            }
            record_hash = hashlib.sha256((prev_hash + _canonical(record)).encode()).hexdigest()
            conn.execute(
                "INSERT INTO audit_log(logged_at, tenant_id, who, action, object_ref,"
                " authorization_ref, detail, prev_hash, record_hash) VALUES(?,?,?,?,?,?,?,?,?)",
                (record["logged_at"], tenant_id, who, action, object_ref,
                 authorization_ref, _canonical(record["detail"]), prev_hash, record_hash))
        return {"record_hash": record_hash, "prev_hash": prev_hash, "logged_at": record["logged_at"]}

    def verify(self) -> dict:
        """全链重算：任何一条被篡改（含删除中间记录）都会断链。"""
        rows = self.db.query(
            "SELECT id, logged_at, tenant_id, who, action, object_ref, authorization_ref,"
            " detail, prev_hash, record_hash FROM audit_log ORDER BY id")
        prev = _GENESIS
        for row in rows:
            record = {
                "logged_at": row["logged_at"], "tenant_id": row["tenant_id"], "who": row["who"],
                "action": row["action"], "object_ref": row["object_ref"],
                "authorization_ref": row["authorization_ref"],
                "detail": json.loads(row["detail"]) if row["detail"] else {},
            }
            expected = hashlib.sha256((prev + _canonical(record)).encode()).hexdigest()
            if row["prev_hash"] != prev or row["record_hash"] != expected:
                return {"ok": False, "checked": rows.index(row), "broken_at": row["id"],
                        "expected": expected, "found": row["record_hash"]}
            prev = row["record_hash"]
        return {"ok": True, "checked": len(rows), "broken_at": None}

    def tail(self, limit: int = 50, tenant_id: str | None = None,
             action_prefix: str | None = None) -> list[dict]:
        sql = ("SELECT id, logged_at, tenant_id, who, action, object_ref, authorization_ref,"
               " detail, record_hash FROM audit_log WHERE 1=1")
        params: list = []
        if tenant_id is not None:
            sql += " AND tenant_id=?"
            params.append(tenant_id)
        if action_prefix:
            sql += " AND action LIKE ?"
            params.append(f"{action_prefix}%")
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        return [dict(row) for row in self.db.query(sql, params)]
