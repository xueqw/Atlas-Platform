"""Planner Session Repository — thin DB access for Plan+Loop persistence.

Provides get_or_create_planner_session() so the Loop Runner works with a
real PlannerSession ORM row instead of a duck-typed fake session.
"""

from __future__ import annotations

import logging
from typing import Optional

from sqlmodel import Session, select

from app.core.database import engine
from app.models.db import PlannerSession

logger = logging.getLogger(__name__)


async def get_or_create_planner_session(conversation_id: str) -> PlannerSession:
    """Get (or create) the PlannerSession row for a conversation.

    This is the canonical way to obtain the real session row for Plan+Loop.
    The returned row can be mutated (planning_state_json) and committed
    via commit_session_row().
    """
    with Session(engine) as session:
        row = session.exec(
            select(PlannerSession).where(
                PlannerSession.conversation_id == conversation_id
            )
        ).first()
        if row is None:
            row = PlannerSession(conversation_id=conversation_id)
            session.add(row)
            session.commit()
            session.refresh(row)
        # Detach from session so it can be used outside the with block
        session.expunge(row)
    return row


def commit_session_row(row: PlannerSession) -> None:
    """Persist mutations to the PlannerSession row (planning_state_json, etc)."""
    from datetime import datetime, timezone

    with Session(engine) as session:
        # Merge detached instance back
        row.last_updated_at = datetime.now(timezone.utc)
        session.merge(row)
        session.commit()
