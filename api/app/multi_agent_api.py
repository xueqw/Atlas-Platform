"""Authenticated management API for durable multi-agent orchestration."""
from __future__ import annotations

from typing import Any
import asyncio
import json
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .auth import current_user, current_workspace_id
from .database import SessionLocal, get_db
from .governance_models import OrchestrationRunRecord
from .models import Agent, AgentVersion, User
from .model_gateway import complete
from .multi_agent_repository import MultiAgentRepository, OrchestrationScope
from .multi_agent_runtime import ResultEnvelope, WorkerSpec
from .multi_agent_service import (
    CandidateAssetPublisher, DurableOrchestrator, TaskPlan,
    deterministic_candidate_replay,
)
from .multi_agent_runtime_adapter import ServerToolRegistry, SubagentRuntimeExecutor


router = APIRouter(prefix="/api/orchestrations", tags=["multi-agent-orchestration"])
_BACKGROUND_RUNS: dict[tuple[str, str], asyncio.Task[Any]] = {}


class WorkerSpecIn(BaseModel):
    worker_id: str
    version: str = "1.0.0"
    role: str
    objective: str
    input_schema: dict[str, Any] = Field(default_factory=lambda: {"type": "object"})
    output_schema: dict[str, Any] = Field(default_factory=lambda: {"type": "object"})
    allowed_tools: set[str] = Field(default_factory=set)
    max_steps: int = Field(default=8, ge=1)
    timeout_seconds: int = Field(default=120, ge=1)
    lifecycle: str = "ephemeral"


class TaskPlanIn(BaseModel):
    task_id: str
    worker_id: str
    payload: dict[str, Any]
    depends_on: set[str] = Field(default_factory=set)
    artifact_refs: list[str] = Field(default_factory=list)
    max_attempts: int = Field(default=2, ge=1)
    timeout_seconds: int = Field(default=120, ge=1)


class CreateOrchestrationIn(BaseModel):
    agent_id: str
    workers: list[WorkerSpecIn]
    tasks: list[TaskPlanIn]
    concurrency: int = Field(default=3, ge=1, le=6)
    max_replans: int = Field(default=2, ge=0, le=2)
    conflict_keys: set[str] = Field(default_factory=set)


class GoalOrchestrationIn(BaseModel):
    """High-level organizer input; workers and the DAG are generated server-side."""

    agent_id: str
    goal: str = Field(min_length=1, max_length=20_000)
    max_workers: int = Field(default=4, ge=1, le=6)
    concurrency: int = Field(default=3, ge=1, le=6)
    max_replans: int = Field(default=2, ge=0, le=2)
    allowed_tools: set[str] = Field(default_factory=set)
    conflict_keys: set[str] = Field(default_factory=set)


class TicketIn(BaseModel):
    worker_id: str
    tool_name: str
    parameters: dict[str, Any]
    scope: set[str] = Field(default_factory=set)
    ttl_seconds: int = Field(default=300, ge=1, le=3600)
    resource_version: str = Field(default="current", min_length=1, max_length=160)


class ExecuteOrchestrationIn(BaseModel):
    authorizations: dict[str, dict[str, str]] = Field(default_factory=dict)


class CandidateIn(BaseModel):
    candidate_type: str
    name: str
    content: dict[str, Any]
    evidence_ids: list[str]
    idempotency_key: str


class RedactIn(BaseModel):
    content: dict[str, Any]


class EvaluateIn(BaseModel):
    replay_report: dict[str, Any]
    minimum_pass_rate: float = Field(default=0.8, ge=0, le=1)


class ApproveIn(BaseModel):
    human_approval: bool


class VersionIn(BaseModel):
    version: str


def _scope_for_run(db: Session, *, run_id: str, workspace_id: str, user_id: str) -> OrchestrationScope:
    record = db.scalar(select(OrchestrationRunRecord).where(
        OrchestrationRunRecord.run_id == run_id,
        OrchestrationRunRecord.workspace_id == workspace_id,
        OrchestrationRunRecord.user_id == user_id,
    ))
    if record is None:
        raise HTTPException(404, "orchestration run not found")
    return OrchestrationScope(workspace_id, user_id, record.agent_id, run_id)


def _pinned_version(agent: Agent, db: Session) -> AgentVersion:
    version_id = agent.published_version_id or agent.current_version_id
    version = db.scalar(select(AgentVersion).where(
        AgentVersion.id == version_id, AgentVersion.agent_id == agent.id,
    )) if version_id else None
    if version is None:
        raise HTTPException(409, "agent must have an immutable version before orchestration")
    return version


def _json_object(raw: str) -> dict[str, Any] | None:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text
        text = text.rsplit("```", 1)[0].strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        return None
    try:
        value = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _fallback_goal_plan(goal: str) -> dict[str, Any]:
    """A bounded, executable plan when the organizer model is unavailable."""
    return {
        "workers": [{
            "worker_id": "primary-worker",
            "version": "1.0.0",
            "role": "goal-specialist",
            "objective": goal,
            "input_schema": {
                "type": "object",
                "required": ["goal"],
                "properties": {"goal": {"type": "string"}},
            },
            "output_schema": {
                "type": "object",
                "required": ["answer"],
                "properties": {"answer": {"type": "string"}},
            },
            "allowed_tools": [],
        }],
        "tasks": [{
            "task_id": "primary-task",
            "worker_id": "primary-worker",
            "payload": {"goal": goal},
            "depends_on": [],
        }],
        "planner_mode": "deterministic_fallback",
    }


def _normalize_goal_plan(
    raw: dict[str, Any] | None,
    *,
    goal: str,
    max_workers: int,
    allowed_tools: set[str],
) -> tuple[list[WorkerSpec], list[TaskPlan], str]:
    candidate = raw or _fallback_goal_plan(goal)
    mode = str(candidate.get("planner_mode") or ("model" if raw else "deterministic_fallback"))
    workers_raw = candidate.get("workers")
    tasks_raw = candidate.get("tasks")
    if not isinstance(workers_raw, list) or not isinstance(tasks_raw, list):
        candidate = _fallback_goal_plan(goal)
        workers_raw, tasks_raw, mode = candidate["workers"], candidate["tasks"], candidate["planner_mode"]
    if not 1 <= len(workers_raw) <= max_workers or not 1 <= len(tasks_raw) <= max_workers:
        raise ValueError("organizer plan exceeds the bounded worker/task cap")

    workers: list[WorkerSpec] = []
    for item in workers_raw:
        if not isinstance(item, dict):
            raise ValueError("organizer workers must be JSON objects")
        requested_tools = {str(value) for value in item.get("allowed_tools", [])}
        # The organizer may only narrow the caller's trusted server allowlist.
        tools = requested_tools & allowed_tools
        spec = WorkerSpec(
            worker_id=str(item.get("worker_id") or ""),
            version=str(item.get("version") or "1.0.0"),
            role=str(item.get("role") or "specialist"),
            objective=str(item.get("objective") or goal),
            input_schema=item.get("input_schema") if isinstance(item.get("input_schema"), dict) else {"type": "object"},
            output_schema=item.get("output_schema") if isinstance(item.get("output_schema"), dict) else {
                "type": "object", "required": ["answer"],
                "properties": {"answer": {"type": "string"}},
            },
            allowed_tools=frozenset(tools),
            max_steps=min(max(int(item.get("max_steps", 8)), 1), 16),
            timeout_seconds=min(max(int(item.get("timeout_seconds", 120)), 1), 600),
            lifecycle="ephemeral",
        )
        spec.validate()
        workers.append(spec)

    worker_ids = {item.worker_id for item in workers}
    tasks: list[TaskPlan] = []
    for item in tasks_raw:
        if not isinstance(item, dict):
            raise ValueError("organizer tasks must be JSON objects")
        worker_id = str(item.get("worker_id") or "")
        if worker_id not in worker_ids:
            raise ValueError("organizer task references an unknown worker")
        payload = item.get("payload")
        if not isinstance(payload, dict):
            payload = {"goal": str(item.get("objective") or goal)}
        tasks.append(TaskPlan(
            task_id=str(item.get("task_id") or ""),
            worker_id=worker_id,
            payload=payload,
            depends_on=frozenset(str(value) for value in item.get("depends_on", [])),
            max_attempts=min(max(int(item.get("max_attempts", 2)), 1), 3),
            timeout_seconds=min(max(int(item.get("timeout_seconds", 120)), 1), 600),
        ))
    return workers, tasks, mode


async def _organizer_plan(payload: GoalOrchestrationIn, agent: Agent) -> dict[str, Any] | None:
    system = (
        "You are the Atlas organizer. Decompose the goal into independent tasks and create ephemeral workers. "
        "Return JSON only with workers[] and tasks[]. Each worker needs worker_id, role, objective, "
        "input_schema, output_schema and allowed_tools. Each task needs task_id, worker_id, payload and "
        "depends_on. Prefer parallel dependency-free tasks, use at most the requested cap, and never invent "
        "tools outside the supplied allowlist. Worker inputs and outputs must be JSON objects."
    )
    try:
        response = await complete([
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps({
                "goal": payload.goal,
                "max_workers": payload.max_workers,
                "allowed_tools": sorted(payload.allowed_tools),
            }, ensure_ascii=False)},
        ], model=agent.model or None)
    except Exception:
        # Planning has not dispatched a worker or crossed a side-effect
        # boundary, so a deterministic single-worker plan is the safe fallback.
        return None
    return _json_object(response)


def _make_replan_handler(*, model: str | None):
    """Create the one bounded replan path shared by foreground/background runs."""

    async def replan(_scope, failures):
        response = await complete([{
            "role": "system", "content": (
                "Return JSON only: {\"tasks\":[...]}. Replan failed tasks using existing worker IDs. "
                "Each new task needs task_id, worker_id, payload, depends_on."
            )
        }, {"role": "user", "content": json.dumps([
            {"task_id": item.task_id, "worker_id": item.worker_id,
             "error": item.error, "category": item.error_category}
            for item in failures
        ], ensure_ascii=False)}], model=model or None)
        raw = _json_object(response) or {}
        rows = raw.get("tasks") if isinstance(raw.get("tasks"), list) else []
        return [TaskPlan(
            task_id=str(item.get("task_id") or ""),
            worker_id=str(item.get("worker_id") or ""),
            payload=item.get("payload") if isinstance(item.get("payload"), dict) else {},
            depends_on=frozenset(str(value) for value in item.get("depends_on", [])),
        ) for item in rows if isinstance(item, dict)]

    return replan


def _persist_tool_discovery_warnings(
    db: Session,
    *,
    scope: OrchestrationScope,
    registry: ServerToolRegistry,
    emit_audit: bool = True,
) -> None:
    """Expose fail-closed dynamic-tool discovery failures in state and audit."""
    if not registry.discovery_warnings:
        return
    repository = MultiAgentRepository(db)
    record = repository.get_run(scope)
    scheduler_state = dict(record.scheduler_state or {})
    scheduler_state["dependency_warnings"] = [dict(item) for item in registry.discovery_warnings]
    repository.save_run(
        scope,
        status=record.status,
        scheduler_state=scheduler_state,
        aggregate=record.aggregate,
    )
    if emit_audit:
        repository.append_audit(
            scope,
            action="tool_registry.discovery",
            decision="dependency_warning",
            details={"warnings": [dict(item) for item in registry.discovery_warnings]},
        )


@router.post("", status_code=201)
def create_orchestration(
    payload: CreateOrchestrationIn,
    user: User = Depends(current_user),
    workspace_id: str = Depends(current_workspace_id),
    db: Session = Depends(get_db),
):
    agent = db.scalar(select(Agent).where(
        Agent.id == payload.agent_id, Agent.workspace_id == workspace_id
    ))
    if agent is None:
        raise HTTPException(404, "agent not found")
    version = _pinned_version(agent, db)
    scope = OrchestrationScope(workspace_id, user.id, payload.agent_id, str(uuid.uuid4()))
    try:
        state = DurableOrchestrator(db).create(
            scope,
            workers=[WorkerSpec(
                item.worker_id, item.version, item.role, item.objective,
                item.input_schema, item.output_schema, frozenset(item.allowed_tools),
                item.max_steps, item.timeout_seconds, item.lifecycle,
            ) for item in payload.workers],
            tasks=[TaskPlan(
                item.task_id, item.worker_id, item.payload, frozenset(item.depends_on),
                tuple(item.artifact_refs), item.max_attempts, item.timeout_seconds,
            ) for item in payload.tasks],
            concurrency=payload.concurrency, max_replans=payload.max_replans,
            conflict_keys=payload.conflict_keys,
            parent_agent_version_id=version.id,
            user_tool_grants={name for item in payload.workers for name in item.allowed_tools},
            orchestrator_tool_grants={name for item in payload.workers for name in item.allowed_tools},
        )
    except (ValueError, LookupError) as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"run_id": scope.run_id, "status": "created", "scheduler_state": state}


@router.post("/plan", status_code=201)
async def plan_orchestration(
    payload: GoalOrchestrationIn,
    user: User = Depends(current_user),
    workspace_id: str = Depends(current_workspace_id),
    db: Session = Depends(get_db),
):
    """Let the organizer create isolated ephemeral Agents and a bounded DAG."""
    agent = db.scalar(select(Agent).where(
        Agent.id == payload.agent_id, Agent.workspace_id == workspace_id
    ))
    if agent is None:
        raise HTTPException(404, "agent not found")
    version = _pinned_version(agent, db)
    raw_plan = await _organizer_plan(payload, agent)
    try:
        workers, tasks, planner_mode = _normalize_goal_plan(
            raw_plan,
            goal=payload.goal,
            max_workers=payload.max_workers,
            allowed_tools=payload.allowed_tools,
        )
        scope = OrchestrationScope(workspace_id, user.id, agent.id, str(uuid.uuid4()))
        state = DurableOrchestrator(db).create(
            scope,
            workers=workers,
            tasks=tasks,
            concurrency=min(payload.concurrency, len(workers)),
            max_replans=payload.max_replans,
            conflict_keys=payload.conflict_keys,
            parent_agent_version_id=version.id,
            user_tool_grants=payload.allowed_tools,
            orchestrator_tool_grants=payload.allowed_tools,
        )
        MultiAgentRepository(db).append_audit(
            scope,
            action="orchestration.planned",
            decision=planner_mode,
            details={"goal": payload.goal, "worker_count": len(workers), "task_count": len(tasks)},
        )
    except (ValueError, LookupError) as exc:
        raise HTTPException(422, str(exc)) from exc
    return {
        "run_id": scope.run_id,
        "status": "created",
        "planner_mode": planner_mode,
        "workers": [item.as_dict() for item in workers],
        "tasks": [{
            "task_id": item.task_id, "worker_id": item.worker_id,
            "payload": item.payload, "depends_on": sorted(item.depends_on),
        } for item in tasks],
        "scheduler_state": state,
    }


@router.get("/{run_id}")
def get_orchestration(
    run_id: str,
    user: User = Depends(current_user),
    workspace_id: str = Depends(current_workspace_id),
    db: Session = Depends(get_db),
):
    scope = _scope_for_run(db, run_id=run_id, workspace_id=workspace_id, user_id=user.id)
    record = MultiAgentRepository(db).get_run(scope)
    return {
        "run_id": record.run_id, "agent_id": record.agent_id,
        "status": record.status, "scheduler_state": record.scheduler_state,
        "aggregate": record.aggregate,
    }


@router.post("/{run_id}/execute")
async def execute_orchestration(
    run_id: str,
    payload: ExecuteOrchestrationIn | None = None,
    background: bool = Query(default=False),
    user: User = Depends(current_user),
    workspace_id: str = Depends(current_workspace_id),
    db: Session = Depends(get_db),
):
    """Execute ready workers concurrently with isolated model contexts."""
    scope = _scope_for_run(db, run_id=run_id, workspace_id=workspace_id, user_id=user.id)
    agent = db.scalar(select(Agent).where(
        Agent.id == scope.agent_id, Agent.workspace_id == workspace_id
    ))
    if agent is None:
        raise HTTPException(404, "agent not found")

    authorizations = (payload or ExecuteOrchestrationIn()).authorizations
    actor_user_id = user.id
    try:
        deploy_config = json.loads(agent.deploy_config_json or "{}")
    except json.JSONDecodeError:
        deploy_config = {}
    configured_connectors = deploy_config.get("allowed_connectors")
    connectors = ([str(item) for item in configured_connectors]
                  if isinstance(configured_connectors, list) else [])
    tool_registry = await ServerToolRegistry.discover(connectors)
    _persist_tool_discovery_warnings(db, scope=scope, registry=tool_registry)
    replan = _make_replan_handler(model=agent.model)

    def cancellation_probe() -> bool:
        # Use a fresh session so cancellation written by another API process is
        # observable even while this worker owns a long-lived ORM identity map.
        with SessionLocal() as probe_db:
            status = probe_db.scalar(select(OrchestrationRunRecord.status).where(
                OrchestrationRunRecord.run_id == run_id,
                OrchestrationRunRecord.workspace_id == workspace_id,
                OrchestrationRunRecord.user_id == actor_user_id,
                OrchestrationRunRecord.agent_id == scope.agent_id,
            ))
        return status == "cancelled"

    if background:
        current = MultiAgentRepository(db).get_run(scope)
        MultiAgentRepository(db).save_run(
            scope, status="running", scheduler_state=dict(current.scheduler_state or {})
        )

        async def execute_in_background() -> None:
            with SessionLocal() as worker_db:
                worker_scope = _scope_for_run(
                    worker_db, run_id=run_id, workspace_id=workspace_id, user_id=user.id
                )
                worker_executor = SubagentRuntimeExecutor(
                    worker_db, worker_scope, model_complete=complete,
                    session_factory=SessionLocal, authorizations=authorizations,
                    tool_registry=tool_registry,
                    cancellation_probe=cancellation_probe,
                )
                await DurableOrchestrator(worker_db).execute(
                    worker_scope, worker_executor, replan=replan,
                    resume_task_ids=set(authorizations),
                    cancellation_probe=cancellation_probe,
                )
                # DurableOrchestrator rewrites its scheduler snapshot after
                # execution; restore dependency metadata without duplicating
                # the audit event emitted at discovery time.
                _persist_tool_discovery_warnings(
                    worker_db, scope=worker_scope, registry=tool_registry,
                    emit_audit=False,
                )

        key = (workspace_id, run_id)
        existing = _BACKGROUND_RUNS.get(key)
        if existing is None or existing.done():
            task = asyncio.create_task(execute_in_background(), name=f"atlas-orchestration:{run_id}")
            _BACKGROUND_RUNS[key] = task
            task.add_done_callback(lambda completed, run_key=key: _BACKGROUND_RUNS.pop(run_key, None))
        return {"run_id": run_id, "status": "running", "background": True}
    executor = SubagentRuntimeExecutor(
        db, scope, model_complete=complete, session_factory=SessionLocal,
        authorizations=authorizations, tool_registry=tool_registry,
        cancellation_probe=cancellation_probe,
    )

    aggregate = await DurableOrchestrator(db).execute(
        scope, executor, replan=replan,
        resume_task_ids=set(authorizations),
        cancellation_probe=cancellation_probe,
    )
    _persist_tool_discovery_warnings(
        db, scope=scope, registry=tool_registry, emit_audit=False,
    )
    record = MultiAgentRepository(db).get_run(scope)
    return {
        "run_id": run_id,
        "status": record.status,
        "aggregate": aggregate,
        "scheduler_state": record.scheduler_state,
    }


@router.post("/{run_id}/cancel")
def cancel_orchestration(
    run_id: str,
    user: User = Depends(current_user),
    workspace_id: str = Depends(current_workspace_id),
    db: Session = Depends(get_db),
):
    scope = _scope_for_run(db, run_id=run_id, workspace_id=workspace_id, user_id=user.id)
    state = DurableOrchestrator(db).cancel(scope, actor_id=user.id)
    task = _BACKGROUND_RUNS.get((workspace_id, run_id))
    if task is not None and not task.done():
        task.cancel()
    return {"run_id": run_id, "status": "cancelled", "scheduler_state": state}


@router.post("/{run_id}/authorization-tickets", status_code=201)
async def create_authorization_ticket(
    run_id: str, payload: TicketIn,
    user: User = Depends(current_user),
    workspace_id: str = Depends(current_workspace_id),
    db: Session = Depends(get_db),
):
    scope = _scope_for_run(db, run_id=run_id, workspace_id=workspace_id, user_id=user.id)
    agent = db.scalar(select(Agent).where(
        Agent.id == scope.agent_id, Agent.workspace_id == workspace_id
    ))
    try:
        deploy_config = json.loads(agent.deploy_config_json or "{}") if agent else {}
    except json.JSONDecodeError:
        deploy_config = {}
    allowed = deploy_config.get("allowed_connectors")
    connectors = [str(item) for item in allowed] if isinstance(allowed, list) else []
    tool_registry = await ServerToolRegistry.discover(connectors)
    _persist_tool_discovery_warnings(db, scope=scope, registry=tool_registry)
    trusted = tool_registry.get(payload.tool_name)
    if trusted is None:
        raise HTTPException(422, "tool is not in the trusted server registry")
    required_scope = set(trusted.required_scope)
    if not required_scope.issubset(payload.scope):
        raise HTTPException(422, "ticket scope does not cover the trusted tool policy")
    try:
        ticket = MultiAgentRepository(db).issue_authorization(
            scope, worker_id=payload.worker_id, tool_name=payload.tool_name,
            parameters=payload.parameters, ticket_scope=payload.scope,
            ttl_seconds=payload.ttl_seconds, resource_version=payload.resource_version,
        )
    except (LookupError, PermissionError, ValueError) as exc:
        raise HTTPException(422, str(exc)) from exc
    return {
        "ticket_id": ticket.ticket_id, "run_id": ticket.run_id,
        "worker_id": ticket.worker_id, "tool_name": ticket.tool_name,
        "parameter_digest": ticket.parameter_digest,
        "scope": sorted(ticket.scope), "expires_at": ticket.expires_at,
        "nonce": ticket.nonce, "resource_version": payload.resource_version,
    }


@router.post("/{run_id}/candidates", status_code=201)
def propose_candidate(
    run_id: str, payload: CandidateIn,
    user: User = Depends(current_user),
    workspace_id: str = Depends(current_workspace_id),
    db: Session = Depends(get_db),
):
    scope = _scope_for_run(db, run_id=run_id, workspace_id=workspace_id, user_id=user.id)
    try:
        candidate = CandidateAssetPublisher(db).propose(
            scope, candidate_type=payload.candidate_type, name=payload.name,
            content=payload.content, evidence_ids=payload.evidence_ids,
            actor_id=user.id, idempotency_key=payload.idempotency_key,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"candidate_id": candidate.candidate_id, "status": candidate.status}


@router.post("/{run_id}/candidates/{candidate_id}/redact")
def redact_candidate(
    run_id: str, candidate_id: str, payload: RedactIn,
    user: User = Depends(current_user), workspace_id: str = Depends(current_workspace_id),
    db: Session = Depends(get_db),
):
    scope = _scope_for_run(db, run_id=run_id, workspace_id=workspace_id, user_id=user.id)
    try:
        candidate = CandidateAssetPublisher(db).redact(
            scope, candidate_id, redacted_content=payload.content, actor_id=user.id
        )
    except (ValueError, LookupError) as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"candidate_id": candidate.candidate_id, "status": candidate.status}


@router.post("/{run_id}/candidates/{candidate_id}/evaluate")
def evaluate_candidate(
    run_id: str, candidate_id: str, payload: EvaluateIn,
    user: User = Depends(current_user), workspace_id: str = Depends(current_workspace_id),
    db: Session = Depends(get_db),
):
    scope = _scope_for_run(db, run_id=run_id, workspace_id=workspace_id, user_id=user.id)
    try:
        candidate = CandidateAssetPublisher(
            db, replay_executor=deterministic_candidate_replay
        ).evaluate(
            scope, candidate_id, replay_report=payload.replay_report,
            actor_id=user.id, minimum_pass_rate=payload.minimum_pass_rate,
        )
    except (ValueError, LookupError) as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"candidate_id": candidate.candidate_id, "status": candidate.status}


@router.post("/{run_id}/candidates/{candidate_id}/approve")
def approve_candidate(
    run_id: str, candidate_id: str, payload: ApproveIn,
    user: User = Depends(current_user), workspace_id: str = Depends(current_workspace_id),
    db: Session = Depends(get_db),
):
    scope = _scope_for_run(db, run_id=run_id, workspace_id=workspace_id, user_id=user.id)
    try:
        candidate = CandidateAssetPublisher(db).approve(
            scope, candidate_id, actor_id=user.id, human_approval=payload.human_approval
        )
    except (ValueError, LookupError) as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"candidate_id": candidate.candidate_id, "status": candidate.status}


@router.post("/{run_id}/candidates/{candidate_id}/publish")
def publish_candidate(
    run_id: str, candidate_id: str, payload: VersionIn,
    user: User = Depends(current_user), workspace_id: str = Depends(current_workspace_id),
    db: Session = Depends(get_db),
):
    scope = _scope_for_run(db, run_id=run_id, workspace_id=workspace_id, user_id=user.id)
    try:
        version = CandidateAssetPublisher(db).publish(
            scope, candidate_id, version=payload.version, actor_id=user.id
        )
    except (ValueError, LookupError) as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"candidate_id": candidate_id, "asset_id": version.asset_id,
            "version": version.version, "active": version.active}


@router.post("/{run_id}/candidates/{candidate_id}/rollback")
def rollback_candidate(
    run_id: str, candidate_id: str, payload: VersionIn,
    user: User = Depends(current_user), workspace_id: str = Depends(current_workspace_id),
    db: Session = Depends(get_db),
):
    scope = _scope_for_run(db, run_id=run_id, workspace_id=workspace_id, user_id=user.id)
    try:
        version = CandidateAssetPublisher(db).rollback(
            scope, candidate_id, version=payload.version, actor_id=user.id
        )
    except (ValueError, LookupError) as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"candidate_id": candidate_id, "asset_id": version.asset_id,
            "version": version.version, "active": version.active}
