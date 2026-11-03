"""计量横切（公理 7：写免费读收费，关于自己永远免费；配额即 COGS 上界）。

维度口径（metering_events.dimension）：storage.events（写入事件）/ search.served（检索
服务次数）/ llm.tokens（推理 token 成本）/ intent.active_slots（生效意图发现位）。
聚合面供配额判定与对账（M4 计量对账的基础）。
"""
from __future__ import annotations

import time


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())


class MeteringLedger:
    """计量事件账本（SQLite metering_events；PG 档月分区）。"""

    def __init__(self, db) -> None:
        self.db = db

    def record(self, tenant_id: str, dimension: str, quantity: float, source: str = "") -> None:
        self.db.execute(
            "INSERT INTO metering_events(tenant_id, dimension, quantity, source, created_at)"
            " VALUES(?,?,?,?,?)", (tenant_id, dimension, float(quantity), source, _now()))

    def summary(self, tenant_id: str | None = None) -> list[dict]:
        sql = ("SELECT tenant_id, dimension, COUNT(*) AS events, SUM(quantity) AS total"
               " FROM metering_events")
        params: list = []
        if tenant_id is not None:
            sql += " WHERE tenant_id=?"
            params.append(tenant_id)
        sql += " GROUP BY tenant_id, dimension ORDER BY tenant_id, dimension"
        return [dict(row) for row in self.db.query(sql, params)]

    def total(self, tenant_id: str, dimension: str) -> float:
        rows = self.db.query(
            "SELECT COALESCE(SUM(quantity),0) AS total FROM metering_events"
            " WHERE tenant_id=? AND dimension=?", (tenant_id, dimension))
        return float(rows[0]["total"])
