"""Bounded, inspectable multi-agent runtime primitives.

This module intentionally has no database, network, or model dependency. The API layer
persists its envelopes and audit events; these functions own the invariant checks so a
worker cannot obtain authority simply by changing its own request.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import json
import secrets
from typing import Any


MAX_WORKERS = 6
MAX_CONCURRENT_WORKERS = 3
MAX_REPLANS = 2


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

    def validate(self) -> None:
        if not self.worker_id or not self.version or not self.role or not self.objective:
            raise ValueError("WorkerSpec requires id, version, role, and objective")
        if self.lifecycle not in {"ephemeral", "session"}:
            raise ValueError("WorkerSpec lifecycle must be ephemeral or session")
        if self.max_steps < 1 or self.timeout_seconds < 1:
            raise ValueError("WorkerSpec limits must be positive")


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


@dataclass(frozen=True)
class ResultEnvelope:
    envelope_id: str
    task_id: str
    worker_id: str
    status: str
    result: dict[str, Any]
    artifact_refs: tuple[str, ...] = ()
    error: str = ""
    schema_version: str = "v1"


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
                    now: datetime | None = None) -> tuple[bool, str]:
    now = now or utcnow()
    if ticket.ticket_id in used_ticket_ids:
        return False, "replayed_ticket"
    if ticket.run_id != run_id or ticket.worker_id != worker_id or ticket.tool_name != tool_name:
        return False, "ticket_scope_mismatch"
    if ticket.expires_at <= now:
        return False, "ticket_expired"
    if ticket.parameter_digest != parameter_hash(parameters):
        return False, "parameters_changed"
    return True, "authorized"


def consume_ticket(ticket: AuthorizationTicket, used_ticket_ids: set[str]) -> None:
    used_ticket_ids.add(ticket.ticket_id)


@dataclass
class DagTask:
    task_id: str
    worker_id: str
    depends_on: set[str] = field(default_factory=set)
    status: str = "pending"  # pending|running|succeeded|failed|cancelled
    attempts: int = 0
    max_attempts: int = 2


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
        self.tasks = {task.task_id: task for task in tasks}
        self.concurrency = concurrency
        self.max_replans = max_replans
        self.replans = 0

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
        ready = self.ready()
        for task in ready:
            task.status = "running"
            task.attempts += 1
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
