"""MetaMemory 配置面（三分离·配置侧）：schema 在代码、值在配置文件/环境变量、数据在数据目录。

装载优先级：显式 JSON 文件（参数/env METAMEMORY_CONFIG）> 代码根 config/metamemory.json
> 环境变量 > 内置默认。默认值全部取自文档族裁定：再巩固窗口 72h（约束 11）、
RRF k=60（附录 C）、免费档发现位 3（G3 口径）、快通道承诺 <100ms、四态路由阈值。
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

PROFILES = ("full", "lite", "minimal")  # 档位=槽位预设（ADR-004）

# 代码根（metamemory/）：配置文件与默认数据目录的锚点（三分离：代码/配置/数据）
CODE_ROOT = Path(__file__).resolve().parents[2]
CONFIG_FILE = CODE_ROOT / "config" / "metamemory.json"
DEFAULT_DATA_DIR = CODE_ROOT / "var"


@dataclass
class QuotaConfig:
    max_pending_events: int = 10_000      # 写入配额封顶（COGS 上界执行件）
    discovery_slots_free: int = 3         # 免费档意图发现位（意图本体不限量，G3）
    discovery_slots_pro: int | None = None  # Pro 不限


@dataclass
class StorageConfig:
    """三分离·数据侧：一切运行时数据只落本目录（SQLite WAL + 本地 FS），代码目录零数据。"""

    data_dir: str = ""                    # 空 = CODE_ROOT/var（gitignored）；可指向任意绝对路径
    db_filename: str = "metamemory.db"    # SQLite 主库（lite 档真源；full 档由 PG 槽位替换）

    def resolve_data_dir(self) -> Path:
        path = Path(self.data_dir) if self.data_dir else DEFAULT_DATA_DIR
        return path.expanduser().resolve()


@dataclass
class InferenceConfig:
    """推理网关（唯一最佳路径，ADR-006）：任意 OpenAI 兼容端点（vLLM/Ollama/Ark/OpenAI）。

    model=强模型（深提取/NLI 裁判/L4 精修）；embedding_model=语义向量（/v1/embeddings，
    bge-m3 等）；rerank_model=精排（最佳路径组件，未配置=RRF 直出的显式配置选择）。
    """

    base_url: str = ""                    # 如 http://localhost:11434/v1（Ollama）；空 = 显式配置错误
    api_key: str = ""                     # 云端端点填 API Key；本地端点可空
    model: str = ""                       # 强模型名（深提取/NLI/摘要）
    embedding_model: str = ""             # 嵌入模型名（语义召回路；空=该路不可用，显式计数不静默）
    rerank_model: str = ""                # 精排模型名（空=RRF 直出；配置即启用的最佳路径组件）
    timeout_seconds: int = 60
    prompt_version: str = "v1"            # 深提取 prompt 版本（llm_calls 幂等键组成）
    nli_prompt_version: str = "v1"        # NLI 裁判 prompt 版本（幂等键组成）


@dataclass
class RecallConfig:
    top_k: int = 10                       # search 默认返回条数（≤50）
    token_budget: int = 2048              # express 组装预算
    rrf_k: int = 60                       # RRF 融合常数（技术词典）
    default_intent: str = "fact"          # 检索意图三分默认档（R5）
    semantic_min_score: float = 0.5       # 语义路相关性地板（向量检索必返回 top-k，低于地板=无关，拒答语义）


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
    publish_withdraw_window_hours: int = 72       # L4/L5 发布无痕撤回窗（02 §6.2）


@dataclass
class NotifyConfig:
    """三层通知（T-026）：advance 落库兜底恒开；progress 进程内恒开；webhook 按配置启用。"""

    webhook_url: str = ""                 # 空 = 回调路关闭（显式配置选择）
    webhook_secret: str = ""              # HMAC-SHA256 签名密钥（X-MetaMemory-Signature）
    webhook_max_attempts: int = 5         # 超限转 dead（显式失败）
    webhook_timeout_seconds: int = 10


@dataclass
class MetaMemoryConfig:
    profile: str = "lite"                 # lite：单容器本地全闭环（US-8，推荐默认）
    quota: QuotaConfig = field(default_factory=QuotaConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    inference: InferenceConfig = field(default_factory=InferenceConfig)
    recall: RecallConfig = field(default_factory=RecallConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    governance: GovernanceConfig = field(default_factory=GovernanceConfig)
    notify: NotifyConfig = field(default_factory=NotifyConfig)

    def __post_init__(self) -> None:
        if self.profile not in PROFILES:
            raise ValueError(f"非法部署档位: {self.profile}（可选 {PROFILES}）")

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "MetaMemoryConfig":
        sections = {"quota": QuotaConfig, "storage": StorageConfig, "inference": InferenceConfig,
                    "recall": RecallConfig, "memory": MemoryConfig, "governance": GovernanceConfig,
                    "notify": NotifyConfig}
        known = {k: v for k, v in data.items() if k in ("profile",) or k in sections}
        kwargs = {"profile": known.get("profile", "lite")}
        for key, section in sections.items():
            if key in known:
                kwargs[key] = section(**{k: v for k, v in known[key].items()
                                         if k in section.__dataclass_fields__})
        return cls(**kwargs)


def discover_config_file() -> Path | None:
    """配置文件发现：env METAMEMORY_CONFIG > 代码根 config/metamemory.json > None（用内置默认）。"""
    env_path = os.environ.get("METAMEMORY_CONFIG")
    if env_path:
        path = Path(env_path).expanduser()
        return path if path.is_file() else None
    return CONFIG_FILE if CONFIG_FILE.is_file() else None


def load_config(path: str | None = None, env_prefix: str = "METAMEMORY_") -> MetaMemoryConfig:
    """配置来源优先级：显式 JSON 文件 > 代码根 config/metamemory.json > 环境变量 > 内置默认。

    环境变量只覆盖标量段（METAMEMORY_PROFILE / METAMEMORY_RECALL_TOP_K /
    METAMEMORY_MEMORY_RECONSOLIDATION_WINDOW_HOURS 等，双下划线分段）。
    """
    data: dict = {}
    config_file = Path(path) if path else discover_config_file()
    if config_file is not None and config_file.is_file():
        data = json.loads(config_file.read_text(encoding="utf-8"))
    for key, value in os.environ.items():
        if not key.startswith(env_prefix):
            continue
        parts = key[len(env_prefix):].lower().split("_")
        if len(parts) == 1:
            data[parts[0]] = value
        elif len(parts) >= 3 and parts[0] in ("quota", "storage", "inference", "recall", "memory",
                                              "governance", "notify"):
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
