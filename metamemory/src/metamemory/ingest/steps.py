"""接收管道步骤族 I：归一/切分/门禁/查重/选取（I-013~I-017）。

硬约束落地：pre_gate 输出携带 confidence 软信号（可被下游推翻回写；安全/PII 门
fail-closed 只能收紧）；segment 为写侧第一决策（span 保底可逆，约束 13/ADR 18）。
"""
from __future__ import annotations

import re

from metamemory_framework.container.signature import unified_method

from ..domain.models import Segment
from ..domain.store import MemoryStore
from .fastpath import fastpath_store  # noqa: F401  （注册快通道步骤）

INJECTION_PATTERNS = ("ignore previous", "忽略之前", "你的指令是", "系统提示词")
PII_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
NEGATIVE_LIST = ("哈哈", "你好", "谢谢", "再见", "嗯嗯")
URGENCY_WORDS = ("周五前", "明天", "之前", "deadline", "尽快")
EMPHASIS_WORDS = ("一定", "记住", "重要", "必须")


@unified_method("ingest.normalize_event", 1, "data")
def normalize_event(input: dict, output: dict, context, tenant, metrics, evolution) -> bool:
    """I-013：事件归一——形态标记、空白清理、时间锚定（四要素齐备）。"""
    event = input["event"]
    content = " ".join(str(event["content"]).split())
    if not content:
        evolution.fail("EMPTY_CONTENT", "归一后内容为空，不受理")
        return False
    output["normalized"] = {
        "source_uri": event["source_uri"], "content": content, "actor": event["actor"],
        "occurred_at": event["occurred_at"], "topics": event.get("topics") or [],
    }
    return True


@unified_method("ingest.segment_units", 1, "data")
def segment_units(input: dict, output: dict, context, tenant, metrics, evolution) -> bool:
    """I-014：单元切分——统计信号骨架（段落/轮次边界），参数版本可演化。"""
    store: MemoryStore = context.deps["store"]
    content = input["normalized"]["content"]
    pieces = [p for p in re.split(r"\n+|[。！?？;；]", content) if p.strip()]
    offset, segments = 0, []
    for piece in pieces:
        start = content.find(piece, offset)
        span = (start, start + len(piece))
        segment = Segment(segment_id=store.next_id("seg"), episode_id=input["episode_id"],
                          text=piece.strip(), span=span, basis="paragraph_split", param_version="seg-v0")
        store.add_segment(segment)
        segments.append({"segment_id": segment.segment_id, "text": segment.text,
                         "span": list(span), "basis": segment.basis})
        offset = span[1]
    output["segments"] = segments
    metrics.counter("segment.units", len(segments))
    return True


@unified_method("ingest.pre_gate", 1, "data")
def pre_gate(input: dict, output: dict, context, tenant, metrics, evolution) -> bool:
    """I-015：安全/PII 门（fail-closed）+ 注入疑标记；输出带 confidence 软信号。"""
    gated = []
    for segment in input["segments"]:
        text = segment["text"]
        suspect = any(pattern in text.lower() for pattern in INJECTION_PATTERNS)
        pii = bool(PII_EMAIL.search(text))
        confidence = 0.2 if suspect else (0.6 if pii else 0.95)
        gated.append({**segment, "suspect": suspect, "pii": pii, "confidence": confidence})
    output["gated"] = gated
    output["confidence"] = min((g["confidence"] for g in gated), default=1.0)
    return True


@unified_method("ingest.dedupe_route", 1, "data")
def dedupe_route(input: dict, output: dict, context, tenant, metrics, evolution) -> bool:
    """I-016：查重短路——同内容 hash 已入库则路由 duplicate（排除本 episode 自身）。"""
    store: MemoryStore = context.deps["store"]
    episode_id = input.get("episode_id", "")
    route = "new"
    for segment in input["gated"]:
        for prior in store.dedupe.get(_hash(segment["text"]), []):
            if prior != episode_id:  # 其他 episode 已有同内容 → 重复；自身不算
                route = "duplicate"
    output["route"] = route
    output["content_hash"] = _hash(input["gated"][0]["text"]) if input["gated"] else ""
    return True


@unified_method("ingest.select_worthiness", 1, "data")
def select_worthiness(input: dict, output: dict, context, tenant, metrics, evolution) -> bool:
    """I-017：五维信号骨架 + 反面清单——值得度预判（便宜，全量过）。"""
    selected = []
    for segment in input["gated"]:
        text = segment["text"]
        if any(word in text for word in NEGATIVE_LIST):
            selected.append({**segment, "worthiness": "none", "signals": {"negative_list": True}})
            continue
        signals = {
            "concreteness": len(re.findall(r"\d|[\u4e00-\u9fa5]{2,}(?=:|：)", text)),
            "timeliness": sum(word in text for word in URGENCY_WORDS),
            "investment": sum(word in text for word in EMPHASIS_WORDS),
        }
        score = sum(signals.values())
        worthiness = "high" if score >= 3 else ("mid" if score >= 1 else "low")
        selected.append({**segment, "worthiness": worthiness, "signals": signals})
    rank = {"none": 0, "low": 1, "mid": 2, "high": 3}
    output["selected"] = selected
    output["worthiness"] = "none" if not selected else max(selected, key=lambda s: rank[s["worthiness"]])["worthiness"]
    return True


def _hash(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode()).hexdigest()[:16]
