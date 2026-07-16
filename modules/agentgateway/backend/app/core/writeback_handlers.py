"""Real writeback job handlers (Batch D, task 5.6).

Replaces the Batch C placeholders: each handler extracts long-term candidates
(through the admission gate) and persists them via
``memory_service.record_memory_item``, which resolves dedupe/conflict. Handlers
raise on failure so the worker marks the job failed + retryable.

Registered onto the worker via ``register_all`` (called at worker import). The
"unknown job_type → failed" guard in the worker still protects genuinely
unknown types — these handlers only cover the four known writeback jobs.

Body persistence is pgonly here (mem0_ref stays None); a future read-side batch
offloads bodies to mem0 without changing this write path.
"""

from __future__ import annotations

import logging
from typing import List

from sqlmodel import Session, select

from app.core.database import engine
from app.core import memory_service, memory_extractor
from app.core.memory_provider import MemoryRecord
from app.models.db import MemoryItem

_log = logging.getLogger(__name__)


def handle_extract_profile(job_id: int, payload: dict) -> None:
    """Extract durable user preferences from a conversation slice → profile rows.

    payload: {conversation_id?, user_id?, messages?[]}. When ``messages`` is not
    inlined, recent messages for ``conversation_id`` are loaded from L1.
    """
    from app.core import session_retrieval_service as sr

    user_id = payload.get("user_id")
    messages = payload.get("messages")
    if not messages and payload.get("conversation_id") is not None:
        rows = sr.get_recent_messages(int(payload["conversation_id"]))
        messages = [{"role": m.role, "content": m.content} for m in rows]
    candidates = memory_extractor.extract_profile_candidates(
        messages=messages or [], user_id=user_id, source_ref=str(payload.get("source_ref") or ""),
    )
    for rec in candidates:
        memory_service.record_memory_item(rec)
    _log.info("extract_profile: job %s wrote %s profile candidate(s)", job_id, len(candidates))


def handle_extract_episodic(job_id: int, payload: dict) -> None:
    """Extract a key-event episodic from a proposal/run/eval summary payload."""
    candidates = memory_extractor.extract_episodic_candidates(
        source_kind=str(payload.get("source_kind") or "proposal"),
        source_ref=str(payload.get("source_ref") or ""),
        payload=payload,
        agent_id=payload.get("agent_id"),
    )
    for rec in candidates:
        memory_service.record_memory_item(rec)
    _log.info("extract_episodic: job %s wrote %s episodic candidate(s)", job_id, len(candidates))


def handle_merge_semantic(job_id: int, payload: dict) -> None:
    """Consolidate repeated episodic for a scope into canonical semantic rows."""
    scope = str(payload.get("scope") or "agent")
    agent_id = payload.get("agent_id")
    with Session(engine) as s:
        stmt = select(MemoryItem).where(
            MemoryItem.status == "active", MemoryItem.memory_type == "episodic", MemoryItem.scope == scope,
        )
        if agent_id is not None:
            stmt = stmt.where(MemoryItem.agent_id == agent_id)
        eps = list(s.exec(stmt).all())
    episodic_records: List[MemoryRecord] = [
        MemoryRecord(memory_type="episodic", scope=e.scope, content=e.content,
                     importance=e.importance, confidence=e.confidence,
                     source_kind=e.source_kind, source_ref=e.source_ref, agent_id=e.agent_id)
        for e in eps
    ]
    merged = memory_extractor.extract_semantic_candidates(episodic=episodic_records)
    for rec in merged:
        memory_service.record_memory_item(rec)
    _log.info("merge_semantic: job %s merged %s semantic candidate(s)", job_id, len(merged))


def handle_compress_summary(job_id: int, payload: dict) -> None:
    """Compress over-threshold episodic into a summary, then decay stale episodic."""
    scope = str(payload.get("scope") or "agent")
    agent_id = payload.get("agent_id")
    force = bool(payload.get("stage_end") or payload.get("force"))
    memory_service.compress_episodic_to_summary(scope, agent_id=agent_id, force=force)
    decayed = memory_service.decay_stale_episodic()
    _log.info("compress_summary: job %s compressed scope=%s, decayed %s episodic", job_id, scope, decayed)


def register_all() -> None:
    """Register the four real handlers onto the worker (replaces placeholders)."""
    from app import worker

    worker.register_handler("extract_profile", handle_extract_profile)
    worker.register_handler("extract_episodic", handle_extract_episodic)
    worker.register_handler("merge_semantic", handle_merge_semantic)
    worker.register_handler("compress_summary", handle_compress_summary)
