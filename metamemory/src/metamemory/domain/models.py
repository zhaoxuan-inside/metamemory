"""领域数据模型（core.domain）：双时态原子 / 原文情境 / 切分 / 八类边 / 意图。

术语遵循 _09 词典：episode（原文片段）、entry（语义原子）、segment（单元切分）、
association_edge（关联边）、source_level（来源级别 S1-S4，禁与交付切片混用）。
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field


@dataclass
class MemoryEpisode:
    """原文片段（情境容器，证据层）：全量保底永不删除（硬遗忘除外）。"""

    episode_id: str
    tenant_id: str
    domain_id: str
    source_uri: str
    content: str
    actor: str
    occurred_at: str
    topics: list[str] = field(default_factory=list)

    def content_hash(self) -> str:
        """内容指纹（查重短路键）：同内容即重复，与来源 URI 无关。"""
        return hashlib.sha256(self.content.encode()).hexdigest()[:16]


@dataclass
class Segment:
    """单元切分记录（写侧第一决策，约束 13）：span + 边界依据 + 参数版本，可逆。"""

    segment_id: str
    episode_id: str
    text: str
    span: tuple[int, int]
    basis: str
    param_version: str


@dataclass
class MemoryEntry:
    """语义原子（记忆条目，资产层）：双时态 + supersedes + 引文锚点 + 置信。"""

    entry_id: str
    tenant_id: str
    domain_id: str
    subject: str
    predicate: str
    object_value: str
    entry_type: str
    valid_from: str
    valid_to: str | None = None
    learned_at: float = field(default_factory=time.time)
    supersedes: str | None = None
    anchor_episode: str = ""
    anchor_span: tuple[int, int] = (0, 0)
    source_level: str = "S2"
    confidence: float = 0.5
    visibility: str = "L1"
    strength: float = 1.0
    last_recall_at: float | None = None
    injected_suspect: bool = False
    embedding: list[float] | None = None    # 语义向量（网关 embeddings，落库 BLOB）
    embedding_model: str = ""
    gray_forgotten: bool = False            # 灰色遗忘降权（manage.forget gray，可恢复）
    stale: bool = False                     # 血缘回溯标记（纠错沿派生链下游重算，recompute 管道）


@dataclass
class AssociationEdge:
    """关联边（八类单表，血缘=type='derived'）：无依据不建边。"""

    edge_id: str
    edge_type: str
    src_id: str
    dst_id: str
    weight: float = 1.0
    confidence: float = 1.0
    evidence: dict = field(default_factory=dict)


@dataclass
class IntentRecord:
    """意图（唯一时效/可交易/需确认的记忆）：核心层只有字段与状态机位。"""

    entry_id: str
    intent_id: str = ""  # 缺省 = entry_id（意图与条目 1:1）
    state: str = "candidate"  # candidate|active|expired|completed
    ttl_expires_at: str | None = None
    visibility: str = "L1"
    expected_response: str = "solution"
    origin: str = "declared"  # declared 声明直生效 | agent_candidate Agent 只产候选
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


@dataclass
class AuthorizationRecord:
    """授权三元组：视野档位 × 使用目的 × 时效 + 允许目的集（公开面裁决谓词）。"""

    auth_id: str
    grantee: str
    object_ref: str
    level: str
    purpose: str
    allowed_purposes: list[str] = field(default_factory=list)
    expires_at: float | None = None
    status: str = "active"  # active|revoked|expired
    tenant_id: str = ""
    domain_id: str = ""
    created_at: float = field(default_factory=time.time)


@dataclass
class PublishedItem:
    """公开条目（L4 发布投影 / L5 公开）：生成与发布分离，72h 无痕撤回窗。"""

    item_id: str
    entry_id: str
    level: str
    allowed_purposes: list[str]
    review_status: str = "draft"
    published_at: float | None = None
    withdraw_deadline: float | None = None  # 72h 无痕撤回窗（§11.1-②）
    summary: str = ""                       # L4 抽象投影（生成/发布分离）
