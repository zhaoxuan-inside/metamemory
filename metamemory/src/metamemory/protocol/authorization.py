"""授权三元组注册表（视野 × 目的 × 时效）：撤回幂等，到期惰性判定。"""
from __future__ import annotations

import time

from ..domain.models import AuthorizationRecord
from ..domain.store import MemoryStore


class AuthzRegistry:
    """授权记录的进程内承载（full 档由 authorization_records 表替换）。"""

    def __init__(self, store: MemoryStore) -> None:
        self.store = store

    def grant(self, grantee: str, object_ref: str, level: str, purpose: str,
              allowed_purposes: list[str] | None = None, ttl_seconds: int | None = None,
              tenant_id: str = "", domain_id: str = "") -> AuthorizationRecord:
        record = AuthorizationRecord(
            auth_id=self.store.next_id("auth"), grantee=grantee, object_ref=object_ref,
            level=level, purpose=purpose, allowed_purposes=allowed_purposes or [purpose],
            expires_at=time.time() + ttl_seconds if ttl_seconds else None,
            tenant_id=tenant_id, domain_id=domain_id,
        )
        return self.store.save_authz(record)

    def revoke(self, auth_id: str) -> bool:
        record = self.store.authz.get(auth_id)
        if record is None or record.status != "active":
            return False
        record.status = "revoked"  # 撤回幂等：停止未来检索
        self.store.save_authz(record)
        return True

    def active_for(self, grantee: str, object_ref: str) -> AuthorizationRecord | None:
        for record in self.store.authz.values():
            if record.grantee != grantee or record.status != "active":
                continue
            if record.object_ref not in (object_ref, "*"):
                continue
            if record.expires_at is not None and record.expires_at < time.time():
                record.status = "expired"  # 到期惰性判定
                self.store.save_authz(record)
                continue
            return record
        return None
