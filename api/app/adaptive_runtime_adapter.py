"""Production adapters from adaptive PER graphs to durable Multi-Agent runs."""

from __future__ import annotations

import json
from typing import Any, Callable
import uuid

from sqlalchemy.orm import Session, sessionmaker

from .adaptive_runtime import (
    PlanEnvelope,
    PlanExecutionBatch,
    ReviewRequestEnvelope,
    ReviewResultEnvelope,
    RevisionRequestEnvelope,
    WorkerAuthorizationRequest,
    WorkerResultEnvelope,
)
from .multi_agent_repository import MultiAgentRepository, OrchestrationScope
from .multi_agent_runtime import WorkerSpec
from .multi_agent_runtime_adapter import (
    DEFAULT_TOOL_REGISTRY,
    ModelComplete,
    ServerToolRegistry,
    SubagentRuntimeExecutor,
)
from .multi_agent_service import DurableOrchestrator, TaskPlan
from .runtime_contract import RuntimeErrorCategory, RuntimeFailure
from .runtime_graph import AtlasAgentState


def _child_scope(parent_run_id: str, label: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"atlas:{parent_run_id}:{label}"))


def _tool_grants(state: AtlasAgentState) -> set[str]:
    grants: set[str] = set()
    for resource in state.requested_resources:
        value = resource.removeprefix("tool:").strip()
        if value:
            grants.add(value)
    return grants


def _result_for_parent(
    item: dict[str, Any],
    *,
    state: AtlasAgentState,
    logical_task_id: str,
    logical_worker_id: str,
) -> WorkerResultEnvelope:
    return WorkerResultEnvelope.model_validate({
        "schema_version": "v1",
        "envelope_id": str(item.get("envelope_id") or uuid.uuid4()),
        "run_id": state.identity.run_id,
        "task_id": logical_task_id,
        "worker_id": logical_worker_id,
        "workspace_id": state.identity.workspace_id,
        "user_id": state.identity.user_id,
        "agent_id": state.identity.agent_id,
        "status": str(item.get("status") or "failed"),
        "result": dict(item.get("result") or {}),
        "artifact_refs": tuple(item.get("artifact_refs") or ()),
        "error": str(item.get("error") or ""),
        "error_category": str(item.get("error_category") or ""),
        "runtime_run_id": str(item.get("runtime_run_id") or ""),
    })


class DurableAdaptivePlanExecutor:
    """Run one plan/revision batch through the existing durable DAG executor."""

    def __init__(
        self,
        session_factory: sessionmaker,
        *,
        model_complete: ModelComplete,
        tool_registry: ServerToolRegistry = DEFAULT_TOOL_REGISTRY,
    ) -> None:
        self.session_factory = session_factory
        self.model_complete = model_complete
        self.tool_registry = tool_registry

    async def __call__(
        self,
        state: AtlasAgentState,
        plan: PlanEnvelope,
        pending_task_ids: tuple[str, ...],
        revision: RevisionRequestEnvelope | None,
    ) -> PlanExecutionBatch:
        revision_round = revision.revision_round if revision else 0
        label = f"execute:p{plan.version}:r{revision_round}"
        scope = OrchestrationScope(
            workspace_id=state.identity.workspace_id,
            user_id=state.identity.user_id,
            agent_id=state.identity.agent_id,
            run_id=_child_scope(state.identity.run_id, label),
        )
        pending = set(pending_task_ids)
        logical_tasks = [task for task in plan.tasks if task.task_id in pending]
        if len(logical_tasks) != len(pending):
            raise ValueError("execution batch references an unknown plan task")
        referenced_workers = {task.worker_id for task in logical_tasks}
        logical_workers = [worker for worker in plan.workers if worker.worker_id in referenced_workers]
        # Physical IDs are derived from the complete immutable plan so a
        # paused subset resumes the same durable child tasks.
        worker_ids = {worker.worker_id: f"worker-{index + 1}" for index, worker in enumerate(plan.workers)}
        task_ids = {task.task_id: f"task-{index + 1}" for index, task in enumerate(plan.tasks)}
        workers = [
            WorkerSpec(
                worker_id=worker_ids[worker.worker_id],
                version=f"plan-{plan.version}",
                role=worker.role,
                objective=worker.objective,
                input_schema={
                    "type": "object",
                    "required": [
                        "parent_run_id", "plan_id", "plan_version",
                        "logical_task_id", "objective", "input",
                    ],
                    "properties": {
                        "parent_run_id": {"type": "string"},
                        "plan_id": {"type": "string"},
                        "plan_version": {"type": "integer"},
                        "logical_task_id": {"type": "string"},
                        "objective": {"type": "string"},
                        "input": worker.input_schema,
                        "tool_call": {"type": "object"},
                        "revision": {"type": "object"},
                    },
                    "additionalProperties": False,
                },
                output_schema=worker.output_schema,
                allowed_tools=frozenset(worker.allowed_tools),
                max_steps=worker.max_steps,
                timeout_seconds=worker.timeout_seconds,
                lifecycle=worker.lifecycle,
            )
            for worker in logical_workers
        ]
        tasks = [
            TaskPlan(
                task_id=task_ids[task.task_id],
                worker_id=worker_ids[task.worker_id],
                payload={
                    "parent_run_id": state.identity.run_id,
                    "plan_id": plan.plan_id,
                    "plan_version": plan.version,
                    "logical_task_id": task.task_id,
                    "objective": task.objective,
                    "input": {
                        key: value for key, value in task.payload.items()
                        if key != "tool_call"
                    },
                    **({"tool_call": task.payload["tool_call"]}
                       if isinstance(task.payload.get("tool_call"), dict) else {}),
                    **({"revision": revision.model_dump(mode="json")} if revision else {}),
                },
                depends_on=frozenset(
                    task_ids[item] for item in task.depends_on if item in task_ids
                ),
                artifact_refs=task.artifact_refs,
                max_attempts=task.max_attempts,
                timeout_seconds=task.timeout_seconds,
            )
            for task in logical_tasks
        ]
        grants = _tool_grants(state)
        approved = dict(state.complex_context.get("worker_authorizations") or {})
        physical_authorizations: dict[str, dict[str, str]] = {}
        resume_task_ids: set[str] = set()
        for logical_task_id, authorization in approved.items():
            if logical_task_id not in pending or not isinstance(authorization, dict):
                continue
            physical_task_id = task_ids[logical_task_id]
            if authorization.get("orchestration_run_id") != scope.run_id:
                raise PermissionError("worker authorization targets another orchestration run")
            if authorization.get("physical_task_id") != physical_task_id:
                raise PermissionError("worker authorization targets another physical task")
            physical_authorizations[physical_task_id] = {
                "ticket_id": str(authorization.get("ticket_id") or ""),
                "nonce": str(authorization.get("nonce") or ""),
                "resource_version": str(authorization.get("resource_version") or ""),
            }
            resume_task_ids.add(physical_task_id)
        authorization_requests: list[WorkerAuthorizationRequest] = []
        with self.session_factory() as db:
            orchestrator = DurableOrchestrator(db)
            repository = MultiAgentRepository(db)
            repository.validate_artifact_refs(
                scope,
                tuple(ref for task in logical_tasks for ref in task.artifact_refs),
            )
            try:
                repository.get_run(scope)
            except LookupError:
                orchestrator.create(
                    scope,
                    workers=workers,
                    tasks=tasks,
                    concurrency=min(3, max(1, len(workers))),
                    max_replans=0,
                    parent_agent_version_id=state.identity.agent_version_id,
                    user_tool_grants=grants,
                    orchestrator_tool_grants=grants,
                )
            child_executor = SubagentRuntimeExecutor(
                db,
                scope,
                model_complete=self.model_complete,
                session_factory=self.session_factory,
                tool_registry=self.tool_registry,
                authorizations=physical_authorizations,
            )
            aggregate = await orchestrator.execute(
                scope,
                child_executor,
                resume_task_ids=resume_task_ids,
            )
            physical_results = {
                str(item.get("task_id")): item for item in aggregate.get("results", [])
            }
            if aggregate.get("status") == "paused":
                for task in logical_tasks:
                    physical_task_id = task_ids[task.task_id]
                    item = physical_results.get(physical_task_id) or {}
                    if item.get("status") != "paused" or physical_task_id in physical_authorizations:
                        continue
                    raw_call = task.payload.get("tool_call")
                    if not isinstance(raw_call, dict):
                        raise RuntimeFailure(
                            RuntimeErrorCategory.PERMISSION,
                            "paused task has no declared tool authorization target",
                        )
                    tool_name = str(raw_call.get("name") or "")
                    parameters = raw_call.get("arguments") or {}
                    trusted = self.tool_registry.get(tool_name)
                    if trusted is None or trusted.access != "write" or not isinstance(parameters, dict):
                        raise RuntimeFailure(
                            RuntimeErrorCategory.PERMISSION,
                            "paused task authorization target is not a trusted write tool",
                        )
                    resource_version = f"plan-{plan.version}-revision-{revision_round}"
                    ticket = repository.issue_authorization(
                        scope,
                        worker_id=worker_ids[task.worker_id],
                        tool_name=tool_name,
                        parameters=parameters,
                        ticket_scope=set(trusted.required_scope),
                        resource_version=resource_version,
                    )
                    authorization_requests.append(WorkerAuthorizationRequest(
                        logical_task_id=task.task_id,
                        orchestration_run_id=scope.run_id,
                        physical_task_id=physical_task_id,
                        worker_id=worker_ids[task.worker_id],
                        tool_name=tool_name,
                        parameters=parameters,
                        ticket_id=ticket.ticket_id,
                        nonce=ticket.nonce,
                        parameter_digest=ticket.parameter_digest,
                        resource_version=resource_version,
                        scope=tuple(sorted(ticket.scope)),
                        expires_at=ticket.expires_at.isoformat(),
                    ))
        results = []
        artifacts: set[str] = set()
        for task in logical_tasks:
            item = physical_results.get(task_ids[task.task_id])
            if item is None:
                raise RuntimeFailure(RuntimeErrorCategory.DEPENDENCY, "durable orchestrator omitted a task result")
            parent_result = _result_for_parent(
                item,
                state=state,
                logical_task_id=task.task_id,
                logical_worker_id=task.worker_id,
            )
            results.append(parent_result)
            artifacts.update(parent_result.artifact_refs)
        declared_write = any(
            task.payload.get("tool_call") and self._is_write_tool(str(task.payload["tool_call"].get("name") or ""))
            for task in logical_tasks
        )
        return PlanExecutionBatch(
            results=tuple(results),
            artifact_refs=tuple(sorted(artifacts)),
            side_effects_started=declared_write,
            authorization_requests=tuple(authorization_requests),
        )

    def requires_side_effect_boundary(
        self,
        plan: PlanEnvelope,
        pending_task_ids: tuple[str, ...],
    ) -> bool:
        pending = set(pending_task_ids)
        return any(
            task.task_id in pending
            and isinstance(task.payload.get("tool_call"), dict)
            and self._is_write_tool(str(task.payload["tool_call"].get("name") or ""))
            for task in plan.tasks
        )

    def _is_write_tool(self, name: str) -> bool:
        tool = self.tool_registry.get(name)
        return tool is not None and tool.access == "write"


class DurableIndependentReviewer:
    """Create and run one read-only reviewer in its own orchestration scope."""

    def __init__(
        self,
        session_factory: sessionmaker,
        *,
        model_complete: ModelComplete,
    ) -> None:
        self.session_factory = session_factory
        self.model_complete = model_complete

    async def __call__(self, request: ReviewRequestEnvelope) -> ReviewResultEnvelope:
        label = f"review:p{request.plan.version}:r{request.review_round}"
        scope = OrchestrationScope(
            workspace_id=request.workspace_id,
            user_id=request.user_id,
            agent_id=request.agent_id,
            run_id=_child_scope(request.run_id, label),
        )
        reviewer_spec = WorkerSpec(
            worker_id="reviewer",
            version=f"review-{request.review_round}",
            role="independent-reviewer",
            objective=(
                "Independently verify every acceptance criterion and reviewed task. "
                "Return only a JSON review verdict. You cannot dispatch workers, mutate results, "
                "approve tools, or perform writes."
            ),
            input_schema={"type": "object"},
            output_schema={
                "type": "object",
                "required": ["verdict", "criteria", "reviewed_task_ids", "confidence"],
                "properties": {
                    "verdict": {"type": "string"},
                    "criteria": {"type": "array"},
                    "reviewed_task_ids": {"type": "array"},
                    "required_actions": {"type": "array"},
                    "revise_task_ids": {"type": "array"},
                    "evidence_refs": {"type": "array"},
                    "confidence": {"type": "number"},
                },
                "additionalProperties": False,
            },
            allowed_tools=frozenset(),
            max_steps=4,
            timeout_seconds=120,
            lifecycle="ephemeral",
        )
        task = TaskPlan(
            task_id="review-task",
            worker_id="reviewer",
            payload={"review_request": request.model_dump(mode="json")},
            artifact_refs=request.artifact_refs,
            max_attempts=2,
            timeout_seconds=120,
        )
        with self.session_factory() as db:
            orchestrator = DurableOrchestrator(db)
            repository = MultiAgentRepository(db)
            repository.validate_artifact_refs(scope, request.artifact_refs)
            try:
                repository.get_run(scope)
            except LookupError:
                orchestrator.create(
                    scope,
                    workers=(reviewer_spec,),
                    tasks=(task,),
                    concurrency=1,
                    max_replans=0,
                    parent_agent_version_id=request.plan.agent_id and self._parent_version(request, db),
                    user_tool_grants=set(),
                    orchestrator_tool_grants=set(),
                )
            executor = SubagentRuntimeExecutor(
                db,
                scope,
                model_complete=self.model_complete,
                session_factory=self.session_factory,
                tool_registry=ServerToolRegistry({}),
            )
            aggregate = await orchestrator.execute(scope, executor)
        items = [item for item in aggregate.get("results", []) if item.get("task_id") == "review-task"]
        if len(items) != 1 or items[0].get("status") != "succeeded":
            error = str(items[0].get("error") if items else "reviewer result missing")
            raise RuntimeFailure(RuntimeErrorCategory.VALIDATION, error)
        model_payload = dict(items[0].get("result") or {})
        controlled = {
            **model_payload,
            "schema_version": "v1",
            "request_envelope_id": request.envelope_id,
            "run_id": request.run_id,
            "workspace_id": request.workspace_id,
            "user_id": request.user_id,
            "agent_id": request.agent_id,
            "reviewer_id": request.reviewer_id,
        }
        return ReviewResultEnvelope.model_validate_json(
            json.dumps(controlled, ensure_ascii=False)
        )

    @staticmethod
    def _parent_version(request: ReviewRequestEnvelope, db: Session) -> str:
        from .models import RuntimeRun

        row = db.get(RuntimeRun, request.run_id)
        if row is None or row.workspace_id != request.workspace_id or row.user_id != request.user_id:
            raise PermissionError("review parent runtime is unavailable in scope")
        return row.version_id
