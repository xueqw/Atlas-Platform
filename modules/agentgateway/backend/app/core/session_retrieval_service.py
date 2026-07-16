"""Session-history retrieval service skeleton (Batch B).

L1 session-history layer reads: planner / agent / evaluation conversation
history and lightweight session summaries. This layer lives entirely in
PostgreSQL (``conversations`` / ``messages`` / ``planner_sessions``) and MUST
NOT touch mem0 — raw transcript is not semantic long-term memory.

Skeleton stage: simple, bounded PG reads + a placeholder summary that returns
the most recent turns. No LLM summarization wired yet.
"""

from __future__ import annotations

import logging
from typing import List, Optional

from sqlmodel import Session, select

from app.core.database import engine
from app.models.db import Conversation, Message, PlannerSession

_log = logging.getLogger(__name__)


def get_recent_messages(conversation_id: int, limit: int = 20) -> List[Message]:
    """Return the most recent ``limit`` messages for a conversation, oldest-first.
    Pure PG read; never reads mem0."""
    with Session(engine) as s:
        rows = s.exec(
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.created_at.desc())
            .limit(limit)
        ).all()
    return list(reversed(rows))


def get_planner_history(conversation_id: str) -> Optional[PlannerSession]:
    """Fetch a planner session by its string conversation id (L1 planner
    history). Returns None when absent."""
    with Session(engine) as s:
        return s.exec(
            select(PlannerSession).where(PlannerSession.conversation_id == conversation_id)
        ).first()


def summarize_session(conversation_id: int, max_turns: int = 10) -> str:
    """Produce a lightweight session summary for prompt injection.

    Skeleton: concatenates the last ``max_turns`` role/content pairs. A real
    LLM-backed summarizer is queued as a ``compress_summary`` writeback job in a
    later change; this keeps the interface stable and returns something usable
    without an LLM call.
    """
    msgs = get_recent_messages(conversation_id, limit=max_turns)
    if not msgs:
        return ""
    lines = [f"{(m.role or 'user')}: {(m.content or '').strip()}" for m in msgs]
    return "\n".join(lines)
