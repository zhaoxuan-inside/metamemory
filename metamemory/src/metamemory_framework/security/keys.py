"""API Key 存储（_06 §1 认证面）：key 即查询者——key 的登记信息直接派生 TenantCtx。

存储只落 sha256(key)（泄露库不泄露 key）；明文仅创建时返回一次。首启无任何 key 时
放行为默认租户（US-8 lite 本地全闭环的零配置引导），一旦建 key 即强制认证。
infra 技术动作（豁免五段）。
"""
from __future__ import annotations

import hashlib
import secrets
import time

from ..stores.sqlite_impl import SqliteDb

DEFAULT_TENANT = ("t_default", "d_default", "anonymous")


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


class ApiKeyStore:
    """API Key 注册表（SQLite api_keys；full 档由网关 APISIX 承载，接口同构）。"""

    def __init__(self, db: SqliteDb) -> None:
        self.db = db

    def create(self, tenant_id: str, domain_id: str, actor: str, note: str = "") -> str:
        key = f"mm_{secrets.token_hex(20)}"
        self.db.execute(
            "INSERT INTO api_keys(key_hash, tenant_id, domain_id, actor, note, created_at)"
            " VALUES(?,?,?,?,?,?)",
            (_hash(key), tenant_id, domain_id, actor, note,
             time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())))
        return key  # 明文仅此一次

    def authenticate(self, key: str) -> tuple[str, str, str] | None:
        rows = self.db.query(
            "SELECT tenant_id, domain_id, actor FROM api_keys WHERE key_hash=?", (_hash(key),))
        if not rows:
            return None
        return (rows[0]["tenant_id"], rows[0]["domain_id"], rows[0]["actor"])

    def count(self) -> int:
        rows = self.db.query("SELECT COUNT(*) AS n FROM api_keys")
        return int(rows[0]["n"])

    def require_tenant(self, key: str | None) -> tuple[str, str, str]:
        """key 即查询者：有 key 验 key；库内无任何 key（零配置引导）放行默认租户。"""
        if key:
            record = self.authenticate(key)
            if record is None:
                raise PermissionError("AUTH-4010: 无效 API Key")
            return record
        if self.count() > 0:
            raise PermissionError("AUTH-4010: 缺少 API Key（本实例已启用认证）")
        return DEFAULT_TENANT
