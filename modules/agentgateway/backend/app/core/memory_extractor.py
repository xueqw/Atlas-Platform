"""Memory extractor skeleton (Batch B/D).

Pulls long-term-memory *candidates* out of planner sessions, conversations,
proposals, run summaries and evaluations. Candidates are not written here —
extraction is async: the runtime enqueues a ``memory_writeback_job`` and the
worker calls these helpers, applies the write-admission gate + dedupe/conflict
rules, then persists via ``memory_provider``.

Skeleton stage: deterministic, dependency-free heuristics that surface obvious
candidates and apply the admission gate. No LLM extraction wired yet; the seams
(one function per source kind) stay stable for Batch D to flesh out.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from app.core.memory_provider import MemoryRecord
from app.core.memory_service import is_admissible_for_long_term

_log = logging.getLogger(__name__)


def _candidate(
    *, memory_type: str, scope: str, content: str, source_kind: str,
    source_ref: str = "", agent_id: Optional[int] = None, importance: float = 0.3,
) -> Optional[MemoryRecord]:
    """Build a candidate record, returning None if the content is inadmissible
    for the long-term layer (transcript noise / tool output / empty)."""
    if not is_admissible_for_long_term(content=content, source_kind=source_kind):
        return None
    return MemoryRecord(
        memory_type=memory_type,
        scope=scope,
        content=content.strip(),
        importance=importance,
        confidence=0.5,
        source_kind=source_kind,
        source_ref=source_ref,
        agent_id=agent_id,
    )


def extract_profile_candidates(
    *, messages: List[Dict[str, Any]], user_id: Optional[int] = None, source_ref: str = ""
) -> List[MemoryRecord]:
    """Stable-preference profile facts from a conversation slice.

    Conservative deterministic heuristic (Batch D): only user messages that
    explicitly state a durable preference are surfaced. We look for preference
    cues ("我喜欢 / 我倾向 / 请一直 / 以后 / 默认 / 总是 / 用…回答") and emit a
    ``profile_preference`` candidate, normalised to ``<主题>：<内容>`` when a cue
    maps to a known key. Each candidate still passes the admission gate via
    ``_candidate``. LLM-grade extraction is future work; this keeps only clearly
    long-term signals.
    """
    cues = ("我喜欢", "我倾向", "请一直", "请始终", "以后都", "以后请", "默认", "总是", "习惯", "偏好")
    out: List[MemoryRecord] = []
    seen: set = set()
    for msg in messages or []:
        if (msg.get("role") or "") != "user":
            continue
        text = str(msg.get("content") or "").strip()
        if not text or not any(c in text for c in cues):
            continue
        norm = " ".join(text.lower().split())
        if norm in seen:
            continue
        seen.add(norm)
        rec = _candidate(
            memory_type="profile_preference", scope="user", content=text,
            source_kind="conversation", source_ref=source_ref, importance=0.6,
        )
        if rec is not None:
            rec.user_id = user_id
            out.append(rec)
    return out


def extract_episodic_candidates(
    *, source_kind: str, source_ref: str, payload: Dict[str, Any], agent_id: Optional[int] = None
) -> List[MemoryRecord]:
    """Episodic candidates from a key decision / replan / failure-fix event.

    Wraps a ``summary`` / ``rationale`` field from the payload (if present and
    admissible) as one episodic candidate so the writeback path is exercisable
    end-to-end. Richer event mining is future work.
    """
    summary = str(payload.get("summary") or payload.get("rationale") or "").strip()
    rec = _candidate(
        memory_type="episodic",
        scope="agent",
        content=summary,
        source_kind=source_kind,
        source_ref=source_ref,
        agent_id=agent_id,
        importance=0.4,
    )
    return [rec] if rec else []


def extract_semantic_candidates(
    *, episodic: List[MemoryRecord]
) -> List[MemoryRecord]:
    """Semantic memories merged from validated episodic candidates.

    Conservative deterministic consolidation (Batch D): group episodic by
    normalised content, and for any content seen more than once (i.e. a
    repeated, thus stabler, signal) emit a single ``semantic`` candidate. One-off
    episodic are left as-is (not promoted). Candidates pass the admission gate.
    """
    counts: Dict[str, int] = {}
    sample: Dict[str, MemoryRecord] = {}
    for e in episodic or []:
        norm = " ".join((e.content or "").lower().split())
        if not norm:
            continue
        counts[norm] = counts.get(norm, 0) + 1
        sample.setdefault(norm, e)
    out: List[MemoryRecord] = []
    for norm, n in counts.items():
        if n < 2:
            continue
        src = sample[norm]
        rec = _candidate(
            memory_type="semantic", scope=src.scope or "agent", content=src.content,
            source_kind="run_summary", source_ref=src.source_ref, agent_id=src.agent_id,
            importance=min(0.9, 0.5 + 0.1 * n),
        )
        if rec is not None:
            out.append(rec)
    return out
