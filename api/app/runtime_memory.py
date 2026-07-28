"""Read-only, tenant-scoped context loading for the LangGraph runtime."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .memory_ledger import MemoryScope, explain_memory_retrieval
from .models import AgentMemory
from .runtime_graph import AtlasAgentState
from .state_store import StateStore, state_store


SessionFactory = Callable[[], Session]


class RuntimeMemoryProvider:
    """Loads bounded memory data without allowing it to change runtime identity."""

    def __init__(self, session_factory: SessionFactory, *, hot_store: StateStore = state_store) -> None:
        self._session_factory = session_factory
        self._hot_store = hot_store

    async def load(self, state: AtlasAgentState) -> tuple[dict[str, Any], ...]:
        identity = state.identity
        context: list[dict[str, Any]] = []

        if identity.conversation_id:
            try:
                messages = self._hot_store.get(
                    "short-memory",
                    f"{identity.workspace_id}:{identity.user_id}:{identity.agent_id}:{identity.conversation_id}",
                )
            except Exception:
                # Redis is non-authoritative. A hot-memory outage must not stop
                # an otherwise read-only durable runtime execution.
                messages = None
            if isinstance(messages, list):
                recent = [self._message(item) for item in messages[-8:]]
                recent = [item for item in recent if item is not None]
                if recent:
                    context.append({"source": "short_term", "items": recent})

        try:
            with self._session_factory() as db:
                now = datetime.now(timezone.utc)
                long_rows = db.scalars(
                    select(AgentMemory)
                    .where(
                        AgentMemory.workspace_id == identity.workspace_id,
                        AgentMemory.user_id == identity.user_id,
                        AgentMemory.agent_id == identity.agent_id,
                        or_(AgentMemory.expires_at.is_(None), AgentMemory.expires_at > now),
                    )
                    .order_by(AgentMemory.updated_at.desc())
                    .limit(6)
                ).all()
                if long_rows:
                    context.append({
                        "source": "long_term",
                        "items": [
                            {"id": row.id, "category": row.category, "content": row.content[:1200]}
                            for row in long_rows
                        ],
                    })

                governed = explain_memory_retrieval(
                    db,
                    scope=MemoryScope(
                        workspace_id=identity.workspace_id,
                        user_id=identity.user_id,
                        agent_id=identity.agent_id,
                        run_id=identity.run_id,
                    ),
                )
                facts = governed.get("facts", [])[:8]
                if facts:
                    context.append({"source": "governed_facts", "items": facts})
        except Exception:
            # Persistent memory is useful context, never a dependency that
            # prevents a direct response from completing.
            pass

        return tuple(context)

    @staticmethod
    def _message(value: Any) -> dict[str, str] | None:
        if not isinstance(value, dict):
            return None
        role = value.get("role")
        content = value.get("content")
        if not isinstance(role, str) or not isinstance(content, str):
            return None
        return {"role": role[:32], "content": content[:1200]}
