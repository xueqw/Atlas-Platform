"""Memory event recorder (T9: mem0-observability-and-verification).

Auditable trail of memory operations — write / recall / healthcheck / review —
persisted to ``memory_events`` and, when a planner socket is live, pushed as a
``memory_event`` WS message (same payload → 落库与推送对齐). Mirrors the T8
RunEventEmitter contract: best-effort throughout, never breaks the caller.

Most writes/recalls happen OUTSIDE a turn loop (apply / writeback jobs) where no
socket exists — so persistence is the source of truth and the push is an
opportunistic extra when ``websocket`` is provided.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from sqlmodel import Session, select

from app.core.database import engine
from app.models.db import (
    MEMORY_EVENT_STATUSES,
    MEMORY_EVENT_TYPES,
    MemoryEvent,
)

_log = logging.getLogger(__name__)


def record_memory_event(
    *,
    provider: str,
    event_type: str,
    status: str = "success",
    run_id: str = "",
    proposal_id: Optional[int] = None,
    scope: str = "",
    source: str = "",
    source_ref: str = "",
    query_or_reason: str = "",
    payload_summary: str = "",
    details: Optional[dict] = None,
) -> None:
    """Persist one memory_event (sync, best-effort). Used by the sync memory
    service / provider call sites, which have no live socket. The async planner
    recall site uses ``emit_memory_event`` to also push.

    Unknown event_type/status are still recorded verbatim (so a typo surfaces in
    the data rather than being dropped), but a debug log flags it."""
    if event_type not in MEMORY_EVENT_TYPES:
        _log.debug("memory_event: non-standard event_type %r", event_type)
    if status not in MEMORY_EVENT_STATUSES:
        _log.debug("memory_event: non-standard status %r", status)
    details = details or {}
    try:
        with Session(engine) as s:
            s.add(MemoryEvent(
                run_id=run_id, proposal_id=proposal_id, provider=provider,
                event_type=event_type, scope=scope, source=source,
                source_ref=source_ref, query_or_reason=query_or_reason[:2000],
                payload_summary=payload_summary[:2000],
                payload_json=json.dumps(details, ensure_ascii=False), status=status,
            ))
            s.commit()
    except Exception as exc:
        _log.warning("memory_event persist failed (%s)", exc)


def _push_payload(**kw) -> dict:
    return {
        "type": "memory_event", "provider": kw.get("provider"),
        "event_type": kw.get("event_type"), "status": kw.get("status"),
        "run_id": kw.get("run_id", ""), "proposal_id": kw.get("proposal_id"),
        "scope": kw.get("scope", ""), "source": kw.get("source", ""),
        "query_or_reason": kw.get("query_or_reason", ""),
        "payload_summary": kw.get("payload_summary", ""),
        "details": kw.get("details") or {},
    }


async def emit_memory_event(websocket=None, **kw) -> None:
    """Persist + (when a socket is live) push a memory_event. Used by the async
    planner recall site, the one place with an active WebSocket. Persist and push
    are each best-effort (落库与推送对齐 — same fields)."""
    record_memory_event(**kw)
    if websocket is not None:
        try:
            await websocket.send_json(_push_payload(**kw))
        except Exception:
            pass


def list_memory_events(run_id: str, *, limit: int = 500, session: Optional[Session] = None) -> list:
    own = session is None
    s = session or Session(engine)
    try:
        rows = s.exec(
            select(MemoryEvent).where(MemoryEvent.run_id == run_id)
            .order_by(MemoryEvent.id).limit(limit)
        ).all()
        return [_view(r) for r in rows]
    finally:
        if own:
            s.close()


def list_memory_events_by_proposal(proposal_id: int, *, limit: int = 500,
                                   session: Optional[Session] = None) -> list:
    own = session is None
    s = session or Session(engine)
    try:
        rows = s.exec(
            select(MemoryEvent).where(MemoryEvent.proposal_id == proposal_id)
            .order_by(MemoryEvent.id).limit(limit)
        ).all()
        return [_view(r) for r in rows]
    finally:
        if own:
            s.close()


def _view(r: MemoryEvent) -> dict:
    return {
        "id": r.id, "run_id": r.run_id, "proposal_id": r.proposal_id,
        "provider": r.provider, "event_type": r.event_type, "scope": r.scope,
        "source": r.source, "source_ref": r.source_ref,
        "query_or_reason": r.query_or_reason, "payload_summary": r.payload_summary,
        "details": json.loads(r.payload_json or "{}"), "status": r.status,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }
