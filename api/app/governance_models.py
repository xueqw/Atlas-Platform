"""Independent persistence models for the memory governance foundation.

The main application currently owns its own SQLAlchemy metadata.  These models
intentionally use a separate metadata registry so they can be introduced and
migrated without changing the existing application model module.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from pgvector.sqlalchemy import Vector


def governance_uid() -> str:
    return str(uuid.uuid4())


def governance_now() -> datetime:
    return datetime.now(timezone.utc)


class GovernanceBase(DeclarativeBase):
    """Metadata for the append-only memory governance tables."""


class LedgerEvent(GovernanceBase):
    """Immutable record of a memory-affecting decision or state transition."""

    __tablename__ = "memory_ledger_events"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "user_id", "agent_id", "idempotency_key",
            name="uq_memory_ledger_scope_idempotency",
        ),
    )

    event_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=governance_uid)
    idempotency_key: Mapped[str] = mapped_column(String(160), index=True)
    workspace_id: Mapped[str] = mapped_column(String(36), index=True)
    user_id: Mapped[str] = mapped_column(String(36), index=True)
    agent_id: Mapped[str] = mapped_column(String(36), index=True)
    run_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    worker_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    event_type: Mapped[str] = mapped_column(String(80), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=governance_now, index=True)


class SemanticFact(GovernanceBase):
    """Bitemporal memory fact.  Revisions form a chain via ``supersedes_fact_id``."""

    __tablename__ = "memory_semantic_facts"

    fact_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=governance_uid)
    workspace_id: Mapped[str] = mapped_column(String(36), index=True)
    user_id: Mapped[str] = mapped_column(String(36), index=True)
    agent_id: Mapped[str] = mapped_column(String(36), index=True)
    run_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    worker_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    source_event_id: Mapped[str] = mapped_column(ForeignKey("memory_ledger_events.event_id"), unique=True, index=True)
    supersedes_fact_id: Mapped[str | None] = mapped_column(ForeignKey("memory_semantic_facts.fact_id"), nullable=True, index=True)
    subject: Mapped[str] = mapped_column(String(240), index=True)
    predicate: Mapped[str] = mapped_column(String(160), index=True)
    object_value: Mapped[str] = mapped_column(Text)
    evidence: Mapped[str] = mapped_column(Text, default="")
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    sensitivity: Mapped[str] = mapped_column(String(24), default="internal")
    consent_status: Mapped[str] = mapped_column(String(24), default="unknown")
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    transaction_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=governance_now, index=True)
    transaction_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    tombstone_event_id: Mapped[str | None] = mapped_column(ForeignKey("memory_ledger_events.event_id"), nullable=True, unique=True)
    tombstoned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    is_sensitive: Mapped[bool] = mapped_column(Boolean, default=False)
    embedding: Mapped[list[float] | str | None] = mapped_column(Vector(1024).with_variant(Text(), "sqlite"), nullable=True)
    embedding_model: Mapped[str] = mapped_column(String(160), default="")


class MemoryCheckpoint(GovernanceBase):
    """Durable session or task-working projection with optimistic versioning."""

    __tablename__ = "memory_checkpoints"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "user_id", "agent_id", "scope_key",
            "state_kind", "state_key", name="uq_memory_checkpoint_scope_key_v2",
        ),
    )

    checkpoint_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=governance_uid)
    workspace_id: Mapped[str] = mapped_column(String(36), index=True)
    user_id: Mapped[str] = mapped_column(String(36), index=True)
    agent_id: Mapped[str] = mapped_column(String(36), index=True)
    run_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    worker_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    scope_key: Mapped[str] = mapped_column(String(160), default="-:-", index=True)
    state_kind: Mapped[str] = mapped_column(String(24), index=True)
    state_key: Mapped[str] = mapped_column(String(160), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    source_event_id: Mapped[str] = mapped_column(ForeignKey("memory_ledger_events.event_id"), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=governance_now, index=True)


class UserProfileCard(GovernanceBase):
    """Structured user-profile view rebuilt from scoped ledger mutations."""

    __tablename__ = "memory_user_profile_cards"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "user_id", "agent_id",
            name="uq_memory_profile_scope",
        ),
    )

    card_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=governance_uid)
    workspace_id: Mapped[str] = mapped_column(String(36), index=True)
    user_id: Mapped[str] = mapped_column(String(36), index=True)
    agent_id: Mapped[str] = mapped_column(String(36), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    fields: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    source_event_id: Mapped[str] = mapped_column(ForeignKey("memory_ledger_events.event_id"), index=True)
    tombstoned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=governance_now, index=True)


class EpisodicEvidence(GovernanceBase):
    """A redacted execution trajectory that may support a future procedure."""

    __tablename__ = "memory_episodic_evidence"

    evidence_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=governance_uid)
    workspace_id: Mapped[str] = mapped_column(String(36), index=True)
    user_id: Mapped[str] = mapped_column(String(36), index=True)
    agent_id: Mapped[str] = mapped_column(String(36), index=True)
    run_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    worker_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    source_event_id: Mapped[str] = mapped_column(ForeignKey("memory_ledger_events.event_id"), unique=True)
    task_type: Mapped[str] = mapped_column(String(160), index=True)
    outcome: Mapped[str] = mapped_column(String(32), index=True)
    trajectory: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    redaction_status: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=governance_now, index=True)


class ProceduralCandidate(GovernanceBase):
    """Candidate procedure/Skill/Agent; never selectable until explicitly published."""

    __tablename__ = "memory_procedural_candidates"

    candidate_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=governance_uid)
    workspace_id: Mapped[str] = mapped_column(String(36), index=True)
    user_id: Mapped[str] = mapped_column(String(36), index=True)
    agent_id: Mapped[str] = mapped_column(String(36), index=True)
    run_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    worker_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    source_event_id: Mapped[str] = mapped_column(ForeignKey("memory_ledger_events.event_id"), unique=True)
    candidate_type: Mapped[str] = mapped_column(String(24), index=True)
    name: Mapped[str] = mapped_column(String(240))
    content: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    evidence_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(24), default="candidate", index=True)
    evaluation: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    approved_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    asset_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rolled_back_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=governance_now, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=governance_now)


class SkillRouterDecision(GovernanceBase):
    __tablename__ = "skill_router_decisions"

    decision_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=governance_uid)
    workspace_id: Mapped[str] = mapped_column(String(36), index=True)
    user_id: Mapped[str] = mapped_column(String(36), index=True)
    agent_id: Mapped[str] = mapped_column(String(36), index=True)
    run_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    query: Mapped[str] = mapped_column(Text)
    inputs: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    recall: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    rerank: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    selected_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    decision: Mapped[str] = mapped_column(String(24), index=True)
    reasons: Mapped[list[str]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=governance_now, index=True)


class WorkerRecord(GovernanceBase):
    __tablename__ = "orchestration_workers"

    worker_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    version: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(36), index=True)
    user_id: Mapped[str] = mapped_column(String(36), index=True)
    agent_id: Mapped[str] = mapped_column(String(36), index=True)
    run_id: Mapped[str] = mapped_column(String(36), index=True)
    spec: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(24), default="created", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=governance_now)
    terminated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class TaskEnvelopeRecord(GovernanceBase):
    __tablename__ = "orchestration_task_envelopes"

    envelope_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(36), index=True)
    user_id: Mapped[str] = mapped_column(String(36), index=True)
    agent_id: Mapped[str] = mapped_column(String(36), index=True)
    run_id: Mapped[str] = mapped_column(String(36), index=True)
    worker_id: Mapped[str] = mapped_column(String(80), index=True)
    task_id: Mapped[str] = mapped_column(String(80), index=True)
    schema_version: Mapped[str] = mapped_column(String(16))
    envelope: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=governance_now)


class ResultEnvelopeRecord(GovernanceBase):
    __tablename__ = "orchestration_result_envelopes"

    envelope_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(36), index=True)
    user_id: Mapped[str] = mapped_column(String(36), index=True)
    agent_id: Mapped[str] = mapped_column(String(36), index=True)
    run_id: Mapped[str] = mapped_column(String(36), index=True)
    worker_id: Mapped[str] = mapped_column(String(80), index=True)
    task_id: Mapped[str] = mapped_column(String(80), index=True)
    schema_version: Mapped[str] = mapped_column(String(16))
    envelope: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=governance_now)


class ToolAuthorizationRecord(GovernanceBase):
    __tablename__ = "orchestration_tool_authorizations"

    ticket_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(36), index=True)
    user_id: Mapped[str] = mapped_column(String(36), index=True)
    agent_id: Mapped[str] = mapped_column(String(36), index=True)
    run_id: Mapped[str] = mapped_column(String(36), index=True)
    worker_id: Mapped[str] = mapped_column(String(80), index=True)
    tool_name: Mapped[str] = mapped_column(String(160), index=True)
    parameter_digest: Mapped[str] = mapped_column(String(64))
    scope: Mapped[list[str]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(24), default="issued", index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ArtifactRecord(GovernanceBase):
    __tablename__ = "orchestration_artifacts"

    artifact_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(36), index=True)
    user_id: Mapped[str] = mapped_column(String(36), index=True)
    agent_id: Mapped[str] = mapped_column(String(36), index=True)
    run_id: Mapped[str] = mapped_column(String(36), index=True)
    worker_id: Mapped[str] = mapped_column(String(80), index=True)
    storage_ref: Mapped[str] = mapped_column(Text)
    media_type: Mapped[str] = mapped_column(String(160), default="application/octet-stream")
    sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=governance_now)


class GovernanceAuditRecord(GovernanceBase):
    __tablename__ = "governance_audit_records"

    audit_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=governance_uid)
    workspace_id: Mapped[str] = mapped_column(String(36), index=True)
    user_id: Mapped[str] = mapped_column(String(36), index=True)
    agent_id: Mapped[str] = mapped_column(String(36), index=True)
    run_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    worker_id: Mapped[str | None] = mapped_column(String(80), index=True, nullable=True)
    action: Mapped[str] = mapped_column(String(160), index=True)
    decision: Mapped[str] = mapped_column(String(24), index=True)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=governance_now, index=True)


class OrchestrationRunRecord(GovernanceBase):
    """Durable scheduler snapshot used to resume an interrupted orchestration."""

    __tablename__ = "orchestration_runs"

    run_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(36), index=True)
    user_id: Mapped[str] = mapped_column(String(36), index=True)
    agent_id: Mapped[str] = mapped_column(String(36), index=True)
    status: Mapped[str] = mapped_column(String(24), default="created", index=True)
    scheduler_state: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    aggregate: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=governance_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=governance_now)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CandidateAssetVersionRecord(GovernanceBase):
    """Immutable published snapshot linking a candidate to a real Atlas asset."""

    __tablename__ = "governance_candidate_asset_versions"
    __table_args__ = (
        UniqueConstraint("candidate_id", "version", name="uq_candidate_asset_version"),
    )

    version_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=governance_uid)
    candidate_id: Mapped[str] = mapped_column(
        ForeignKey("memory_procedural_candidates.candidate_id"), index=True
    )
    workspace_id: Mapped[str] = mapped_column(String(36), index=True)
    asset_type: Mapped[str] = mapped_column(String(24), index=True)
    asset_id: Mapped[str] = mapped_column(String(36), index=True)
    version: Mapped[str] = mapped_column(String(64))
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    published_by: Mapped[str] = mapped_column(String(36))
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=governance_now)
    rolled_back_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
