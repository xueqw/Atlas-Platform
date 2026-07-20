"""Tenant-scoped persistence for governed multi-agent orchestration."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from .governance_models import (
    ArtifactRecord,
    GovernanceAuditRecord,
    OrchestrationRunRecord,
    ResultEnvelopeRecord,
    TaskEnvelopeRecord,
    ToolAuthorizationRecord,
    WorkerRecord,
    governance_now,
)
from .multi_agent_runtime import (
    AuthorizationTicket,
    ResultEnvelope,
    TaskEnvelope,
    ToolPolicy,
    WorkerSpec,
    effective_tools,
    issue_ticket,
    parameter_hash,
)


@dataclass(frozen=True)
class OrchestrationScope:
    workspace_id: str
    user_id: str
    agent_id: str
    run_id: str

    def validate(self) -> None:
        if not all((self.workspace_id, self.user_id, self.agent_id, self.run_id)):
            raise ValueError("complete orchestration scope is required")


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _task_payload(envelope: TaskEnvelope) -> dict[str, Any]:
    envelope.validate()
    return {
        "schema_version": envelope.schema_version,
        "envelope_id": envelope.envelope_id,
        "run_id": envelope.run_id,
        "task_id": envelope.task_id,
        "worker_id": envelope.worker_id,
        "workspace_id": envelope.workspace_id,
        "user_id": envelope.user_id,
        "agent_id": envelope.agent_id,
        "payload": envelope.payload,
        "artifact_refs": list(envelope.artifact_refs),
    }


def _result_payload(envelope: ResultEnvelope) -> dict[str, Any]:
    envelope.validate()
    return {
        "schema_version": envelope.schema_version,
        "envelope_id": envelope.envelope_id,
        "run_id": envelope.run_id,
        "task_id": envelope.task_id,
        "worker_id": envelope.worker_id,
        "workspace_id": envelope.workspace_id,
        "user_id": envelope.user_id,
        "agent_id": envelope.agent_id,
        "status": envelope.status,
        "result": envelope.result,
        "artifact_refs": list(envelope.artifact_refs),
        "error": envelope.error,
        "error_category": envelope.error_category,
        "runtime_run_id": envelope.runtime_run_id,
    }


class MultiAgentRepository:
    """Repository whose every read and mutation is bound to a full tenant scope."""

    def __init__(self, db: Session):
        self.db = db

    def append_audit(
        self,
        scope: OrchestrationScope,
        *,
        action: str,
        decision: str,
        details: dict[str, Any] | None = None,
        worker_id: str | None = None,
        commit: bool = True,
    ) -> GovernanceAuditRecord:
        scope.validate()
        record = GovernanceAuditRecord(
            workspace_id=scope.workspace_id,
            user_id=scope.user_id,
            agent_id=scope.agent_id,
            run_id=scope.run_id,
            worker_id=worker_id,
            action=action,
            decision=decision,
            details=details or {},
        )
        self.db.add(record)
        if commit:
            self.db.commit()
            self.db.refresh(record)
        return record

    def create_run(self, scope: OrchestrationScope, *, scheduler_state: dict[str, Any]) -> OrchestrationRunRecord:
        scope.validate()
        existing = self.db.get(OrchestrationRunRecord, scope.run_id)
        if existing:
            if (existing.workspace_id, existing.user_id, existing.agent_id) != (
                scope.workspace_id, scope.user_id, scope.agent_id
            ):
                raise PermissionError("run ID belongs to another orchestration scope")
            return existing
        record = OrchestrationRunRecord(
            run_id=scope.run_id,
            workspace_id=scope.workspace_id,
            user_id=scope.user_id,
            agent_id=scope.agent_id,
            status="created",
            scheduler_state=scheduler_state,
        )
        self.db.add(record)
        self.append_audit(
            scope, action="orchestration.created", decision="allowed",
            details={"scheduler_state": scheduler_state}, commit=False,
        )
        self.db.commit()
        self.db.refresh(record)
        return record

    def get_run(self, scope: OrchestrationScope) -> OrchestrationRunRecord:
        scope.validate()
        record = self.db.scalar(select(OrchestrationRunRecord).where(
            OrchestrationRunRecord.run_id == scope.run_id,
            OrchestrationRunRecord.workspace_id == scope.workspace_id,
            OrchestrationRunRecord.user_id == scope.user_id,
            OrchestrationRunRecord.agent_id == scope.agent_id,
        ))
        if record is None:
            raise LookupError("orchestration run not found in scope")
        return record

    def save_run(
        self,
        scope: OrchestrationScope,
        *,
        status: str,
        scheduler_state: dict[str, Any],
        aggregate: dict[str, Any] | None = None,
        ended: bool = False,
    ) -> OrchestrationRunRecord:
        record = self.get_run(scope)
        record.status = status
        record.scheduler_state = scheduler_state
        if aggregate is not None:
            record.aggregate = aggregate
        record.updated_at = governance_now()
        if ended:
            record.ended_at = governance_now()
        self.db.commit()
        self.db.refresh(record)
        return record

    def save_worker(self, scope: OrchestrationScope, spec: WorkerSpec) -> WorkerRecord:
        spec.validate()
        # The storage key is run-namespaced because the original additive schema
        # uses worker_id + version as its physical primary key.
        storage_worker_id = f"{scope.run_id}:{spec.worker_id}"
        if len(storage_worker_id) > 80:
            raise ValueError("run and worker IDs exceed persistence key limit")
        record = WorkerRecord(
            worker_id=storage_worker_id,
            version=spec.version,
            workspace_id=scope.workspace_id,
            user_id=scope.user_id,
            agent_id=scope.agent_id,
            run_id=scope.run_id,
            spec=spec.as_dict(),
            status="created",
        )
        self.db.add(record)
        self.append_audit(
            scope, action="worker.created", decision="allowed",
            details={"worker_id": spec.worker_id, "version": spec.version},
            worker_id=spec.worker_id, commit=False,
        )
        self.db.commit()
        return record

    def list_workers(self, scope: OrchestrationScope) -> list[WorkerRecord]:
        self.get_run(scope)
        return list(self.db.scalars(select(WorkerRecord).where(
            WorkerRecord.workspace_id == scope.workspace_id,
            WorkerRecord.user_id == scope.user_id,
            WorkerRecord.agent_id == scope.agent_id,
            WorkerRecord.run_id == scope.run_id,
        ).order_by(WorkerRecord.created_at)))

    def save_task(self, scope: OrchestrationScope, envelope: TaskEnvelope) -> TaskEnvelopeRecord:
        if (envelope.workspace_id, envelope.user_id, envelope.agent_id, envelope.run_id) != (
            scope.workspace_id, scope.user_id, scope.agent_id, scope.run_id
        ):
            raise PermissionError("task envelope scope mismatch")
        record = TaskEnvelopeRecord(
            envelope_id=envelope.envelope_id,
            workspace_id=scope.workspace_id,
            user_id=scope.user_id,
            agent_id=scope.agent_id,
            run_id=scope.run_id,
            worker_id=envelope.worker_id,
            task_id=envelope.task_id,
            schema_version=envelope.schema_version,
            envelope=_task_payload(envelope),
        )
        self.db.add(record)
        self.append_audit(
            scope, action="task.enveloped", decision="allowed",
            details={"task_id": envelope.task_id, "envelope_id": envelope.envelope_id},
            worker_id=envelope.worker_id, commit=False,
        )
        self.db.commit()
        return record

    def list_tasks(self, scope: OrchestrationScope) -> list[TaskEnvelopeRecord]:
        self.get_run(scope)
        return list(self.db.scalars(select(TaskEnvelopeRecord).where(
            TaskEnvelopeRecord.workspace_id == scope.workspace_id,
            TaskEnvelopeRecord.user_id == scope.user_id,
            TaskEnvelopeRecord.agent_id == scope.agent_id,
            TaskEnvelopeRecord.run_id == scope.run_id,
        ).order_by(TaskEnvelopeRecord.created_at)))

    def save_result(self, scope: OrchestrationScope, envelope: ResultEnvelope) -> ResultEnvelopeRecord:
        if (envelope.workspace_id, envelope.user_id, envelope.agent_id, envelope.run_id) != (
            scope.workspace_id, scope.user_id, scope.agent_id, scope.run_id
        ):
            raise PermissionError("result envelope scope mismatch")
        record = ResultEnvelopeRecord(
            envelope_id=envelope.envelope_id,
            workspace_id=scope.workspace_id,
            user_id=scope.user_id,
            agent_id=scope.agent_id,
            run_id=scope.run_id,
            worker_id=envelope.worker_id,
            task_id=envelope.task_id,
            schema_version=envelope.schema_version,
            envelope=_result_payload(envelope),
        )
        self.db.add(record)
        self.append_audit(
            scope, action="result.accepted", decision=envelope.status,
            details={"task_id": envelope.task_id, "envelope_id": envelope.envelope_id},
            worker_id=envelope.worker_id, commit=False,
        )
        self.db.commit()
        return record

    def list_results(self, scope: OrchestrationScope) -> list[ResultEnvelopeRecord]:
        self.get_run(scope)
        return list(self.db.scalars(select(ResultEnvelopeRecord).where(
            ResultEnvelopeRecord.workspace_id == scope.workspace_id,
            ResultEnvelopeRecord.user_id == scope.user_id,
            ResultEnvelopeRecord.agent_id == scope.agent_id,
            ResultEnvelopeRecord.run_id == scope.run_id,
        ).order_by(ResultEnvelopeRecord.created_at)))

    def save_artifact(
        self, scope: OrchestrationScope, *, artifact_id: str, worker_id: str,
        storage_ref: str, media_type: str, sha256: str,
    ) -> ArtifactRecord:
        if not storage_ref or len(sha256) != 64:
            raise ValueError("artifact storage reference and SHA-256 are required")
        record = ArtifactRecord(
            artifact_id=artifact_id, workspace_id=scope.workspace_id,
            user_id=scope.user_id, agent_id=scope.agent_id, run_id=scope.run_id,
            worker_id=worker_id, storage_ref=storage_ref,
            media_type=media_type, sha256=sha256,
        )
        self.db.add(record)
        self.append_audit(
            scope, action="artifact.recorded", decision="allowed",
            details={"artifact_id": artifact_id, "sha256": sha256},
            worker_id=worker_id, commit=False,
        )
        self.db.commit()
        return record

    def validate_artifact_refs(
        self,
        scope: OrchestrationScope,
        refs: tuple[str, ...] | list[str],
    ) -> list[ArtifactRecord]:
        """Resolve artifact IDs/storage refs without disclosing another scope."""
        unique = {str(item) for item in refs if str(item)}
        if not unique:
            return []
        records = list(self.db.scalars(select(ArtifactRecord).where(
            ArtifactRecord.workspace_id == scope.workspace_id,
            ArtifactRecord.user_id == scope.user_id,
            ArtifactRecord.agent_id == scope.agent_id,
            or_(
                ArtifactRecord.artifact_id.in_(unique),
                ArtifactRecord.storage_ref.in_(unique),
            ),
        )))
        resolved = {item.artifact_id for item in records} | {item.storage_ref for item in records}
        if not unique.issubset(resolved):
            raise PermissionError("artifact is unavailable in orchestration scope")
        return records

    def issue_authorization(
        self, scope: OrchestrationScope, *, worker_id: str, tool_name: str,
        parameters: dict[str, Any], ticket_scope: set[str] | None = None,
        ttl_seconds: int = 300, now: datetime | None = None,
        resource_version: str = "current",
    ) -> AuthorizationTicket:
        self.get_run(scope)
        worker = next((item for item in self.list_workers(scope)
                       if item.spec.get("worker_id") == worker_id), None)
        if worker is None:
            raise LookupError("worker not found in orchestration scope")
        if tool_name not in set(worker.spec.get("allowed_tools") or []):
            raise PermissionError("tool is not granted to this worker")
        ticket = issue_ticket(
            scope.run_id, worker_id, tool_name, parameters,
            scope=ticket_scope, ttl_seconds=ttl_seconds, now=now,
        )
        persisted_scope = [*sorted(ticket.scope), f"_nonce:{ticket.nonce}",
                           f"_resource:{resource_version}"]
        record = ToolAuthorizationRecord(
            ticket_id=ticket.ticket_id, workspace_id=scope.workspace_id,
            user_id=scope.user_id, agent_id=scope.agent_id, run_id=scope.run_id,
            worker_id=worker_id, tool_name=tool_name,
            parameter_digest=ticket.parameter_digest, scope=persisted_scope,
            status="issued", expires_at=ticket.expires_at,
        )
        self.db.add(record)
        self.append_audit(
            scope, action="tool.authorization.issued", decision="pending",
            details={"ticket_id": ticket.ticket_id, "tool_name": tool_name,
                     "scope": sorted(ticket.scope), "expires_at": ticket.expires_at.isoformat()},
            worker_id=worker_id, commit=False,
        )
        self.db.commit()
        return ticket

    def authorize_tool(
        self,
        scope: OrchestrationScope,
        *,
        worker_id: str,
        tool_name: str,
        parameters: dict[str, Any],
        policy: ToolPolicy,
        user_grant: set[str],
        orchestrator_grant: set[str],
        worker_grant: set[str],
        current_authorization: set[str],
        ticket_id: str | None = None,
        ticket_nonce: str | None = None,
        resource_version: str = "current",
        now: datetime | None = None,
    ) -> tuple[bool, str]:
        policy.validate()
        self.get_run(scope)
        if tool_name not in effective_tools(
            user_grant, orchestrator_grant, worker_grant, current_authorization
        ):
            return self._deny(scope, worker_id, tool_name, "tool_not_in_permission_intersection")
        if policy.mode == "deny":
            return self._deny(scope, worker_id, tool_name, "tool_policy_denied")
        if policy.mode == "auto" or (policy.mode == "conditional" and not policy.required_scope):
            reason = "authorized_automatically" if policy.mode == "auto" else "authorized_conditionally"
            self.append_audit(
                scope, action="tool.authorization.checked", decision="allowed",
                details={"tool_name": tool_name, "reason": reason}, worker_id=worker_id,
            )
            return True, reason
        if not ticket_id:
            return self._deny(scope, worker_id, tool_name, "confirmation_required")

        current = now or governance_now()
        record = self.db.scalar(select(ToolAuthorizationRecord).where(
            ToolAuthorizationRecord.ticket_id == ticket_id,
            ToolAuthorizationRecord.workspace_id == scope.workspace_id,
            ToolAuthorizationRecord.user_id == scope.user_id,
            ToolAuthorizationRecord.agent_id == scope.agent_id,
            ToolAuthorizationRecord.run_id == scope.run_id,
            ToolAuthorizationRecord.worker_id == worker_id,
            ToolAuthorizationRecord.tool_name == tool_name,
        ))
        if record is None:
            return self._deny(scope, worker_id, tool_name, "ticket_scope_mismatch")
        if record.status != "issued":
            return self._deny(scope, worker_id, tool_name, "replayed_ticket")
        if _aware(record.expires_at) <= _aware(current):
            record.status = "expired"
            self.db.commit()
            return self._deny(scope, worker_id, tool_name, "ticket_expired")
        if record.parameter_digest != parameter_hash(parameters):
            return self._deny(scope, worker_id, tool_name, "parameters_changed")
        persisted_scope = set(record.scope or [])
        if ticket_nonce is not None and f"_nonce:{ticket_nonce}" not in persisted_scope:
            return self._deny(scope, worker_id, tool_name, "ticket_nonce_mismatch")
        if f"_resource:{resource_version}" not in persisted_scope:
            return self._deny(scope, worker_id, tool_name, "resource_version_changed")
        if not set(policy.required_scope).issubset(persisted_scope):
            return self._deny(scope, worker_id, tool_name, "ticket_scope_mismatch")

        # Conditional update is the anti-replay boundary: only one concurrent
        # consumer can transition issued -> consumed.
        consumed_at = governance_now()
        outcome = self.db.execute(update(ToolAuthorizationRecord).where(
            ToolAuthorizationRecord.ticket_id == ticket_id,
            ToolAuthorizationRecord.status == "issued",
        ).values(status="consumed", consumed_at=consumed_at))
        if outcome.rowcount != 1:
            self.db.rollback()
            return self._deny(scope, worker_id, tool_name, "replayed_ticket")
        self.append_audit(
            scope, action="tool.authorization.consumed", decision="allowed",
            details={"ticket_id": ticket_id, "tool_name": tool_name},
            worker_id=worker_id, commit=False,
        )
        self.db.commit()
        return True, "authorized"

    def _deny(
        self, scope: OrchestrationScope, worker_id: str, tool_name: str, reason: str,
    ) -> tuple[bool, str]:
        self.append_audit(
            scope, action="tool.authorization.checked", decision="denied",
            details={"tool_name": tool_name, "reason": reason}, worker_id=worker_id,
        )
        return False, reason
