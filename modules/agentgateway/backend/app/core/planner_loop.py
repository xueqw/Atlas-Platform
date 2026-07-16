"""Planner Loop Runner — state machine that drives Plan step execution.

Implements run_until_pause_or_complete: iterates through Plan steps,
dispatches to the appropriate executor, validates outputs, persists state,
and emits events until a stop condition is met.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional, Protocol

from .planner_plan import (
    ArtifactRef,
    Plan,
    PlanStatus,
    PlanStep,
    StepStatus,
    StopReason,
    insert_repair_step,
    next_runnable_step,
    update_step_status,
)

logger = logging.getLogger(__name__)


# ─── Loop Config ───────────────────────────────────────────────────────────────

DEFAULT_MAX_ITERATIONS = 12


@dataclass
class LoopConfig:
    """Configuration for the plan loop runner."""
    max_iterations: int = DEFAULT_MAX_ITERATIONS


@dataclass
class LoopResult:
    """Result of a loop run."""
    plan: Plan
    stop_reason: StopReason
    iterations_used: int = 0
    last_step_id: Optional[str] = None
    error: Optional[str] = None


# ─── Event Emitter Protocol ────────────────────────────────────────────────────

class EventEmitter(Protocol):
    """Protocol for emitting plan events to the frontend."""

    async def emit_plan_update(self, plan: Plan, stop_reason: Optional[StopReason] = None) -> None:
        ...

    async def emit_step_started(self, step: PlanStep) -> None:
        ...

    async def emit_step_completed(self, step: PlanStep, duration_ms: int) -> None:
        ...

    async def emit_step_failed(self, step: PlanStep, error: str) -> None:
        ...


# ─── Step Executor Protocol ────────────────────────────────────────────────────

class StepResult:
    """Result of executing a single step."""
    def __init__(
        self,
        status: str = "success",  # "success" | "failed" | "waiting_user"
        outputs: Optional[dict[str, Any]] = None,
        error: Optional[str] = None,
        artifact_refs: Optional[list[ArtifactRef]] = None,
        can_auto_repair: bool = False,
        repair_config: Optional[dict] = None,
    ):
        self.status = status
        self.outputs = outputs or {}
        self.error = error
        self.artifact_refs = artifact_refs or []
        self.can_auto_repair = can_auto_repair
        self.repair_config = repair_config


class StepExecutorDispatcher(Protocol):
    """Protocol for dispatching step execution to the appropriate executor."""

    async def execute(self, step: PlanStep, plan: Plan, session: Any) -> StepResult:
        ...


# ─── Persistence Protocol ──────────────────────────────────────────────────────

class PlanPersister(Protocol):
    """Protocol for persisting plan state."""

    async def persist(self, session: Any, plan: Plan) -> None:
        ...


# ─── Loop Runner ───────────────────────────────────────────────────────────────

async def run_until_pause_or_complete(
    session: Any,
    plan: Plan,
    executor: StepExecutorDispatcher,
    emitter: EventEmitter,
    persister: PlanPersister,
    config: Optional[LoopConfig] = None,
) -> LoopResult:
    """Main loop: execute plan steps until a stop condition is met.

    Stop conditions:
    - All steps completed → StopReason.completed
    - Step requires user confirmation → StopReason.waiting_user
    - Budget exhausted (max_iterations) → StopReason.budget_exhausted
    - Step failed and cannot auto-repair → StopReason.step_failed
    """
    config = config or LoopConfig()
    iterations = 0

    while iterations < config.max_iterations:
        # Find next step
        step, stop_reason = next_runnable_step(plan)

        if step is None:
            # No more steps to execute
            await emitter.emit_plan_update(plan, stop_reason)
            await persister.persist(session, plan)
            return LoopResult(
                plan=plan,
                stop_reason=stop_reason or StopReason.completed,
                iterations_used=iterations,
            )

        # Mark step as running
        plan = update_step_status(plan, step.id, StepStatus.running)
        await emitter.emit_step_started(step)
        await emitter.emit_plan_update(plan)

        # Execute step
        start_time = time.monotonic()
        try:
            result = await executor.execute(step, plan, session)
        except Exception as exc:
            logger.exception(f"Step {step.id} raised unexpected exception")
            result = StepResult(status="failed", error=str(exc))

        duration_ms = int((time.monotonic() - start_time) * 1000)

        # Handle result
        if result.status == "success":
            plan = update_step_status(
                plan, step.id, StepStatus.done,
                outputs=result.outputs,
                artifact_refs=result.artifact_refs or None,
            )
            await emitter.emit_step_completed(step, duration_ms)

        elif result.status == "waiting_user":
            plan = update_step_status(plan, step.id, StepStatus.waiting_user)
            plan.status = PlanStatus.waiting_user
            await emitter.emit_plan_update(plan, StopReason.waiting_user)
            await persister.persist(session, plan)
            return LoopResult(
                plan=plan,
                stop_reason=StopReason.waiting_user,
                iterations_used=iterations + 1,
                last_step_id=step.id,
            )

        elif result.status == "failed":
            plan = update_step_status(
                plan, step.id, StepStatus.failed,
                error=result.error,
            )
            await emitter.emit_step_failed(step, result.error or "Unknown error")

            # Decide repair path
            if result.can_auto_repair and result.repair_config and step.attempt < step.max_attempts:
                # Auto repair: insert repair step and continue
                plan = insert_repair_step(plan, step.id, result.repair_config)
                logger.info(f"Auto-repair step inserted after {step.id}")
            else:
                # Unrecoverable or max attempts reached
                plan.status = PlanStatus.failed
                await emitter.emit_plan_update(plan, StopReason.step_failed)
                await persister.persist(session, plan)
                return LoopResult(
                    plan=plan,
                    stop_reason=StopReason.step_failed,
                    iterations_used=iterations + 1,
                    last_step_id=step.id,
                    error=result.error,
                )

        # Persist after each step
        await emitter.emit_plan_update(plan)
        await persister.persist(session, plan)
        iterations += 1

    # Budget exhausted
    await emitter.emit_plan_update(plan, StopReason.budget_exhausted)
    await persister.persist(session, plan)
    return LoopResult(
        plan=plan,
        stop_reason=StopReason.budget_exhausted,
        iterations_used=iterations,
    )


# ─── Session Resume ────────────────────────────────────────────────────────────

async def resume_loop(
    session: Any,
    executor: StepExecutorDispatcher,
    emitter: EventEmitter,
    persister: PlanPersister,
    config: Optional[LoopConfig] = None,
) -> Optional[LoopResult]:
    """Resume loop from a persisted session.

    Loads the plan from session, checks if it's resumable, and continues.
    Returns None if session is legacy or plan is already completed.
    """
    from .planner_plan import load_plan, is_legacy_session

    if is_legacy_session(session):
        return None

    plan = load_plan(session)
    if plan is None:
        return None

    if plan.status == PlanStatus.completed:
        return None

    # If plan was waiting_user, don't resume automatically
    if plan.status == PlanStatus.waiting_user:
        await emitter.emit_plan_update(plan, StopReason.waiting_user)
        return LoopResult(
            plan=plan,
            stop_reason=StopReason.waiting_user,
            iterations_used=0,
        )

    # Resume execution
    return await run_until_pause_or_complete(
        session=session,
        plan=plan,
        executor=executor,
        emitter=emitter,
        persister=persister,
        config=config,
    )


# ─── User Confirmation Resolution ─────────────────────────────────────────────

def resolve_user_confirmation(
    plan: Plan,
    request_id: str,
    selected: str,
    payload: Optional[dict] = None,
) -> Optional[Plan]:
    """Resolve a waiting_user step by matching confirmation request_id.

    Returns updated plan if match found, None otherwise.
    The step is NOT automatically marked done — only confirmation is resolved.
    The caller should then mark it done and resume the loop.
    """
    from datetime import datetime, timezone

    for step in plan.steps:
        if step.status != StepStatus.waiting_user:
            continue
        if step.confirmation and step.confirmation.request_id == request_id:
            step.confirmation.selected = selected
            step.confirmation.resolved_at = datetime.now(timezone.utc)
            if payload:
                step.confirmation.payload.update(payload)
            # Mark step done
            step.status = StepStatus.done
            step.completed_at = datetime.now(timezone.utc)
            step.outputs["confirmation_selected"] = selected
            # Update plan status back to running
            plan.status = PlanStatus.running
            plan.updated_at = datetime.now(timezone.utc)
            # Advance current_step_id
            next_step, _ = next_runnable_step(plan)
            plan.current_step_id = next_step.id if next_step else None
            return plan

    return None  # No matching request_id found
