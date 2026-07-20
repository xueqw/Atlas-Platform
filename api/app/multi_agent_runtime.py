"""Bounded, inspectable multi-agent runtime primitives.

This module intentionally has no database, network, or model dependency. The API layer
persists its envelopes and audit events; these functions own the invariant checks so a
worker cannot obtain authority simply by changing its own request.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
import asyncio
import hashlib
import json
import secrets
from typing import Any, Awaitable, Callable, Mapping
import uuid

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError


MAX_WORKERS = 6
MAX_CONCURRENT_WORKERS = 3
MAX_REPLANS = 2
SCHEMA_VERSION = "v1"


WORKER_SPEC_JSON_SCHEMA: dict[str, Any] = {
    "$id": "atlas://schemas/worker-spec/v1",
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": ["schema_version", "worker_id", "version", "role", "objective", "input_schema", "output_schema"],
    "properties": {
        "schema_version": {"const": SCHEMA_VERSION},
        "worker_id": {"type": "string", "minLength": 1},
        "version": {"type": "string", "minLength": 1},
        "role": {"type": "string", "minLength": 1},
        "objective": {"type": "string", "minLength": 1},
        "input_schema": {"type": "object"},
        "output_schema": {"type": "object"},
        "allowed_tools": {"type": "array", "items": {"type": "string"}, "uniqueItems": True},
        "max_steps": {"type": "integer", "minimum": 1},
        "timeout_seconds": {"type": "integer", "minimum": 1},
        "lifecycle": {"enum": ["ephemeral", "session"]},
    },
    "additionalProperties": False,
}

TASK_ENVELOPE_JSON_SCHEMA: dict[str, Any] = {
    "$id": "atlas://schemas/task-envelope/v1",
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": ["schema_version", "envelope_id", "run_id", "task_id", "worker_id", "workspace_id", "user_id", "agent_id", "payload"],
    "properties": {
        "schema_version": {"const": SCHEMA_VERSION},
        "envelope_id": {"type": "string", "minLength": 1},
        "run_id": {"type": "string", "minLength": 1},
        "task_id": {"type": "string", "minLength": 1},
        "worker_id": {"type": "string", "minLength": 1},
        "workspace_id": {"type": "string", "minLength": 1},
        "user_id": {"type": "string", "minLength": 1},
        "agent_id": {"type": "string", "minLength": 1},
        "payload": {"type": "object"},
        "artifact_refs": {"type": "array", "items": {"type": "string"}},
    },
    "additionalProperties": False,
}

RESULT_ENVELOPE_JSON_SCHEMA: dict[str, Any] = {
    "$id": "atlas://schemas/result-envelope/v1",
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": ["schema_version", "envelope_id", "run_id", "task_id", "worker_id", "workspace_id", "user_id", "agent_id", "status", "result"],
    "properties": {
        "schema_version": {"const": SCHEMA_VERSION},
        "envelope_id": {"type": "string", "minLength": 1},
        "run_id": {"type": "string", "minLength": 1},
        "task_id": {"type": "string", "minLength": 1},
        "worker_id": {"type": "string", "minLength": 1},
        "workspace_id": {"type": "string", "minLength": 1},
        "user_id": {"type": "string", "minLength": 1},
        "agent_id": {"type": "string", "minLength": 1},
        "status": {"enum": ["succeeded", "failed", "cancelled", "timed_out", "paused"]},
        "result": {"type": "object"},
        "artifact_refs": {"type": "array", "items": {"type": "string"}},
        "error": {"type": "string"},
        "error_category": {"type": "string"},
        "runtime_run_id": {"type": "string"},
    },
    "additionalProperties": False,
}

TOOL_AUTHORIZATION_TICKET_JSON_SCHEMA: dict[str, Any] = {
    "$id": "atlas://schemas/tool-authorization-ticket/v1",
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": ["schema_version", "ticket_id", "run_id", "worker_id", "tool_name", "parameter_digest", "expires_at", "nonce", "scope"],
    "properties": {
        "schema_version": {"const": SCHEMA_VERSION},
        "ticket_id": {"type": "string", "minLength": 1},
        "run_id": {"type": "string", "minLength": 1},
        "worker_id": {"type": "string", "minLength": 1},
        "tool_name": {"type": "string", "minLength": 1},
        "parameter_digest": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
        "expires_at": {"type": "string", "format": "date-time"},
        "nonce": {"type": "string", "minLength": 1},
        "scope": {"type": "array", "items": {"type": "string"}, "uniqueItems": True},
    },
    "additionalProperties": False,
}


def contract_schemas() -> dict[str, dict[str, Any]]:
    """Return detached versioned schemas suitable for API publication."""
    return {
        "WorkerSpec": dict(WORKER_SPEC_JSON_SCHEMA),
        "TaskEnvelope": dict(TASK_ENVELOPE_JSON_SCHEMA),
        "ResultEnvelope": dict(RESULT_ENVELOPE_JSON_SCHEMA),
        "ToolAuthorizationTicket": dict(TOOL_AUTHORIZATION_TICKET_JSON_SCHEMA),
    }


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def canonical_parameters(parameters: dict[str, Any]) -> str:
    """Stable serialization is the authorization binding, not a mutable dict object."""
    return json.dumps(parameters, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def parameter_hash(parameters: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_parameters(parameters).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class WorkerSpec:
    worker_id: str
    version: str
    role: str
    objective: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    allowed_tools: frozenset[str] = frozenset()
    max_steps: int = 8
    timeout_seconds: int = 120
    lifecycle: str = "ephemeral"  # ephemeral | session
    schema_version: str = SCHEMA_VERSION

    def validate(self) -> None:
        if not self.worker_id or not self.version or not self.role or not self.objective:
            raise ValueError("WorkerSpec requires id, version, role, and objective")
        if self.lifecycle not in {"ephemeral", "session"}:
            raise ValueError("WorkerSpec lifecycle must be ephemeral or session")
        if self.max_steps < 1 or self.timeout_seconds < 1:
            raise ValueError("WorkerSpec limits must be positive")
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError("unsupported WorkerSpec schema version")
        if self.input_schema.get("type") not in {None, "object"}:
            raise ValueError("WorkerSpec input_schema root must be object")
        if self.output_schema.get("type") not in {None, "object"}:
            raise ValueError("WorkerSpec output_schema root must be object")

    def as_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "schema_version": self.schema_version,
            "worker_id": self.worker_id,
            "version": self.version,
            "role": self.role,
            "objective": self.objective,
            "input_schema": self.input_schema,
            "output_schema": self.output_schema,
            "allowed_tools": sorted(self.allowed_tools),
            "max_steps": self.max_steps,
            "timeout_seconds": self.timeout_seconds,
            "lifecycle": self.lifecycle,
        }


@dataclass(frozen=True)
class TaskEnvelope:
    envelope_id: str
    run_id: str
    task_id: str
    worker_id: str
    workspace_id: str
    user_id: str
    agent_id: str
    payload: dict[str, Any]
    artifact_refs: tuple[str, ...] = ()
    schema_version: str = "v1"

    def validate(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError("unsupported TaskEnvelope schema version")
        if not all((self.envelope_id, self.run_id, self.task_id, self.worker_id,
                    self.workspace_id, self.user_id, self.agent_id)):
            raise ValueError("TaskEnvelope identity fields are required")
        if not isinstance(self.payload, dict):
            raise ValueError("TaskEnvelope payload must be an object")


@dataclass(frozen=True)
class ResultEnvelope:
    envelope_id: str
    task_id: str
    worker_id: str
    status: str
    result: dict[str, Any]
    artifact_refs: tuple[str, ...] = ()
    error: str = ""
    error_category: str = ""
    runtime_run_id: str = ""
    schema_version: str = "v1"
    run_id: str = ""
    workspace_id: str = ""
    user_id: str = ""
    agent_id: str = ""

    def validate(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError("unsupported ResultEnvelope schema version")
        if self.status not in {"succeeded", "failed", "cancelled", "timed_out", "paused"}:
            raise ValueError("invalid ResultEnvelope status")
        if not all((self.envelope_id, self.run_id, self.task_id, self.worker_id,
                    self.workspace_id, self.user_id, self.agent_id)):
            raise ValueError("ResultEnvelope identity fields are required")
        if self.status == "succeeded" and self.error:
            raise ValueError("successful ResultEnvelope cannot contain an error")


@dataclass(frozen=True)
class AuthorizationTicket:
    ticket_id: str
    run_id: str
    worker_id: str
    tool_name: str
    parameter_digest: str
    expires_at: datetime
    nonce: str
    scope: frozenset[str] = frozenset()
    schema_version: str = SCHEMA_VERSION


ToolAuthorizationTicket = AuthorizationTicket


def effective_tools(*grants: set[str] | frozenset[str]) -> set[str]:
    """Return the least-privilege intersection. Empty is deny-by-default."""
    normalized = [set(g) for g in grants]
    if not normalized:
        return set()
    return set.intersection(*normalized)


def issue_ticket(run_id: str, worker_id: str, tool_name: str, parameters: dict[str, Any],
                 *, scope: set[str] | None = None, ttl_seconds: int = 300,
                 now: datetime | None = None) -> AuthorizationTicket:
    if ttl_seconds < 1:
        raise ValueError("ticket ttl must be positive")
    now = now or utcnow()
    return AuthorizationTicket(
        ticket_id=secrets.token_urlsafe(18), run_id=run_id, worker_id=worker_id,
        tool_name=tool_name, parameter_digest=parameter_hash(parameters),
        expires_at=now + timedelta(seconds=ttl_seconds), nonce=secrets.token_urlsafe(12),
        scope=frozenset(scope or set()),
    )


def validate_ticket(ticket: AuthorizationTicket, *, run_id: str, worker_id: str,
                    tool_name: str, parameters: dict[str, Any], used_ticket_ids: set[str],
                    now: datetime | None = None,
                    required_scope: set[str] | frozenset[str] | None = None) -> tuple[bool, str]:
    now = now or utcnow()
    if ticket.ticket_id in used_ticket_ids:
        return False, "replayed_ticket"
    if ticket.run_id != run_id or ticket.worker_id != worker_id or ticket.tool_name != tool_name:
        return False, "ticket_scope_mismatch"
    if ticket.expires_at <= now:
        return False, "ticket_expired"
    if ticket.parameter_digest != parameter_hash(parameters):
        return False, "parameters_changed"
    if required_scope is not None and not set(required_scope).issubset(ticket.scope):
        return False, "ticket_scope_mismatch"
    return True, "authorized"


def consume_ticket(ticket: AuthorizationTicket, used_ticket_ids: set[str]) -> None:
    if ticket.ticket_id in used_ticket_ids:
        raise ValueError("authorization ticket already consumed")
    used_ticket_ids.add(ticket.ticket_id)


@dataclass(frozen=True)
class ToolPolicy:
    """Policy decision for one tool; confirmation never implies broader authority."""

    mode: str  # auto | conditional | confirm | deny
    required_scope: frozenset[str] = frozenset()

    def validate(self) -> None:
        if self.mode not in {"auto", "conditional", "confirm", "deny"}:
            raise ValueError("invalid tool policy mode")


def authorize_tool_call(
    *,
    tool_name: str,
    parameters: dict[str, Any],
    policy: ToolPolicy,
    user_grant: set[str],
    orchestrator_grant: set[str],
    worker_grant: set[str] | frozenset[str],
    current_authorization: set[str],
    ticket: AuthorizationTicket | None = None,
    run_id: str,
    worker_id: str,
    used_ticket_ids: set[str],
    now: datetime | None = None,
) -> tuple[bool, str]:
    """Evaluate least-privilege authority and, when required, a bound ticket."""
    policy.validate()
    if tool_name not in effective_tools(
        user_grant, orchestrator_grant, worker_grant, current_authorization
    ):
        return False, "tool_not_in_permission_intersection"
    if policy.mode == "deny":
        return False, "tool_policy_denied"
    if policy.mode == "auto":
        return True, "authorized_automatically"
    if policy.mode == "conditional" and not policy.required_scope:
        return True, "authorized_conditionally"
    if ticket is None:
        return False, "confirmation_required"
    valid, reason = validate_ticket(
        ticket,
        run_id=run_id,
        worker_id=worker_id,
        tool_name=tool_name,
        parameters=parameters,
        used_ticket_ids=used_ticket_ids,
        required_scope=policy.required_scope,
        now=now,
    )
    if valid:
        consume_ticket(ticket, used_ticket_ids)
    return valid, reason


@dataclass
class DagTask:
    task_id: str
    worker_id: str
    depends_on: set[str] = field(default_factory=set)
    status: str = "pending"  # pending|running|paused|succeeded|failed|cancelled
    attempts: int = 0
    max_attempts: int = 2
    timeout_seconds: int = 120
    started_at: datetime | None = None


class DagScheduler:
    """A deterministic scheduler that keeps orchestration bounds outside workers."""

    def __init__(self, tasks: list[DagTask], *, max_workers: int = MAX_WORKERS,
                 concurrency: int = MAX_CONCURRENT_WORKERS, max_replans: int = MAX_REPLANS):
        if len(tasks) > max_workers:
            raise ValueError("worker cap exceeded")
        if concurrency < 1 or concurrency > max_workers:
            raise ValueError("invalid concurrency cap")
        ids = [task.task_id for task in tasks]
        if len(set(ids)) != len(ids):
            raise ValueError("task IDs must be unique")
        if any(not task.depends_on.issubset(ids) for task in tasks):
            raise ValueError("unknown DAG dependency")
        if any(task.task_id in task.depends_on for task in tasks):
            raise ValueError("DAG task cannot depend on itself")
        self.tasks = {task.task_id: task for task in tasks}
        self.concurrency = concurrency
        self.max_replans = max_replans
        self.replans = 0
        self.cancelled = False
        self._assert_acyclic()

    def _assert_acyclic(self) -> None:
        remaining = {task_id: set(task.depends_on) for task_id, task in self.tasks.items()}
        while remaining:
            roots = {task_id for task_id, dependencies in remaining.items() if not dependencies}
            if not roots:
                raise ValueError("task dependencies must form an acyclic DAG")
            remaining = {
                task_id: dependencies - roots
                for task_id, dependencies in remaining.items()
                if task_id not in roots
            }

    def ready(self) -> list[DagTask]:
        running = sum(task.status == "running" for task in self.tasks.values())
        slots = self.concurrency - running
        if slots <= 0:
            return []
        done = {task.task_id for task in self.tasks.values() if task.status == "succeeded"}
        candidates = [task for task in self.tasks.values()
                      if task.status == "pending" and task.depends_on.issubset(done)]
        return sorted(candidates, key=lambda task: task.task_id)[:slots]

    def dispatch(self) -> list[DagTask]:
        if self.cancelled:
            return []
        ready = self.ready()
        for task in ready:
            task.status = "running"
            task.attempts += 1
            task.started_at = utcnow()
        return ready

    def complete(self, task_id: str, *, success: bool, retryable: bool = False) -> str:
        task = self.tasks[task_id]
        if task.status != "running":
            raise ValueError("only running tasks can complete")
        if success:
            task.status = "succeeded"
        elif retryable and task.attempts < task.max_attempts:
            task.status = "pending"
        else:
            task.status = "failed"
            self._cancel_dependents(task_id)
        return task.status

    def timeout(self, task_id: str) -> str:
        task = self.tasks[task_id]
        if task.status != "running":
            raise ValueError("only running tasks can time out")
        task.status = "failed"
        self._cancel_dependents(task_id)
        return task.status

    def cancel(self) -> None:
        self.cancelled = True
        for task in self.tasks.values():
            if task.status in {"pending", "running"}:
                task.status = "cancelled"

    def _cancel_dependents(self, failed_id: str) -> None:
        changed = True
        while changed:
            changed = False
            for task in self.tasks.values():
                if task.status == "pending" and any(
                    self.tasks[parent].status in {"failed", "cancelled"} for parent in task.depends_on
                ):
                    task.status = "cancelled"
                    changed = True

    def request_replan(self) -> bool:
        if self.replans >= self.max_replans:
            return False
        self.replans += 1
        return True

    def terminal(self) -> bool:
        return all(task.status in {"succeeded", "failed", "cancelled"} for task in self.tasks.values())

    def paused(self) -> bool:
        return any(task.status == "paused" for task in self.tasks.values())

    def resume_task(self, task_id: str) -> None:
        task = self.tasks[task_id]
        if task.status != "paused":
            raise ValueError("only paused tasks can be resumed")
        task.status = "pending"


def _matches_object_schema(value: dict[str, Any], schema: Mapping[str, Any]) -> bool:
    """Validate a detached payload with the pinned JSON Schema 2020-12 engine."""
    if not isinstance(value, dict) or not isinstance(schema, Mapping):
        return False
    try:
        Draft202012Validator.check_schema(schema)
        return Draft202012Validator(schema).is_valid(value)
    except SchemaError:
        return False


@dataclass
class OrchestratorTeam:
    """Sole factory for bounded workers and exact-scope envelopes."""

    run_id: str
    workspace_id: str
    user_id: str
    agent_id: str
    max_workers: int = MAX_WORKERS
    workers: dict[str, WorkerSpec] = field(default_factory=dict)
    issued_tasks: dict[str, TaskEnvelope] = field(default_factory=dict)

    def create_worker(self, spec: WorkerSpec) -> WorkerSpec:
        spec.validate()
        if len(self.workers) >= self.max_workers:
            raise ValueError("worker cap exceeded")
        if spec.worker_id in self.workers:
            raise ValueError("worker ID already exists in this run")
        detached = replace(
            spec,
            input_schema=deepcopy(spec.input_schema),
            output_schema=deepcopy(spec.output_schema),
            allowed_tools=frozenset(spec.allowed_tools),
        )
        self.workers[spec.worker_id] = detached
        return detached

    def terminate_worker(self, worker_id: str) -> WorkerSpec:
        try:
            return self.workers.pop(worker_id)
        except KeyError as exc:
            raise LookupError("worker not found in this run") from exc

    def make_task(
        self, *, task_id: str, worker_id: str, payload: dict[str, Any],
        artifact_refs: tuple[str, ...] = (),
    ) -> TaskEnvelope:
        if task_id in self.issued_tasks:
            raise ValueError("task ID already exists in this run")
        spec = self.workers.get(worker_id)
        if spec is None:
            raise LookupError("only the orchestrator may address a created worker")
        if not _matches_object_schema(payload, spec.input_schema):
            raise ValueError("task payload does not match WorkerSpec input_schema")
        envelope = TaskEnvelope(
            envelope_id=str(uuid.uuid4()), run_id=self.run_id, task_id=task_id,
            worker_id=worker_id, workspace_id=self.workspace_id, user_id=self.user_id,
            agent_id=self.agent_id, payload=deepcopy(payload), artifact_refs=tuple(artifact_refs),
        )
        envelope.validate()
        self.issued_tasks[task_id] = envelope
        return envelope

    def accept_result(self, result: ResultEnvelope) -> ResultEnvelope:
        result.validate()
        task = self.issued_tasks.get(result.task_id)
        if task is None:
            raise LookupError("result references an unknown task")
        expected_scope = (
            self.run_id, task.worker_id, self.workspace_id, self.user_id, self.agent_id
        )
        supplied_scope = (
            result.run_id, result.worker_id, result.workspace_id, result.user_id, result.agent_id
        )
        if supplied_scope != expected_scope:
            raise PermissionError("result envelope scope does not match its isolated task")
        spec = self.workers.get(result.worker_id)
        if spec is None:
            raise LookupError("result worker is no longer active")
        if result.status == "succeeded" and not _matches_object_schema(result.result, spec.output_schema):
            raise ValueError("result does not match WorkerSpec output_schema")
        # Detach mutable worker-owned output before it enters orchestrator state
        # or persistence. Frozen envelopes alone do not freeze nested objects.
        return replace(
            result,
            result=deepcopy(result.result),
            artifact_refs=tuple(result.artifact_refs),
        )


async def execute_ready_parallel(
    scheduler: DagScheduler,
    execute: Callable[[DagTask], Awaitable[ResultEnvelope]],
) -> list[ResultEnvelope]:
    """Dispatch one dependency-free batch concurrently within the scheduler cap."""
    tasks = scheduler.dispatch()
    if not tasks:
        return []

    async def run_one(task: DagTask) -> ResultEnvelope:
        try:
            result = await asyncio.wait_for(execute(task), timeout=task.timeout_seconds)
        except asyncio.TimeoutError:
            scheduler.timeout(task.task_id)
            return ResultEnvelope(
                envelope_id=str(uuid.uuid4()), task_id=task.task_id, worker_id=task.worker_id,
                status="timed_out", result={}, error="worker timeout",
            )
        scheduler.complete(task.task_id, success=result.status == "succeeded", retryable=False)
        return result

    return list(await asyncio.gather(*(run_one(task) for task in tasks)))


def aggregate_results(
    results: list[ResultEnvelope], *, conflict_keys: set[str] = frozenset()
) -> dict[str, Any]:
    """Combine successful worker results and surface conflicting scalar claims."""
    ordered = sorted(results, key=lambda item: (item.task_id, item.worker_id, item.envelope_id))
    conflicts: list[dict[str, Any]] = []
    claims: dict[str, tuple[Any, str]] = {}
    for envelope in ordered:
        if envelope.status != "succeeded":
            continue
        for key in conflict_keys:
            if key not in envelope.result:
                continue
            value = envelope.result[key]
            if key in claims and claims[key][0] != value:
                conflicts.append({
                    "key": key,
                    "first_task_id": claims[key][1], "first_value": claims[key][0],
                    "conflicting_task_id": envelope.task_id, "conflicting_value": value,
                })
            else:
                claims[key] = (value, envelope.task_id)
    return {
        "status": "conflict" if conflicts else (
            "succeeded" if all(item.status == "succeeded" for item in ordered) else "partial"
        ),
        "results": [
            {"task_id": item.task_id, "worker_id": item.worker_id,
             "status": item.status, "result": item.result, "artifact_refs": list(item.artifact_refs),
             "error": item.error, "error_category": item.error_category,
             "runtime_run_id": item.runtime_run_id}
            for item in ordered
        ],
        "conflicts": conflicts,
    }


@dataclass
class CandidateAsset:
    """Redacted, evaluated, human-approved versioned Agent/Skill proposal."""

    candidate_id: str
    asset_type: str
    name: str
    content: dict[str, Any]
    evidence_refs: tuple[str, ...]
    status: str = "candidate"
    redacted: bool = False
    evaluation: dict[str, Any] = field(default_factory=dict)
    approved_by: str = ""
    versions: list[dict[str, Any]] = field(default_factory=list)
    audit: list[dict[str, Any]] = field(default_factory=list)

    def redact(self, *, actor_id: str, content: dict[str, Any]) -> None:
        if self.status != "candidate":
            raise ValueError("only candidate assets can be redacted")
        self.content = dict(content)
        self.redacted = True
        self.audit.append({"action": "redacted", "actor_id": actor_id})

    def evaluate(self, *, actor_id: str, report: dict[str, Any]) -> None:
        if not self.redacted or self.status != "candidate" or not report:
            raise ValueError("redacted candidate and evaluation report are required")
        self.evaluation = dict(report)
        self.status = "evaluated"
        self.audit.append({"action": "evaluated", "actor_id": actor_id})

    def approve(self, *, actor_id: str) -> None:
        if self.status != "evaluated":
            raise ValueError("candidate must be evaluated before approval")
        self.status = "approved"
        self.approved_by = actor_id
        self.audit.append({"action": "approved", "actor_id": actor_id})

    def publish(self, *, actor_id: str, version: str) -> dict[str, Any]:
        if self.status != "approved" or not self.approved_by or not version:
            raise ValueError("approved candidate and version are required for publish")
        if any(item["version"] == version for item in self.versions):
            raise ValueError("asset version already exists")
        published = {"version": version, "content": dict(self.content), "active": True}
        for item in self.versions:
            item["active"] = False
        self.versions.append(published)
        self.status = "published"
        self.audit.append({"action": "published", "actor_id": actor_id, "version": version})
        return published

    def rollback(self, *, actor_id: str, version: str) -> dict[str, Any]:
        if self.status != "published":
            raise ValueError("only published assets can roll back")
        target = next((item for item in self.versions if item["version"] == version), None)
        if target is None:
            raise LookupError("rollback version not found")
        for item in self.versions:
            item["active"] = item is target
        self.audit.append({"action": "rolled_back", "actor_id": actor_id, "version": version})
        return target
