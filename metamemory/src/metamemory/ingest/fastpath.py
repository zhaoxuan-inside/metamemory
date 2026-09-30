"""快通道（I-012）：接入事务内同步直写原文层——零 LLM，<100ms 可检索（US-1）。"""
from __future__ import annotations

import time

from metamemory_framework.container.signature import unified_method

from ..domain.models import MemoryEpisode
from ..domain.store import MemoryStore


@unified_method("ingest.fastpath_store", 1, "data")
def fastpath_store(input: dict, output: dict, context, tenant, metrics, evolution) -> bool:
    """受理即落袋：原文片段进情境容器，同事务幂等（source_uri 去重）。"""
    store: MemoryStore = context.deps["store"]
    event = input["event"]
    existing = store.by_uri.get(event["source_uri"])
    if existing is not None:
        output.update({"episode_id": existing, "stored": True, "replayed": True})
        return True
    episode = MemoryEpisode(
        episode_id=store.next_id("ep"), tenant_id=tenant.tenant_id,
        domain_id=tenant.domain_id, source_uri=event["source_uri"],
        content=event["content"], actor=event["actor"], occurred_at=event["occurred_at"],
        topics=event.get("topics") or [],
    )
    store.add_episode(episode)
    store.dedupe[episode.content_hash()] = episode.episode_id
    output.update({"episode_id": episode.episode_id, "stored": True, "replayed": False,
                   "content_hash": episode.content_hash()})
    metrics.counter("fastpath.stored")
    metrics.mark("fastpath.latency_ms", round((time.time() % 1) * 1000, 1))
    return True
