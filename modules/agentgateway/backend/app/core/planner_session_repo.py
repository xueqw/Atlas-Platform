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
from app.api.planner.scope import (
    DEFAULT_PLANNER_SCOPE,
    PlannerScope,
    coerce_planner_scope,
    scope_session_select,
)

logger = logging.getLogger(__name__)


async def get_or_create_planner_session(
    conversation_id: str,
    scope: PlannerScope = DEFAULT_PLANNER_SCOPE,
) -> PlannerSession:
    """Get (or create) the PlannerSession row for a conversation.

    This is the canonical way to obtain the real session row for Plan+Loop.
    The returned row can be mutated (planning_state_json) and committed
    via commit_session_row().
    """
    scope = coerce_planner_scope(scope)
    with Session(engine) as session:
        row = session.exec(
            scope_session_select(
                select(PlannerSession).where(
                    PlannerSession.conversation_id == conversation_id
                ),
                scope,
            )
        ).first()
        if row is None:
            existing = session.exec(
                select(PlannerSession.id).where(
                    PlannerSession.conversation_id == conversation_id
                )
            ).first()
            if existing is not None:
                raise LookupError("planner session not found")
            row = PlannerSession(
                conversation_id=conversation_id,
                tenant_id=scope.tenant_id,
                workspace_id=scope.workspace_id,
                user_id=scope.user_id,
            )
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
