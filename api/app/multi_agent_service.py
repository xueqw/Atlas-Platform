"""Durable orchestration and governed candidate publication services."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import json
from typing import Any, Awaitable, Callable, Iterable
import uuid

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from .governance_models import (
    CandidateAssetVersionRecord,
    EpisodicEvidence,
    LedgerEvent,
    ProceduralCandidate,
    ResultEnvelopeRecord,
    governance_now,
)
from .models import Agent, AgentVersion, Skill
from .multi_agent_repository import MultiAgentRepository, OrchestrationScope
from .multi_agent_runtime import (
    MAX_CONCURRENT_WORKERS,
    MAX_REPLANS,
    DagScheduler,
    DagTask,
    OrchestratorTeam,
    ResultEnvelope,
    TaskEnvelope,
    WorkerSpec,
    aggregate_results,
)
from .runtime_contract import RuntimeErrorCategory, classify_error


WorkerExecutor = Callable[[TaskEnvelope, WorkerSpec], Awaitable[ResultEnvelope]]
ReplanHandler = Callable[[OrchestrationScope, list[ResultEnvelope]], Awaitable[list["TaskPlan"]]]
ReplayExecutor = Callable[[dict[str, Any], tuple[dict[str, Any], ...]], dict[str, Any]]
CandidateReplayRunner = Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]
CancellationProbe = Callable[[], Awaitable[bool] | bool]


@dataclass(frozen=True)
class TaskPlan:
    task_id: str
    worker_id: str
    payload: dict[str, Any]
    depends_on: frozenset[str] = frozenset()
    artifact_refs: tuple[str, ...] = ()
    max_attempts: int = 2
    timeout_seconds: int = 120

    def validate(self) -> None:
        if not self.task_id or not self.worker_id or not isinstance(self.payload, dict):
            raise ValueError("task plan requires task_id, worker_id, and object payload")
        if self.max_attempts < 1 or self.timeout_seconds < 1:
            raise ValueError("task retry and timeout limits must be positive")


def _scheduler_state(scheduler: DagScheduler, *, conflict_keys: set[str]) -> dict[str, Any]:
    return {
        **dict(getattr(scheduler, "metadata", {})),
        "schema_version": "atlas.orchestration-state.v1",
        "concurrency": scheduler.concurrency,
        "max_replans": scheduler.max_replans,
        "replans": scheduler.replans,
        "cancelled": scheduler.cancelled,
        "conflict_keys": sorted(conflict_keys),
        "tasks": {
            task_id: {
                "worker_id": task.worker_id,
                "depends_on": sorted(task.depends_on),
                "status": task.status,
                "attempts": task.attempts,
                "max_attempts": task.max_attempts,
                "timeout_seconds": task.timeout_seconds,
                "started_at": task.started_at.isoformat() if task.started_at else None,
            }
            for task_id, task in scheduler.tasks.items()
        },
    }


def _scheduler_from_state(state: dict[str, Any]) -> DagScheduler:
    tasks = [
        DagTask(
            task_id=task_id,
            worker_id=item["worker_id"],
            depends_on=set(item.get("depends_on", [])),
            status=item.get("status", "pending"),
            attempts=int(item.get("attempts", 0)),
            max_attempts=int(item.get("max_attempts", 2)),
            timeout_seconds=int(item.get("timeout_seconds", 120)),
        )
        for task_id, item in state.get("tasks", {}).items()
    ]
    scheduler = DagScheduler(
        tasks,
        concurrency=int(state.get("concurrency", MAX_CONCURRENT_WORKERS)),
        max_replans=int(state.get("max_replans", MAX_REPLANS)),
    )
    scheduler.replans = int(state.get("replans", 0))
    scheduler.cancelled = bool(state.get("cancelled", False))
    scheduler.metadata = {
        key: state[key] for key in (
            "parent_agent_version_id", "user_tool_grants", "orchestrator_tool_grants"
        ) if key in state
    }
    return scheduler


def _worker_from_record(payload: dict[str, Any]) -> WorkerSpec:
    return WorkerSpec(
        worker_id=payload["worker_id"], version=payload["version"],
        role=payload["role"], objective=payload["objective"],
        input_schema=payload.get("input_schema", {}),
        output_schema=payload.get("output_schema", {}),
        allowed_tools=frozenset(payload.get("allowed_tools", [])),
        max_steps=int(payload.get("max_steps", 8)),
        timeout_seconds=int(payload.get("timeout_seconds", 120)),
        lifecycle=payload.get("lifecycle", "ephemeral"),
        schema_version=payload.get("schema_version", "v1"),
    )


def _task_from_record(payload: dict[str, Any]) -> TaskEnvelope:
    return TaskEnvelope(
        envelope_id=payload["envelope_id"], run_id=payload["run_id"],
        task_id=payload["task_id"], worker_id=payload["worker_id"],
        workspace_id=payload["workspace_id"], user_id=payload["user_id"],
        agent_id=payload["agent_id"], payload=payload["payload"],
        artifact_refs=tuple(payload.get("artifact_refs", [])),
        schema_version=payload.get("schema_version", "v1"),
    )


def _result_from_record(payload: dict[str, Any]) -> ResultEnvelope:
    return ResultEnvelope(
        envelope_id=payload["envelope_id"], task_id=payload["task_id"],
        worker_id=payload["worker_id"], status=payload["status"],
        result=payload.get("result", {}), artifact_refs=tuple(payload.get("artifact_refs", [])),
        error=payload.get("error", ""), schema_version=payload.get("schema_version", "v1"),
        error_category=payload.get("error_category", ""),
        runtime_run_id=payload.get("runtime_run_id", ""),
        run_id=payload["run_id"], workspace_id=payload["workspace_id"],
        user_id=payload["user_id"], agent_id=payload["agent_id"],
    )


class DurableOrchestrator:
    """Dynamic worker factory and restart-safe bounded DAG executor."""

    def __init__(self, db: Session):
        self.db = db
        self.repository = MultiAgentRepository(db)

    def create(
        self,
        scope: OrchestrationScope,
        *,
        workers: Iterable[WorkerSpec],
        tasks: Iterable[TaskPlan],
        concurrency: int = MAX_CONCURRENT_WORKERS,
        max_replans: int = MAX_REPLANS,
        conflict_keys: set[str] | None = None,
        parent_agent_version_id: str = "",
        user_tool_grants: set[str] | None = None,
        orchestrator_tool_grants: set[str] | None = None,
    ) -> dict[str, Any]:
        worker_list = list(workers)
        task_list = list(tasks)
        team = OrchestratorTeam(
            scope.run_id, scope.workspace_id, scope.user_id, scope.agent_id
        )
        for spec in worker_list:
            team.create_worker(spec)
        scheduler_tasks: list[DagTask] = []
        envelopes: list[TaskEnvelope] = []
        for plan in task_list:
            plan.validate()
            envelope = team.make_task(
                task_id=plan.task_id, worker_id=plan.worker_id, payload=plan.payload,
                artifact_refs=plan.artifact_refs,
            )
            envelopes.append(envelope)
            scheduler_tasks.append(DagTask(
                plan.task_id, plan.worker_id, set(plan.depends_on),
                max_attempts=plan.max_attempts, timeout_seconds=plan.timeout_seconds,
            ))
        scheduler = DagScheduler(
            scheduler_tasks, max_workers=team.max_workers,
            concurrency=concurrency, max_replans=max_replans,
        )
        state = _scheduler_state(scheduler, conflict_keys=conflict_keys or set())
        state["parent_agent_version_id"] = parent_agent_version_id
        state["user_tool_grants"] = sorted(user_tool_grants or set())
        state["orchestrator_tool_grants"] = sorted(orchestrator_tool_grants or set())
        scheduler.metadata = {
            key: state[key] for key in (
                "parent_agent_version_id", "user_tool_grants", "orchestrator_tool_grants"
            )
        }
        self.repository.create_run(scope, scheduler_state=state)
        for spec in team.workers.values():
            self.repository.save_worker(scope, spec)
        for envelope in envelopes:
            self.repository.save_task(scope, envelope)
        return state

    def restore(self, scope: OrchestrationScope) -> tuple[OrchestratorTeam, DagScheduler, set[str]]:
        run = self.repository.get_run(scope)
        state = dict(run.scheduler_state or {})
        scheduler = _scheduler_from_state(state)
        recovered: list[str] = []
        # A process may die after dispatch and before recording a result. Stable
        # task envelope IDs let the worker handle the replay idempotently.
        for task in scheduler.tasks.values():
            if task.status == "running":
                if task.attempts < task.max_attempts:
                    task.status = "pending"
                else:
                    task.status = "failed"
                    scheduler._cancel_dependents(task.task_id)
                recovered.append(task.task_id)
        team = OrchestratorTeam(
            scope.run_id, scope.workspace_id, scope.user_id, scope.agent_id
        )
        for record in self.repository.list_workers(scope):
            team.create_worker(_worker_from_record(record.spec))
        for record in self.repository.list_tasks(scope):
            envelope = _task_from_record(record.envelope)
            team.issued_tasks[envelope.task_id] = envelope
        if recovered:
            self.repository.append_audit(
                scope, action="orchestration.recovered", decision="resumed",
                details={"recovered_task_ids": recovered}, commit=False,
            )
            self.repository.save_run(
                scope, status="running",
                scheduler_state=_scheduler_state(
                    scheduler, conflict_keys=set(state.get("conflict_keys", []))
                ),
            )
        return team, scheduler, set(state.get("conflict_keys", []))

    def cancel(self, scope: OrchestrationScope, *, actor_id: str) -> dict[str, Any]:
        _, scheduler, conflict_keys = self.restore(scope)
        scheduler.cancel()
        state = _scheduler_state(scheduler, conflict_keys=conflict_keys)
        self.repository.append_audit(
            scope, action="orchestration.cancelled", decision="cancelled",
            details={"actor_id": actor_id}, commit=False,
        )
        self.repository.save_run(scope, status="cancelled", scheduler_state=state, ended=True)
        return state

    async def execute(
        self,
        scope: OrchestrationScope,
        executor: WorkerExecutor,
        *,
        replan: ReplanHandler | None = None,
        resume_task_ids: set[str] | None = None,
        cancellation_probe: CancellationProbe | None = None,
    ) -> dict[str, Any]:
        team, scheduler, conflict_keys = self.restore(scope)
        for task_id in resume_task_ids or set():
            if task_id in scheduler.tasks and scheduler.tasks[task_id].status == "paused":
                scheduler.resume_task(task_id)
        run = self.repository.get_run(scope)
        if run.status == "cancelled" or scheduler.cancelled:
            return dict(run.aggregate or {"status": "cancelled", "results": [], "conflicts": []})
        self.repository.save_run(
            scope, status="running",
            scheduler_state=_scheduler_state(scheduler, conflict_keys=conflict_keys),
        )
        latest_results = {
            item.task_id: _result_from_record(item.envelope)
            for item in self.repository.list_results(scope)
        }
        while not scheduler.terminal():
            if scheduler.paused():
                break
            dispatched = scheduler.dispatch()
            if not dispatched:
                # A valid acyclic graph can only stall after an externally persisted
                # inconsistency; fail closed instead of spinning forever.
                scheduler.cancel()
                self.repository.append_audit(
                    scope, action="orchestration.stalled", decision="cancelled",
                    details={"reason": "no_dispatchable_tasks"}, commit=False,
                )
                break
            self.repository.save_run(
                scope, status="running",
                scheduler_state=_scheduler_state(scheduler, conflict_keys=conflict_keys),
            )

            async def run_one(task: DagTask) -> tuple[DagTask, ResultEnvelope]:
                envelope = team.issued_tasks[task.task_id]
                spec = team.workers[task.worker_id]
                try:
                    result = await asyncio.wait_for(
                        executor(envelope, spec), timeout=task.timeout_seconds
                    )
                    result = team.accept_result(result)
                except asyncio.TimeoutError:
                    result = ResultEnvelope(
                        envelope_id=str(uuid.uuid4()), task_id=task.task_id,
                        worker_id=task.worker_id, status="timed_out", result={},
                        error="worker timeout", run_id=scope.run_id,
                        workspace_id=scope.workspace_id, user_id=scope.user_id,
                        agent_id=scope.agent_id,
                        error_category=RuntimeErrorCategory.TRANSIENT.value,
                    )
                except asyncio.CancelledError:
                    result = ResultEnvelope(
                        envelope_id=str(uuid.uuid4()), task_id=task.task_id,
                        worker_id=task.worker_id, status="cancelled", result={},
                        error="worker cancelled", run_id=scope.run_id,
                        workspace_id=scope.workspace_id, user_id=scope.user_id,
                        agent_id=scope.agent_id,
                        error_category=RuntimeErrorCategory.TERMINAL.value,
                    )
                except Exception as exc:
                    result = ResultEnvelope(
                        envelope_id=str(uuid.uuid4()), task_id=task.task_id,
                        worker_id=task.worker_id, status="failed", result={},
                        error=str(exc), run_id=scope.run_id,
                        workspace_id=scope.workspace_id, user_id=scope.user_id,
                        agent_id=scope.agent_id,
                        error_category=classify_error(exc).value,
                    )
                return task, result

            batch_task = asyncio.gather(*(run_one(task) for task in dispatched))
            cancelled_externally = False
            while not batch_task.done():
                done, _ = await asyncio.wait({batch_task}, timeout=0.2)
                if done or cancellation_probe is None:
                    if cancellation_probe is None:
                        break
                    continue
                observed = cancellation_probe()
                should_cancel = await observed if hasattr(observed, "__await__") else observed
                if should_cancel:
                    cancelled_externally = True
                    batch_task.cancel()
                    break
            try:
                batch = await batch_task
            except asyncio.CancelledError:
                batch = []
            if cancelled_externally:
                scheduler.cancel()
                for task in dispatched:
                    cancelled = ResultEnvelope(
                        envelope_id=str(uuid.uuid4()), task_id=task.task_id,
                        worker_id=task.worker_id, status="cancelled", result={},
                        error="orchestration cancelled by persistent control plane",
                        run_id=scope.run_id, workspace_id=scope.workspace_id,
                        user_id=scope.user_id, agent_id=scope.agent_id,
                        error_category=RuntimeErrorCategory.TERMINAL.value,
                    )
                    self.repository.save_result(scope, cancelled)
                    latest_results[task.task_id] = cancelled
                self.repository.append_audit(
                    scope, action="orchestration.cancel_observed", decision="cancelled",
                    details={"task_ids": [task.task_id for task in dispatched]}, commit=False,
                )
                break
            hard_failures: list[ResultEnvelope] = []
            for task, result in batch:
                self.repository.save_result(scope, result)
                latest_results[task.task_id] = result
                if result.status == "succeeded":
                    scheduler.complete(task.task_id, success=True)
                elif result.status == "paused":
                    task.status = "paused"
                elif result.status == "cancelled":
                    # A cancellation-aware worker can observe a durable cancel
                    # before this process' coarser batch probe does. Preserve
                    # that terminal control-plane decision for the entire DAG
                    # instead of degrading it into a generic partial failure.
                    scheduler.cancel()
                else:
                    category = result.error_category or (
                        RuntimeErrorCategory.TRANSIENT.value
                        if result.status == "timed_out" or result.error == "transient"
                        else RuntimeErrorCategory.TERMINAL.value
                    )
                    retryable = category == RuntimeErrorCategory.TRANSIENT.value
                    status = scheduler.complete(
                        task.task_id, success=False, retryable=retryable,
                    )
                    if status == "failed":
                        hard_failures.append(result)

            if hard_failures and replan is not None and scheduler.request_replan():
                additions = await replan(scope, hard_failures)
                self._add_replanned_tasks(scope, team, scheduler, additions)
                self.repository.append_audit(
                    scope, action="orchestration.replanned", decision="allowed",
                    details={"replan_count": scheduler.replans,
                             "task_ids": [item.task_id for item in additions]}, commit=False,
                )
            self.repository.save_run(
                scope, status="running",
                scheduler_state=_scheduler_state(scheduler, conflict_keys=conflict_keys),
            )

        terminal_results = [
            latest_results[task_id] for task_id in sorted(latest_results)
            if task_id in scheduler.tasks
        ]
        aggregate = aggregate_results(terminal_results, conflict_keys=conflict_keys)
        if scheduler.paused():
            aggregate["status"] = "paused"
        if scheduler.cancelled:
            aggregate["status"] = "cancelled"
        status = "succeeded" if aggregate["status"] == "succeeded" else (
            "paused" if scheduler.paused() else ("cancelled" if scheduler.cancelled else "failed")
        )
        if status == "succeeded":
            from .memory_experience import consolidate_successful_trajectory
            from .memory_ledger import MemoryScope
            for result in terminal_results:
                if result.status != "succeeded":
                    continue
                spec = team.workers[result.worker_id]
                # Persist only a redacted structural trace. Worker result bodies
                # can contain PII and never enter experience memory implicitly.
                consolidate_successful_trajectory(
                    self.db,
                    scope=MemoryScope(
                        scope.workspace_id, scope.user_id, scope.agent_id,
                        run_id=scope.run_id, worker_id=result.worker_id,
                    ),
                    task_type=spec.role,
                    trajectory={
                        "task_id": result.task_id,
                        "worker_id": result.worker_id,
                        "runtime_run_id": result.runtime_run_id,
                        "status": result.status,
                    },
                    metrics={"successful": True},
                    idempotency_key=f"orchestration-episode:{result.envelope_id}",
                    redaction_status="redacted",
                )
        self.repository.append_audit(
            scope, action="orchestration.completed", decision=status,
            details={"aggregate_status": aggregate["status"]}, commit=False,
        )
        self.repository.save_run(
            scope, status=status,
            scheduler_state=_scheduler_state(scheduler, conflict_keys=conflict_keys),
            aggregate=aggregate, ended=not scheduler.paused(),
        )
        return aggregate

    def _add_replanned_tasks(
        self, scope: OrchestrationScope, team: OrchestratorTeam,
        scheduler: DagScheduler, additions: list[TaskPlan],
    ) -> None:
        if not additions:
            return
        candidate_tasks = list(scheduler.tasks.values())
        new_envelopes: list[TaskEnvelope] = []
        for plan in additions:
            plan.validate()
            envelope = team.make_task(
                task_id=plan.task_id, worker_id=plan.worker_id,
                payload=plan.payload, artifact_refs=plan.artifact_refs,
            )
            new_envelopes.append(envelope)
            candidate_tasks.append(DagTask(
                plan.task_id, plan.worker_id, set(plan.depends_on),
                max_attempts=plan.max_attempts, timeout_seconds=plan.timeout_seconds,
            ))
        # Re-run all cap/dependency/cycle validation before mutating persisted state.
        checked = DagScheduler(
            candidate_tasks, concurrency=scheduler.concurrency,
            max_replans=scheduler.max_replans,
        )
        scheduler.tasks = checked.tasks
        for envelope in new_envelopes:
            self.repository.save_task(scope, envelope)


_SENSITIVE_KEYS = {"pii", "password", "secret", "token", "ssn", "credential"}


def _contains_sensitive_key(value: Any) -> bool:
    if isinstance(value, dict):
        return any(str(key).lower() in _SENSITIVE_KEYS or _contains_sensitive_key(item)
                   for key, item in value.items())
    if isinstance(value, list):
        return any(_contains_sensitive_key(item) for item in value)
    return False


def _matches_replay_schema(value: Any, schema: dict[str, Any]) -> bool:
    """Validate the JSON-schema subset used by persisted replay fixtures.

    Replay is a publication gate, so unknown or malformed schema constructs do
    not become an implicit pass.  The supported subset deliberately covers the
    object contracts emitted by workers and Skills without adding another
    runtime dependency to the API service.
    """
    if not isinstance(schema, dict) or not schema:
        return False
    if "const" in schema and value != schema["const"]:
        return False
    if "enum" in schema and value not in schema["enum"]:
        return False
    schema_type = schema.get("type")
    type_matches = {
        "object": lambda item: isinstance(item, dict),
        "array": lambda item: isinstance(item, list),
        "string": lambda item: isinstance(item, str),
        "number": lambda item: isinstance(item, (int, float)) and not isinstance(item, bool),
        "integer": lambda item: isinstance(item, int) and not isinstance(item, bool),
        "boolean": lambda item: isinstance(item, bool),
        "null": lambda item: item is None,
    }
    if schema_type not in type_matches or not type_matches[schema_type](value):
        return False
    if schema_type == "string" and len(value) < int(schema.get("minLength", 0)):
        return False
    if schema_type == "array":
        item_schema = schema.get("items")
        return item_schema is None or all(_matches_replay_schema(item, item_schema) for item in value)
    if schema_type != "object":
        return True
    required = schema.get("required", [])
    properties = schema.get("properties", {})
    if not isinstance(required, list) or not isinstance(properties, dict):
        return False
    if any(not isinstance(key, str) or key not in value for key in required):
        return False
    if schema.get("additionalProperties") is False and any(key not in properties for key in value):
        return False
    return all(
        key not in value or _matches_replay_schema(value[key], field_schema)
        for key, field_schema in properties.items()
        if isinstance(field_schema, dict)
    )


def _path_value(value: Any, path: str) -> tuple[bool, Any]:
    current = value
    normalized = path.removeprefix("$.").removeprefix("$")
    if not normalized:
        return True, current
    for part in normalized.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
            continue
        if isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
            continue
        return False, None
    return True, current


def _render_replay_template(value: Any, replay_input: dict[str, Any]) -> Any:
    if isinstance(value, dict):
        return {key: _render_replay_template(item, replay_input) for key, item in value.items()}
    if isinstance(value, list):
        return [_render_replay_template(item, replay_input) for item in value]
    if isinstance(value, str) and value.startswith("{{input.") and value.endswith("}}"):
        found, selected = _path_value(replay_input, value[8:-2])
        if not found:
            raise ValueError(f"replay input path not found: {value[8:-2]}")
        return selected
    return value


def deterministic_replay_runner(
    candidate: dict[str, Any], replay_input: dict[str, Any]
) -> dict[str, Any]:
    """Execute the safe deterministic program embedded in a redacted asset.

    Production deployments may inject an isolated model/tool runner instead.
    The built-in runner supports only data-only ``constant`` and ``template``
    programs, so offline evaluation cannot cause tool side effects.
    """
    program = candidate.get("replay_program")
    if not isinstance(program, dict):
        raise ValueError("candidate replay_program is required")
    operation = program.get("operation")
    output = program.get("output")
    if operation not in {"constant", "template"} or not isinstance(output, dict):
        raise ValueError("unsupported deterministic replay program")
    if operation == "constant":
        return json.loads(json.dumps(output, ensure_ascii=False))
    rendered = _render_replay_template(output, replay_input)
    if not isinstance(rendered, dict):
        raise ValueError("replay runner output must be an object")
    return rendered


def _replay_fixture(item: dict[str, Any]) -> dict[str, Any] | None:
    trajectory = item.get("trajectory")
    metrics = item.get("metrics")
    if not isinstance(trajectory, dict):
        return None
    fixture = trajectory.get("replay_fixture")
    if isinstance(fixture, dict):
        return fixture
    # Persisted ResultEnvelope evidence keeps worker output under ``result``.
    # A producing worker may attach the same versioned fixture contract without
    # changing the ResultEnvelope storage schema.
    result = trajectory.get("result")
    if isinstance(result, dict):
        fixture = result.get("_replay_fixture", result.get("replay_fixture"))
        if isinstance(fixture, dict):
            return fixture
    if isinstance(metrics, dict) and isinstance(metrics.get("replay_fixture"), dict):
        return metrics["replay_fixture"]
    # Backward-compatible field layout for evidence written before the nested
    # replay_fixture contract was introduced.
    if any(key in trajectory for key in ("expected_output", "assertions", "output_schema")):
        return {
            "input": trajectory.get("input", trajectory.get("payload", {})),
            "expected_output": trajectory.get("expected_output"),
            "assertions": trajectory.get("assertions", []),
            "output_schema": trajectory.get(
                "output_schema",
                metrics.get("output_schema") if isinstance(metrics, dict) else None,
            ),
        }
    return None


def _replay_assertions_pass(output: dict[str, Any], fixture: dict[str, Any]) -> tuple[bool, list[dict[str, Any]]]:
    checks: list[dict[str, Any]] = []
    expected_present = "expected_output" in fixture and fixture.get("expected_output") is not None
    if expected_present:
        checks.append({
            "kind": "expected_output",
            "passed": output == fixture["expected_output"],
        })
    assertions = fixture.get("assertions", [])
    if not isinstance(assertions, list):
        return False, [{"kind": "assertions", "passed": False, "reason": "invalid assertions"}]
    for assertion in assertions:
        if not isinstance(assertion, dict) or not isinstance(assertion.get("path"), str):
            checks.append({"kind": "assertion", "passed": False, "reason": "invalid assertion"})
            continue
        found, actual = _path_value(output, assertion["path"])
        operator = assertion.get("operator", "equals")
        expected = assertion.get("expected")
        if operator == "exists":
            passed = found is bool(expected if "expected" in assertion else True)
        elif operator == "equals":
            passed = found and actual == expected
        elif operator == "not_equals":
            passed = found and actual != expected
        elif operator == "contains":
            passed = found and isinstance(actual, (str, list, dict)) and expected in actual
        elif operator == "in":
            passed = found and isinstance(expected, list) and actual in expected
        else:
            passed = False
        checks.append({
            "kind": "assertion", "path": assertion["path"],
            "operator": operator, "passed": passed,
        })
    if not expected_present and not assertions:
        checks.append({"kind": "expected_result", "passed": False, "reason": "missing expectation"})
    return bool(checks) and all(check["passed"] for check in checks), checks


def deterministic_candidate_replay(
    content: dict[str, Any], evidence: tuple[dict[str, Any], ...], *,
    runner: CandidateReplayRunner = deterministic_replay_runner,
) -> dict[str, Any]:
    """Run the built-in, server-owned offline replay gate.

    This gate deliberately avoids trusting client-supplied scores.  It replays
    every persisted evidence fixture against the redacted candidate contract,
    executes a side-effect-free runner, then verifies its output schema and
    expected assertions. Deployments may inject an isolated richer runner, but
    the HTTP workflow has a deterministic fail-closed evaluator out of the box.
    """
    instruction = content.get("content") or content.get("system_prompt")
    candidate_safe = isinstance(instruction, str) and bool(instruction.strip())
    candidate_safe = candidate_safe and not _contains_sensitive_key(content)
    details: list[dict[str, Any]] = []
    passed = 0
    for item in evidence:
        fixture = _replay_fixture(item)
        fixture_safe = isinstance(fixture, dict) and not _contains_sensitive_key(fixture)
        replay_input = fixture.get("input") if fixture_safe else None
        input_schema = fixture.get("input_schema") if fixture_safe else None
        schema = fixture.get("output_schema") if fixture_safe else None
        input_schema_ok = False
        execution_ok = False
        schema_ok = False
        assertions_ok = False
        assertion_details: list[dict[str, Any]] = []
        error: str | None = None
        if candidate_safe and fixture_safe and isinstance(replay_input, dict):
            try:
                input_schema_ok = _matches_replay_schema(replay_input, input_schema)
                if input_schema_ok:
                    output = runner(content, replay_input)
                    execution_ok = isinstance(output, dict) and not _contains_sensitive_key(output)
                    schema_ok = execution_ok and _matches_replay_schema(output, schema)
                    assertions_ok, assertion_details = (
                        _replay_assertions_pass(output, fixture) if execution_ok else (False, [])
                    )
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
        success = bool(candidate_safe and fixture_safe and input_schema_ok
                       and execution_ok and schema_ok and assertions_ok)
        passed += int(success)
        details.append({
            "evidence_id": item.get("evidence_id"),
            "passed": success,
            "checks": {
                "candidate_instructions": candidate_safe,
                "persisted_replay_fixture": fixture_safe,
                "input_schema": input_schema_ok,
                "runner_execution": execution_ok,
                "output_schema": schema_ok,
                "expected_assertions": assertions_ok,
            },
            "assertions": assertion_details,
            **({"error": error} if error else {}),
        })
    return {"passed": passed, "total": len(evidence), "details": details,
            "runner": "atlas.deterministic-candidate-replay.v2"}


class CandidateAssetPublisher:
    """Governed bridge from trajectory candidate to real versioned Atlas assets."""

    def __init__(self, db: Session, *, replay_executor: ReplayExecutor | None = None):
        self.db = db
        self.replay_executor = replay_executor

    def _audit(
        self, scope: OrchestrationScope, *, action: str, decision: str,
        details: dict[str, Any], actor_id: str,
    ) -> None:
        MultiAgentRepository(self.db).append_audit(
            scope, action=action, decision=decision,
            details={**details, "actor_id": actor_id}, commit=False,
        )

    def _candidate(self, scope: OrchestrationScope, candidate_id: str) -> ProceduralCandidate:
        candidate = self.db.scalar(select(ProceduralCandidate).where(
            ProceduralCandidate.candidate_id == candidate_id,
            ProceduralCandidate.workspace_id == scope.workspace_id,
            ProceduralCandidate.user_id == scope.user_id,
            ProceduralCandidate.agent_id == scope.agent_id,
            ProceduralCandidate.run_id == scope.run_id,
        ))
        if candidate is None:
            raise LookupError("candidate not found in orchestration scope")
        return candidate

    def _verified_evidence(
        self, scope: OrchestrationScope, evidence_ids: Iterable[str], *,
        allow_agent_history: bool = False,
    ) -> tuple[dict[str, Any], ...]:
        """Resolve successful, redacted evidence inside the exact run boundary.

        Evidence identifiers may point to a durable episodic trajectory or a
        persisted ResultEnvelope. A ResultEnvelope is accepted only when its
        result explicitly carries the server-side ``_redaction_status`` marker.
        """
        requested = list(dict.fromkeys(evidence_ids))
        if not requested:
            raise ValueError("trajectory evidence is required")
        resolved: dict[str, dict[str, Any]] = {}
        episode_filters = [
            EpisodicEvidence.evidence_id.in_(requested),
            EpisodicEvidence.workspace_id == scope.workspace_id,
            EpisodicEvidence.user_id == scope.user_id,
            EpisodicEvidence.agent_id == scope.agent_id,
        ]
        if not allow_agent_history:
            episode_filters.append(EpisodicEvidence.run_id == scope.run_id)
        episodes = self.db.scalars(select(EpisodicEvidence).where(*episode_filters)).all()
        for episode in episodes:
            if episode.outcome != "succeeded" or episode.redaction_status != "redacted":
                raise ValueError("episodic evidence must be successful and redacted")
            resolved[episode.evidence_id] = {
                "evidence_type": "episodic",
                "evidence_id": episode.evidence_id,
                "task_type": episode.task_type,
                "trajectory": episode.trajectory,
                "metrics": episode.metrics,
            }

        remaining = [item for item in requested if item not in resolved]
        if remaining:
            results = self.db.scalars(select(ResultEnvelopeRecord).where(
                ResultEnvelopeRecord.envelope_id.in_(remaining),
                ResultEnvelopeRecord.workspace_id == scope.workspace_id,
                ResultEnvelopeRecord.user_id == scope.user_id,
                ResultEnvelopeRecord.agent_id == scope.agent_id,
                ResultEnvelopeRecord.run_id == scope.run_id,
            )).all()
            for record in results:
                envelope = dict(record.envelope or {})
                result = envelope.get("result") if isinstance(envelope.get("result"), dict) else {}
                if (envelope.get("status") != "succeeded"
                        or result.get("_redaction_status") != "redacted"
                        or _contains_sensitive_key(result)):
                    raise ValueError("result evidence must be successful and redacted")
                resolved[record.envelope_id] = {
                    "evidence_type": "result_envelope",
                    "evidence_id": record.envelope_id,
                    "task_type": record.task_id,
                    "trajectory": {
                        "worker_id": record.worker_id,
                        "task_id": record.task_id,
                        "result": {key: value for key, value in result.items()
                                   if key != "_redaction_status"},
                        "artifact_refs": list(envelope.get("artifact_refs", [])),
                    },
                    "metrics": {},
                }
        missing = [item for item in requested if item not in resolved]
        if missing:
            raise LookupError("candidate evidence not found in orchestration scope")
        return tuple(resolved[item] for item in requested)

    def _allows_agent_history_replay(self, candidate: ProceduralCandidate) -> bool:
        """Trust only the server ledger marker emitted by consolidation."""
        source = self.db.scalar(select(LedgerEvent).where(
            LedgerEvent.event_id == candidate.source_event_id,
            LedgerEvent.workspace_id == candidate.workspace_id,
            LedgerEvent.user_id == candidate.user_id,
            LedgerEvent.agent_id == candidate.agent_id,
            LedgerEvent.event_type == "memory.procedural_candidate.proposed",
        ))
        return bool(source and source.payload.get("evidence_scope") == "agent_history")

    @staticmethod
    def _evidence_digest(candidate: ProceduralCandidate, evidence: tuple[dict[str, Any], ...]) -> str:
        canonical = json.dumps(
            {"candidate_id": candidate.candidate_id, "content": candidate.content,
             "evidence": evidence},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str,
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    def propose(
        self, scope: OrchestrationScope, *, candidate_type: str, name: str,
        content: dict[str, Any], evidence_ids: list[str], actor_id: str,
        idempotency_key: str,
    ) -> ProceduralCandidate:
        if candidate_type not in {"agent", "skill"} or not name or not evidence_ids:
            raise ValueError("candidate type, name, and trajectory evidence are required")
        evidence = self._verified_evidence(scope, evidence_ids)
        existing_event = self.db.scalar(select(LedgerEvent).where(
            LedgerEvent.workspace_id == scope.workspace_id,
            LedgerEvent.user_id == scope.user_id,
            LedgerEvent.agent_id == scope.agent_id,
            LedgerEvent.idempotency_key == idempotency_key,
        ))
        if existing_event:
            existing = self.db.scalar(select(ProceduralCandidate).where(
                ProceduralCandidate.source_event_id == existing_event.event_id
            ))
            if existing:
                return existing
        event = LedgerEvent(
            idempotency_key=idempotency_key, workspace_id=scope.workspace_id,
            user_id=scope.user_id, agent_id=scope.agent_id, run_id=scope.run_id,
            event_type="candidate.proposed",
            payload={"candidate_type": candidate_type, "evidence_ids": evidence_ids,
                     "evidence_types": [item["evidence_type"] for item in evidence]},
        )
        self.db.add(event)
        self.db.flush()
        candidate = ProceduralCandidate(
            workspace_id=scope.workspace_id, user_id=scope.user_id,
            agent_id=scope.agent_id, run_id=scope.run_id,
            source_event_id=event.event_id, candidate_type=candidate_type,
            name=name, content=content, evidence_ids=evidence_ids, status="candidate",
        )
        self.db.add(candidate)
        self.db.flush()
        self._audit(
            scope, action="candidate.proposed", decision="candidate",
            details={"candidate_id": candidate.candidate_id, "type": candidate_type},
            actor_id=actor_id,
        )
        self.db.commit()
        self.db.refresh(candidate)
        return candidate

    def redact(
        self, scope: OrchestrationScope, candidate_id: str, *,
        redacted_content: dict[str, Any], actor_id: str,
    ) -> ProceduralCandidate:
        candidate = self._candidate(scope, candidate_id)
        if candidate.status not in {"candidate", "published"}:
            raise ValueError("candidate cannot enter redaction from current status")
        if _contains_sensitive_key(redacted_content):
            raise ValueError("redacted candidate still contains sensitive fields")
        candidate.content = redacted_content
        candidate.status = "redacted"
        candidate.evaluation = {}
        candidate.approved_by = None
        candidate.updated_at = governance_now()
        self._audit(scope, action="candidate.redacted", decision="allowed",
                    details={"candidate_id": candidate_id}, actor_id=actor_id)
        self.db.commit()
        return candidate

    def evaluate(
        self, scope: OrchestrationScope, candidate_id: str, *,
        replay_report: dict[str, Any], actor_id: str, minimum_pass_rate: float = 0.8,
    ) -> ProceduralCandidate:
        candidate = self._candidate(scope, candidate_id)
        if candidate.status != "redacted":
            raise ValueError("redacted candidate must pass replay evaluation")
        if self.replay_executor is None:
            raise ValueError("server replay executor is not configured")
        evidence = self._verified_evidence(
            scope, candidate.evidence_ids,
            allow_agent_history=self._allows_agent_history_replay(candidate),
        )
        digest = self._evidence_digest(candidate, evidence)
        try:
            server_report = self.replay_executor(dict(candidate.content), evidence)
        except Exception as exc:
            raise ValueError("server replay evaluation failed closed") from exc
        if not isinstance(server_report, dict):
            raise ValueError("server replay evaluation returned an invalid report")
        try:
            passed = int(server_report["passed"])
            total = int(server_report["total"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("server replay evaluation requires passed and total counts") from exc
        if passed < 0 or total <= 0 or passed > total:
            raise ValueError("server replay evaluation counts are invalid")
        pass_rate = passed / total
        if pass_rate < minimum_pass_rate:
            raise ValueError("redacted candidate must pass replay evaluation")
        # The client report is retained only as an untrusted request annotation;
        # it never contributes to the pass/fail decision.
        candidate.evaluation = {
            "source": "server_replay",
            "evidence_sha256": digest,
            "passed": passed,
            "total": total,
            "replay_pass_rate": pass_rate,
            "details": server_report.get("details", {}),
            "client_annotation": dict(replay_report),
        }
        candidate.status = "evaluated"
        candidate.updated_at = governance_now()
        self._audit(scope, action="candidate.replay_evaluated", decision="passed",
                    details={"candidate_id": candidate_id, "pass_rate": pass_rate}, actor_id=actor_id)
        self.db.commit()
        return candidate

    def approve(
        self, scope: OrchestrationScope, candidate_id: str, *, actor_id: str,
        human_approval: bool,
    ) -> ProceduralCandidate:
        candidate = self._candidate(scope, candidate_id)
        if candidate.status != "evaluated" or not human_approval or not actor_id:
            raise ValueError("explicit human approval is required")
        candidate.status = "approved"
        candidate.approved_by = actor_id
        candidate.updated_at = governance_now()
        self._audit(scope, action="candidate.approved", decision="allowed",
                    details={"candidate_id": candidate_id}, actor_id=actor_id)
        self.db.commit()
        return candidate

    def publish(
        self, scope: OrchestrationScope, candidate_id: str, *,
        version: str, actor_id: str,
    ) -> CandidateAssetVersionRecord:
        candidate = self._candidate(scope, candidate_id)
        if candidate.status != "approved" or not candidate.approved_by or not version:
            raise ValueError("approved candidate and version are required")
        if self.db.scalar(select(CandidateAssetVersionRecord).where(
            CandidateAssetVersionRecord.candidate_id == candidate_id,
            CandidateAssetVersionRecord.version == version,
        )):
            raise ValueError("candidate version already exists")
        previous = self.db.scalars(select(CandidateAssetVersionRecord).where(
            CandidateAssetVersionRecord.candidate_id == candidate_id
        )).all()
        asset_id = previous[0].asset_id if previous else ""
        snapshot = dict(candidate.content)
        if candidate.candidate_type == "agent":
            asset_id, snapshot = self._publish_agent(scope, candidate, asset_id, snapshot, version, actor_id)
        elif candidate.candidate_type == "skill":
            asset_id, snapshot = self._publish_skill(scope, candidate, asset_id, snapshot, version)
        else:
            raise ValueError("unsupported candidate asset type")
        self.db.execute(update(CandidateAssetVersionRecord).where(
            CandidateAssetVersionRecord.candidate_id == candidate_id
        ).values(active=False))
        record = CandidateAssetVersionRecord(
            candidate_id=candidate_id, workspace_id=scope.workspace_id,
            asset_type=candidate.candidate_type, asset_id=asset_id, version=version,
            snapshot=snapshot, active=True, published_by=actor_id,
        )
        self.db.add(record)
        candidate.status = "published"
        candidate.asset_version = version
        candidate.published_at = governance_now()
        candidate.rolled_back_at = None
        candidate.updated_at = governance_now()
        self._audit(scope, action="candidate.published", decision="published",
                    details={"candidate_id": candidate_id, "asset_id": asset_id, "version": version},
                    actor_id=actor_id)
        self.db.commit()
        self.db.refresh(record)
        return record

    def rollback(
        self, scope: OrchestrationScope, candidate_id: str, *,
        version: str, actor_id: str,
    ) -> CandidateAssetVersionRecord:
        candidate = self._candidate(scope, candidate_id)
        if candidate.status != "published":
            raise ValueError("only published candidates can roll back")
        target = self.db.scalar(select(CandidateAssetVersionRecord).where(
            CandidateAssetVersionRecord.candidate_id == candidate_id,
            CandidateAssetVersionRecord.workspace_id == scope.workspace_id,
            CandidateAssetVersionRecord.version == version,
        ))
        if target is None:
            raise LookupError("candidate asset version not found")
        self.db.execute(update(CandidateAssetVersionRecord).where(
            CandidateAssetVersionRecord.candidate_id == candidate_id
        ).values(active=False))
        target.active = True
        target.rolled_back_at = governance_now()
        if candidate.candidate_type == "agent":
            agent = self.db.scalar(select(Agent).where(
                Agent.id == target.asset_id, Agent.workspace_id == scope.workspace_id
            ))
            if agent is None:
                raise LookupError("published Agent asset missing")
            agent.published_version_id = target.snapshot["agent_version_id"]
            agent.status = "published"
        else:
            skill = self.db.scalar(select(Skill).where(
                Skill.id == target.asset_id, Skill.workspace_id == scope.workspace_id
            ))
            if skill is None:
                raise LookupError("published Skill asset missing")
            self._apply_skill_snapshot(skill, target.snapshot)
            skill.status = "active"
        candidate.asset_version = version
        candidate.rolled_back_at = governance_now()
        candidate.updated_at = governance_now()
        self._audit(scope, action="candidate.rolled_back", decision="published",
                    details={"candidate_id": candidate_id, "asset_id": target.asset_id,
                             "version": version}, actor_id=actor_id)
        self.db.commit()
        self.db.refresh(target)
        return target

    def _publish_agent(
        self, scope: OrchestrationScope, candidate: ProceduralCandidate,
        asset_id: str, snapshot: dict[str, Any], version: str, actor_id: str,
    ) -> tuple[str, dict[str, Any]]:
        agent = self.db.scalar(select(Agent).where(
            Agent.id == asset_id, Agent.workspace_id == scope.workspace_id
        )) if asset_id else None
        if agent is None:
            agent = Agent(
                workspace_id=scope.workspace_id, created_by=actor_id,
                name=candidate.name, description=str(snapshot.get("description", "")),
                kind="prompt", system_prompt=str(snapshot.get("system_prompt", "")),
                model=str(snapshot.get("model", "gpt-4.1-mini")), status="draft",
            )
            self.db.add(agent)
            self.db.flush()
        next_no = int(self.db.scalar(select(func.max(AgentVersion.version_no)).where(
            AgentVersion.agent_id == agent.id
        )) or 0) + 1
        version_snapshot = {
            "system_prompt": str(snapshot.get("system_prompt", "")),
            "model": str(snapshot.get("model", "gpt-4.1-mini")),
            "knowledge_base_id": snapshot.get("knowledge_base_id"),
            "candidate_version": version,
        }
        agent_version = AgentVersion(
            agent_id=agent.id, version_no=next_no, kind="prompt", label="published",
            note=f"Governed candidate {candidate.candidate_id} {version}",
            snapshot_json=json.dumps(version_snapshot, ensure_ascii=False),
        )
        self.db.add(agent_version)
        self.db.flush()
        agent.name = candidate.name
        agent.description = str(snapshot.get("description", ""))
        agent.system_prompt = version_snapshot["system_prompt"]
        agent.model = version_snapshot["model"]
        agent.current_version_id = agent_version.id
        agent.published_version_id = agent_version.id
        agent.status = "published"
        return agent.id, {**version_snapshot, "agent_version_id": agent_version.id}

    def _publish_skill(
        self, scope: OrchestrationScope, candidate: ProceduralCandidate,
        asset_id: str, snapshot: dict[str, Any], version: str,
    ) -> tuple[str, dict[str, Any]]:
        skill = self.db.scalar(select(Skill).where(
            Skill.id == asset_id, Skill.workspace_id == scope.workspace_id
        )) if asset_id else None
        normalized = {
            "name": candidate.name,
            "description": str(snapshot.get("description", candidate.name)),
            "content": str(snapshot.get("content", "")),
            "category_path": str(snapshot.get("category_path", "general")),
            "summary": str(snapshot.get("summary", snapshot.get("description", candidate.name))),
            "use_when": list(snapshot.get("use_when", [])),
            "do_not_use_when": list(snapshot.get("do_not_use_when", [])),
            "examples": list(snapshot.get("examples", [])),
            "input_schema": dict(snapshot.get("input_schema", {"type": "object"})),
            "output_schema": dict(snapshot.get("output_schema", {"type": "object"})),
            "requirements": list(snapshot.get("requirements", [])),
            "permissions": list(snapshot.get("permissions", [])),
            "trigger_phrases": str(snapshot.get("trigger_phrases", "")),
            "version": version,
        }
        if skill is None:
            skill = Skill(workspace_id=scope.workspace_id, status="active", builtin=False)
            self.db.add(skill)
            self.db.flush()
        self._apply_skill_snapshot(skill, normalized)
        skill.status = "active"
        return skill.id, normalized

    @staticmethod
    def _apply_skill_snapshot(skill: Skill, snapshot: dict[str, Any]) -> None:
        skill.name = snapshot["name"]
        skill.description = snapshot["description"]
        skill.content = snapshot["content"]
        skill.category_path = snapshot["category_path"]
        skill.summary = snapshot["summary"]
        for field in ("use_when", "do_not_use_when", "examples", "input_schema",
                      "output_schema", "requirements", "permissions"):
            setattr(skill, field, json.dumps(snapshot[field], ensure_ascii=False))
        skill.trigger_phrases = snapshot["trigger_phrases"]
        skill.version = snapshot["version"]
