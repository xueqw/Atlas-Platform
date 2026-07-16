"""Independent persistence models for the memory governance foundation.

The main application currently owns its own SQLAlchemy metadata.  These models
intentionally use a separate metadata registry so they can be introduced and
migrated without changing the existing application model module.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, JSON, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def governance_uid() -> str:
    return str(uuid.uuid4())


def governance_now() -> datetime:
    return datetime.now(timezone.utc)


class GovernanceBase(DeclarativeBase):
    """Metadata for the append-only memory governance tables."""


class LedgerEvent(GovernanceBase):
    """Immutable record of a memory-affecting decision or state transition."""

    __tablename__ = "memory_ledger_events"

    event_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=governance_uid)
    idempotency_key: Mapped[str] = mapped_column(String(160), unique=True, index=True)
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

