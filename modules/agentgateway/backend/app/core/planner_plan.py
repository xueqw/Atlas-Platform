"""Planner Plan + Loop Engine — Plan / PlanStep schema and core operations.

Implements the Plan-first architecture: structured plans stored in
PlannerSession.planning_state_json, with step-level state machine and
dependency-aware progression.

Feature flag: PLANNER_PLAN_LOOP=on|off (default off).
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


# ─── Feature Flag ──────────────────────────────────────────────────────────────

def plan_loop_enabled() -> bool:
    """Feature flag for the Plan+Loop planner mode.

    Default OFF — opt in via ``PLANNER_PLAN_LOOP=on`` (1/true/yes).
    Read per-call so it can be toggled without a process restart in tests.
    """
    raw = (os.environ.get("PLANNER_PLAN_LOOP") or "off").strip().lower()
    return raw in {"1", "on", "true", "yes"}


# ─── Enums ─────────────────────────────────────────────────────────────────────

class StepStatus(str, Enum):
    pending = "pending"
    running = "running"
    done = "done"
    failed = "failed"
    blocked = "blocked"
    waiting_user = "waiting_user"
    skipped = "skipped"


class ExecutorType(str, Enum):
    deterministic = "deterministic"
    llm = "llm"
    tool_react = "tool_react"
    user_confirmation = "user_confirmation"


class PlanStatus(str, Enum):
    running = "running"
    waiting_user = "waiting_user"
    completed = "completed"
    failed = "failed"


class StopReason(str, Enum):
    completed = "completed"
    waiting_user = "waiting_user"
    budget_exhausted = "budget_exhausted"
    step_failed = "step_failed"
    fallback_legacy = "fallback_legacy"


SCHEMA_VERSION = "plan_loop/v1"


# ─── Confirmation Schema ───────────────────────────────────────────────────────

class ConfirmationRequest(BaseModel):
    """Structured confirmation request for user_confirmation steps."""
    request_id: str = Field(default_factory=lambda: f"confirm_{uuid.uuid4().hex[:8]}")
    kind: str = "proposal_confirm"  # proposal_confirm | option_select | missing_info | repair_confirm
    prompt: str = ""
    options: list[dict[str, str]] = Field(default_factory=list)  # [{id, label}]
    selected: Optional[str] = None
    payload: dict[str, Any] = Field(default_factory=dict)
    resolved_at: Optional[datetime] = None


# ─── PlanStep Schema ───────────────────────────────────────────────────────────

class ArtifactRef(BaseModel):
    """Reference to a business object produced by a step."""
    type: str  # "proposal" | "draft_agent" | "compile_result" | "file_artifact"
    id: Any  # int or str depending on the business object


class PlanStep(BaseModel):
    """A single step in the Planner's structured plan."""
    id: str
    title: str
    status: StepStatus = StepStatus.pending
    executor_type: ExecutorType = ExecutorType.deterministic
    depends_on: list[str] = Field(default_factory=list)
    inputs: dict[str, Any] = Field(default_factory=dict)
    outputs: dict[str, Any] = Field(default_factory=dict)
    artifact_refs: list[ArtifactRef] = Field(default_factory=list)
    attempt: int = 0
    max_attempts: int = 2
    requires_user: bool = False
    confirmation: Optional[ConfirmationRequest] = None
    error: Optional[str] = None
    repair_of: Optional[str] = None
    blocking_reason: Optional[str] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    created_at: Optional[datetime] = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: Optional[datetime] = None


# ─── Plan Schema ───────────────────────────────────────────────────────────────

class Plan(BaseModel):
    """Top-level plan object stored in planning_state_json."""
    schema_version: str = SCHEMA_VERSION
    plan_id: str = Field(default_factory=lambda: f"plan_{uuid.uuid4().hex[:12]}")
    goal: str = ""
    mode: str = "create"  # "create" | "replan"
    status: PlanStatus = PlanStatus.running
    steps: list[PlanStep] = Field(default_factory=list)
    current_step_id: Optional[str] = None
    revision: int = 1
    planner_mode: str = "plan_loop"  # "plan_loop" | "legacy"
    created_at: Optional[datetime] = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: Optional[datetime] = None


# ─── Plan Operations ───────────────────────────────────────────────────────────

def create_initial_plan(user_input: str, task_type: str = "create", mode: str = "create") -> Plan:
    """Generate a 5-7 step initial plan based on task type.

    This creates the deterministic plan template. The actual step content
    (inputs/outputs) is filled by the Loop Runner during execution.
    """
    steps = [
        PlanStep(
            id="understand_requirement",
            title="理解需求",
            executor_type=ExecutorType.llm,
            inputs={"user_input": user_input},
        ),
        PlanStep(
            id="collect_context",
            title="收集上下文",
            executor_type=ExecutorType.deterministic,
            depends_on=["understand_requirement"],
        ),
        PlanStep(
            id="recall_memory",
            title="召回记忆",
            executor_type=ExecutorType.deterministic,
            depends_on=["understand_requirement"],
        ),
        PlanStep(
            id="match_capabilities",
            title="匹配能力",
            executor_type=ExecutorType.deterministic,
            depends_on=["collect_context"],
        ),
        PlanStep(
            id="design_architecture",
            title="设计方案",
            executor_type=ExecutorType.llm,
            depends_on=["collect_context", "recall_memory", "match_capabilities"],
        ),
        PlanStep(
            id="confirm_proposal",
            title="确认方案",
            executor_type=ExecutorType.user_confirmation,
            requires_user=True,
            depends_on=["design_architecture"],
            confirmation=ConfirmationRequest(
                kind="proposal_confirm",
                prompt="是否确认该方案并生成草案？",
                options=[
                    {"id": "confirm", "label": "确认"},
                    {"id": "revise", "label": "修改"},
                    {"id": "reject", "label": "拒绝"},
                ],
            ),
        ),
        PlanStep(
            id="compile_draft",
            title="编译验证",
            executor_type=ExecutorType.deterministic,
            depends_on=["confirm_proposal"],
        ),
    ]

    plan = Plan(
        goal=user_input[:200],
        mode=mode,
        steps=steps,
        current_step_id="understand_requirement",
    )
    return plan


def next_runnable_step(plan: Plan) -> tuple[Optional[PlanStep], Optional[StopReason]]:
    """Return the next executable step following dependency-aware rules.

    Rules (in priority order):
    1. If any step is waiting_user → None, stop_reason=waiting_user
    2. If any step is failed with attempt >= max_attempts → None, stop_reason=step_failed
    3. Prefer current_step_id if it is pending/running
    4. Only return steps whose depends_on are all done/skipped
    5. Repair steps take priority over normal pending steps
    6. All steps done/skipped → None, stop_reason=completed
    """
    # Rule 1: waiting_user blocks the loop
    for step in plan.steps:
        if step.status == StepStatus.waiting_user:
            return None, StopReason.waiting_user

    # Rule 2: unrecoverable failure blocks the loop
    for step in plan.steps:
        if step.status == StepStatus.failed and step.attempt >= step.max_attempts:
            return None, StopReason.step_failed

    # Build set of done/skipped step ids for dependency checking
    completed_ids = {s.id for s in plan.steps if s.status in (StepStatus.done, StepStatus.skipped)}

    def _deps_satisfied(step: PlanStep) -> bool:
        return all(dep in completed_ids for dep in step.depends_on)

    # Rule 3: prefer current_step_id
    if plan.current_step_id:
        for step in plan.steps:
            if step.id == plan.current_step_id and step.status in (StepStatus.pending, StepStatus.running):
                if _deps_satisfied(step):
                    return step, None

    # Rule 5: repair steps first
    repair_candidates = [
        s for s in plan.steps
        if s.status == StepStatus.pending and s.repair_of is not None and _deps_satisfied(s)
    ]
    if repair_candidates:
        return repair_candidates[0], None

    # Rule 4: first pending step with deps satisfied
    for step in plan.steps:
        if step.status == StepStatus.pending and _deps_satisfied(step):
            return step, None

    # Rule 6: all done
    all_terminal = all(s.status in (StepStatus.done, StepStatus.skipped, StepStatus.failed) for s in plan.steps)
    if all_terminal:
        return None, StopReason.completed

    # Blocked — deps not satisfied but not all done
    return None, StopReason.step_failed


def update_step_status(
    plan: Plan,
    step_id: str,
    status: StepStatus,
    outputs: Optional[dict] = None,
    error: Optional[str] = None,
    artifact_refs: Optional[list[ArtifactRef]] = None,
) -> Plan:
    """Update a step's status, outputs, and timestamps. Returns the mutated plan."""
    now = datetime.now(timezone.utc)
    for step in plan.steps:
        if step.id == step_id:
            step.status = status
            step.updated_at = now
            if outputs:
                step.outputs.update(outputs)
            if error is not None:
                step.error = error
            if artifact_refs:
                step.artifact_refs.extend(artifact_refs)
            if status == StepStatus.running:
                step.started_at = step.started_at or now
                step.attempt += 1
            elif status in (StepStatus.done, StepStatus.failed, StepStatus.skipped):
                step.completed_at = now
            break

    # Update plan-level state
    plan.updated_at = now
    if status == StepStatus.waiting_user:
        plan.status = PlanStatus.waiting_user
    elif all(s.status in (StepStatus.done, StepStatus.skipped) for s in plan.steps):
        plan.status = PlanStatus.completed

    # Advance current_step_id
    next_step, _ = next_runnable_step(plan)
    plan.current_step_id = next_step.id if next_step else None

    return plan


def insert_repair_step(plan: Plan, after_step_id: str, repair_config: dict) -> Plan:
    """Insert a repair step after the failed step. Increments plan revision."""
    now = datetime.now(timezone.utc)
    repair_step = PlanStep(
        id=f"repair_{after_step_id}_{plan.revision}",
        title=repair_config.get("title", f"修复: {after_step_id}"),
        executor_type=ExecutorType(repair_config.get("executor_type", "deterministic")),
        depends_on=[],  # repair step has no deps — it runs next
        inputs=repair_config.get("inputs", {}),
        repair_of=after_step_id,
        created_at=now,
    )

    # Insert after the failed step
    insert_idx = None
    for i, step in enumerate(plan.steps):
        if step.id == after_step_id:
            insert_idx = i + 1
            break

    if insert_idx is not None:
        plan.steps.insert(insert_idx, repair_step)
        # Make steps that depended on the failed step now depend on repair
        for step in plan.steps:
            if after_step_id in step.depends_on and step.id != repair_step.id:
                step.depends_on = [
                    repair_step.id if d == after_step_id else d
                    for d in step.depends_on
                ]

    plan.revision += 1
    plan.updated_at = now
    plan.current_step_id = repair_step.id
    return plan


# ─── Persistence ───────────────────────────────────────────────────────────────

def save_plan(session_row, plan: Plan) -> None:
    """Serialize Plan to the session's planning_state_json field.

    Merges into existing JSON to preserve other state fields.
    Caller is responsible for DB commit.
    """
    import json
    existing = {}
    if session_row.planning_state_json:
        try:
            existing = json.loads(session_row.planning_state_json)
        except (json.JSONDecodeError, TypeError):
            existing = {}

    existing["plan"] = plan.model_dump(mode="json")
    existing["planner_mode"] = "plan_loop"
    session_row.planning_state_json = json.dumps(existing, ensure_ascii=False)


def load_plan(session_row) -> Optional[Plan]:
    """Load Plan from session's planning_state_json. Returns None if not present."""
    import json
    if not session_row.planning_state_json:
        return None
    try:
        data = json.loads(session_row.planning_state_json)
    except (json.JSONDecodeError, TypeError):
        return None
    plan_data = data.get("plan")
    if not plan_data:
        return None
    try:
        return Plan.model_validate(plan_data)
    except Exception:
        return None


def is_legacy_session(session_row) -> bool:
    """Check if session is legacy (no plan_loop schema_version).

    Returns True if:
    - planning_state_json is empty or unparseable
    - No "plan" key exists
    - schema_version is missing or not plan_loop/v1
    """
    import json
    if not session_row.planning_state_json:
        return True
    try:
        data = json.loads(session_row.planning_state_json)
    except (json.JSONDecodeError, TypeError):
        return True
    plan_data = data.get("plan")
    if not plan_data:
        return True
    return plan_data.get("schema_version") != SCHEMA_VERSION


def get_planner_mode(session_row) -> str:
    """Return the planner_mode for this session: 'plan_loop' or 'legacy'."""
    import json
    if not session_row.planning_state_json:
        return "legacy"
    try:
        data = json.loads(session_row.planning_state_json)
    except (json.JSONDecodeError, TypeError):
        return "legacy"
    return data.get("planner_mode", "legacy")


def determine_session_mode(session_row) -> str:
    """Determine which mode a session should use.

    For new sessions: check PLANNER_PLAN_LOOP flag.
    For existing sessions: check stored planner_mode / schema_version.
    """
    stored = get_planner_mode(session_row)
    if stored == "plan_loop":
        return "plan_loop"
    # New session — use feature flag
    if plan_loop_enabled():
        return "plan_loop"
    return "legacy"


# ─── plan_update Payload ───────────────────────────────────────────────────────

def build_plan_update_payload(
    conversation_id: str,
    run_id: str,
    plan: Plan,
    stop_reason: Optional[StopReason] = None,
) -> dict:
    """Build the plan_update WS event payload."""
    return {
        "type": "plan_update",
        "conversation_id": conversation_id,
        "run_id": run_id,
        "plan": plan.model_dump(mode="json"),
        "stop_reason": stop_reason.value if stop_reason else None,
    }
