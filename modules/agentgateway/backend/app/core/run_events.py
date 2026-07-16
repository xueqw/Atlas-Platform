"""Planner run-event recorder (T8: planner-run-events).

Normalized, persisted stage timeline for a planner turn, emitted alongside the
existing ephemeral ``activity`` stream (which is unchanged). One ``run_id`` per
turn groups all events; an emitter both persists a ``planner_run_events`` row and
pushes a real-time ``run_event`` WS message with the SAME payload (落库与推送对齐).

Best-effort throughout: a DB or socket hiccup must never break the planner turn.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from sqlmodel import Session, select

from app.core.database import engine
from app.models.db import PlannerRunEvent, _utcnow

_log = logging.getLogger(__name__)

# Normalized step set for the turn-loop stages T8 persists (design D1; the
# out-of-loop draft_generate/compile/test_run stages are deferred).
RUN_STEPS = (
    "intent_inference", "context_load", "memory_recall",
    "expert_retrieval", "capability_match", "proposal_compose",
)
RUN_STATUSES = ("queued", "running", "completed", "failed")

# Coarse progress per step (design D4 / Open Q): enough for a progress bar.
_STEP_PROGRESS = {
    "intent_inference": 0.1,
    "context_load": 0.25,
    "memory_recall": 0.4,
    "expert_retrieval": 0.6,
    "capability_match": 0.8,
    "proposal_compose": 1.0,
}


class RunEventEmitter:
    """Holds the per-turn ``run_id`` (+ optional proposal_id) and emits events.

    ``emit`` is async only because it may push over the WebSocket; the DB write
    is synchronous and best-effort. Pass ``websocket=None`` (e.g. in tests) to
    persist without pushing."""

    def __init__(self, run_id: str, websocket=None, proposal_id: Optional[int] = None):
        self.run_id = run_id
        self.websocket = websocket
        self.proposal_id = proposal_id

    async def emit(self, step: str, status: str, *, message: str = "",
                   progress: Optional[float] = None, details: Optional[dict] = None,
                   proposal_id: Optional[int] = None) -> None:
        # Normalize out-of-range values rather than raising (observability must
        # not break the turn). Unknown step/status are still recorded verbatim so
        # a typo surfaces in the data instead of being silently dropped.
        if proposal_id is not None:
            self.proposal_id = proposal_id
        if progress is None:
            progress = _STEP_PROGRESS.get(step, 0.0)
        progress = max(0.0, min(1.0, float(progress)))
        details = details or {}

        # 1. persist (best-effort)
        try:
            with Session(engine) as s:
                s.add(PlannerRunEvent(
                    run_id=self.run_id, proposal_id=self.proposal_id,
                    step=step, status=status, message=message[:2000],
                    details_json=json.dumps(details, ensure_ascii=False),
                    progress=progress,
                ))
                s.commit()
        except Exception as exc:
            _log.warning("run_event persist failed (%s)", exc)

        # 2. push (best-effort, same payload → 落库与推送对齐)
        if self.websocket is not None:
            try:
                await self.websocket.send_json({
                    "type": "run_event", "run_id": self.run_id,
                    "proposal_id": self.proposal_id, "step": step, "status": status,
                    "message": message, "details": details, "progress": progress,
                })
            except Exception:
                pass


def list_run_events(run_id: str, *, session: Optional[Session] = None) -> list:
    """Return a run's events ordered by id (creation order)."""
    own = session is None
    s = session or Session(engine)
    try:
        rows = s.exec(
            select(PlannerRunEvent).where(PlannerRunEvent.run_id == run_id)
            .order_by(PlannerRunEvent.id)
        ).all()
        return [_view(r) for r in rows]
    finally:
        if own:
            s.close()


def list_run_events_by_proposal(proposal_id: int, *, session: Optional[Session] = None) -> list:
    own = session is None
    s = session or Session(engine)
    try:
        rows = s.exec(
            select(PlannerRunEvent).where(PlannerRunEvent.proposal_id == proposal_id)
            .order_by(PlannerRunEvent.id)
        ).all()
        return [_view(r) for r in rows]
    finally:
        if own:
            s.close()


def _view(r: PlannerRunEvent) -> dict:
    return {
        "id": r.id, "run_id": r.run_id, "proposal_id": r.proposal_id,
        "step": r.step, "status": r.status, "message": r.message,
        "details": json.loads(r.details_json or "{}"), "progress": r.progress,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }
