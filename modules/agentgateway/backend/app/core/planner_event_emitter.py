"""Planner Event Emitter — emits plan_update and run_event over WebSocket.

Implements the EventEmitter protocol expected by the Loop Runner.
Convergent event protocol: only two core WS message types:
- plan_update: authoritative Plan snapshot for frontend Plan Progress
- run_event: step details for frontend Step Details layer
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

from .planner_plan import (
    Plan,
    PlanStep,
    StopReason,
    build_plan_update_payload,
)

logger = logging.getLogger(__name__)


# ─── Run Event Status (extended with waiting_user) ─────────────────────────────

RUN_EVENT_STATUSES = frozenset({"queued", "running", "completed", "failed", "waiting_user"})


# ─── WebSocket Event Emitter ───────────────────────────────────────────────────

class PlannerEventEmitter:
    """Emits plan events over a WebSocket connection.

    Implements the EventEmitter protocol for the Loop Runner.
    All emissions are best-effort — a send failure never breaks the loop.
    """

    def __init__(self, websocket, conversation_id: str, run_id: str):
        self._ws = websocket
        self._conversation_id = conversation_id
        self._run_id = run_id

    async def emit_plan_update(self, plan: Plan, stop_reason: Optional[StopReason] = None) -> None:
        """Push authoritative plan snapshot to frontend."""
        payload = build_plan_update_payload(
            conversation_id=self._conversation_id,
            run_id=self._run_id,
            plan=plan,
            stop_reason=stop_reason,
        )
        await self._safe_send(payload)

    async def emit_step_started(self, step: PlanStep) -> None:
        """Emit run_event with event_kind=plan_step_started."""
        payload = {
            "type": "run_event",
            "run_id": self._run_id,
            "conversation_id": self._conversation_id,
            "step": step.id,
            "event_kind": "plan_step_started",
            "status": "running",
            "message": step.title,
            "progress": self._calc_progress_for_step(step),
            "details": {
                "step_id": step.id,
                "title": step.title,
                "executor_type": step.executor_type.value if step.executor_type else None,
            },
        }
        await self._safe_send(payload)

    async def emit_step_completed(self, step: PlanStep, duration_ms: int) -> None:
        """Emit run_event with event_kind=plan_step_completed."""
        payload = {
            "type": "run_event",
            "run_id": self._run_id,
            "conversation_id": self._conversation_id,
            "step": step.id,
            "event_kind": "plan_step_completed",
            "status": "completed",
            "message": f"{step.title} 完成",
            "progress": 1.0,
            "details": {
                "step_id": step.id,
                "title": step.title,
                "duration_ms": duration_ms,
                "output_summary": _summarize_outputs(step.outputs),
            },
        }
        await self._safe_send(payload)

    async def emit_step_failed(self, step: PlanStep, error: str) -> None:
        """Emit run_event with event_kind=validation_failed."""
        payload = {
            "type": "run_event",
            "run_id": self._run_id,
            "conversation_id": self._conversation_id,
            "step": step.id,
            "event_kind": "validation_failed",
            "status": "failed",
            "message": f"{step.title} 失败: {error[:100]}",
            "progress": 0.0,
            "details": {
                "step_id": step.id,
                "title": step.title,
                "error": error,
            },
        }
        await self._safe_send(payload)

    def _calc_progress_for_step(self, step: PlanStep) -> float:
        """Calculate a progress value (placeholder — real impl uses plan position)."""
        return 0.5  # Step is running, halfway

    async def _safe_send(self, payload: dict) -> None:
        """Best-effort send — never raise."""
        try:
            await self._ws.send_json(payload)
        except Exception:
            logger.debug(f"Failed to send event: {payload.get('type')}/{payload.get('event_kind')}")


# ─── Null Emitter (for testing) ────────────────────────────────────────────────

class NullEventEmitter:
    """No-op emitter for testing and headless execution."""

    def __init__(self):
        self.events: list[dict] = []

    async def emit_plan_update(self, plan: Plan, stop_reason: Optional[StopReason] = None) -> None:
        self.events.append({"type": "plan_update", "plan_id": plan.plan_id, "stop_reason": stop_reason})

    async def emit_step_started(self, step: PlanStep) -> None:
        self.events.append({"type": "step_started", "step_id": step.id})

    async def emit_step_completed(self, step: PlanStep, duration_ms: int) -> None:
        self.events.append({"type": "step_completed", "step_id": step.id, "duration_ms": duration_ms})

    async def emit_step_failed(self, step: PlanStep, error: str) -> None:
        self.events.append({"type": "step_failed", "step_id": step.id, "error": error})


# ─── Plan Persister (default implementation) ───────────────────────────────────

class DefaultPlanPersister:
    """Default plan persister — writes to session's planning_state_json and commits."""

    async def persist(self, session: Any, plan: Plan) -> None:
        """Persist plan to session row and commit to DB."""
        from .planner_plan import save_plan
        try:
            save_plan(session, plan)
            # Commit if we have a real ORM row (not a duck-typed fake)
            if hasattr(session, "id") and session.id is not None:
                from .planner_session_repo import commit_session_row
                commit_session_row(session)
        except Exception:
            logger.exception("Failed to persist plan state")


# ─── Helpers ───────────────────────────────────────────────────────────────────

def _summarize_outputs(outputs: dict) -> str:
    """Create a brief human-readable summary of step outputs."""
    if not outputs:
        return ""
    # Take first 3 keys as summary
    parts = []
    for k, v in list(outputs.items())[:3]:
        if isinstance(v, str) and len(v) > 50:
            v = v[:50] + "…"
        parts.append(f"{k}={v}")
    return "; ".join(parts)
