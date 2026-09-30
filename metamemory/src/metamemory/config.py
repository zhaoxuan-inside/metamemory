"""MetaMemory 配置面（骨架期 dataclass + 环境变量；full 档替换为 pydantic-settings）。

默认值全部取自文档族裁定：再巩固窗口 72h（约束 11）、RRF k=60（附录 C）、
免费档发现位 3（G3 口径）、审计保留下限、快通道承诺 <100ms、四态路由阈值。
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

PROFILES = ("full", "lite", "minimal")  # 档位=槽位预设（ADR-004）


@dataclass
class QuotaConfig:
    max_pending_events: int = 10_000      # 写入配额封顶（COGS 上界执行件）
    discovery_slots_free: int = 3         # 免费档意图发现位（意图本体不限量，G3）
    discovery_slots_pro: int | None = None  # Pro 不限


@dataclass
class RecallConfig:
    top_k: int = 10                       # search 默认返回条数（≤50）
    token_budget: int = 2048              # express 组装预算
    rrf_k: int = 60                       # RRF 融合常数（技术词典）
    rerank_enabled: bool = False          # lite 档默认关（C-G 可降档）
    default_intent: str = "fact"          # 检索意图三分默认档（R5）


@dataclass
class MemoryConfig:
    reconsolidation_window_hours: int = 72        # 再巩固窗口（约束 11，默认 72h）
    segment_param_version: str = "seg-v0"         # 切分参数版本（演化参数，约束 13）
    worthiness_high: int = 3                      # 五维信号 high 阈值（提案值，演化闭环）
    worthiness_mid: int = 1
    low_confidence_floor: float = 0.3             # 低置信隔离阈值（提案值）


@dataclass
class GovernanceConfig:
    audit_retention_domain_days: int = 180        # 域内自读保留（配置化，下限硬约束不可缩短）
    review_queue_enabled: bool = True             # abstain 挂人裁汇口（ADR-003）
    purpose_gate_enforced: bool = True            # 公开层目的闸（约束 16，本期必做）


@dataclass
class MetaMemoryConfig:
    profile: str = "lite"                 # lite：单容器本地全闭环（US-8，推荐默认）
    quota: QuotaConfig = field(default_factory=QuotaConfig)
    recall: RecallConfig = field(default_factory=RecallConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    governance: GovernanceConfig = field(default_factory=GovernanceConfig)

    def __post_init__(self) -> None:
        if self.profile not in PROFILES:
            raise ValueError(f"非法部署档位: {self.profile}（可选 {PROFILES}）")

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "MetaMemoryConfig":
        sections = {"quota": QuotaConfig, "recall": RecallConfig,
                    "memory": MemoryConfig, "governance": GovernanceConfig}
        known = {k: v for k, v in data.items() if k in ("profile",) or k in sections}
        kwargs = {"profile": known.get("profile", "lite")}
        for key, section in sections.items():
            if key in known:
                kwargs[key] = section(**{k: v for k, v in known[key].items()
                                         if k in section.__dataclass_fields__})
        return cls(**kwargs)


def load_config(path: str | None = None, env_prefix: str = "METAMEMORY_") -> MetaMemoryConfig:
    """配置来源优先级：显式 JSON 文件 > 环境变量（METAMEMORY_PROFILE 等）> 内置默认。

    环境变量只覆盖标量段（METAMEMORY_PROFILE / METAMEMORY_RECALL_TOP_K /
    METAMEMORY_MEMORY_RECONSOLIDATION_WINDOW_HOURS 等，双下划线分段）。
    """
    data: dict = {}
    if path:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    for key, value in os.environ.items():
        if not key.startswith(env_prefix):
            continue
        parts = key[len(env_prefix):].lower().split("_")
        if len(parts) == 1:
            data[parts[0]] = value
        elif len(parts) >= 3 and parts[0] in ("quota", "recall", "memory", "governance"):
            section = data.setdefault(parts[0], {})
            section["_".join(parts[1:])] = _coerce(value)
    return MetaMemoryConfig.from_dict(data)


def _coerce(raw: str):
    for cast in (int, float):
        try:
            return cast(raw)
        except ValueError:
            continue
    if raw.lower() in ("true", "false"):
        return raw.lower() == "true"
    return raw
