"""Ledger-first, bitemporal semantic memory service.

All public reads require an exact workspace/user/agent scope.  Memory values
are returned with provenance and must be treated as untrusted model context.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from .governance_models import LedgerEvent, SemanticFact, governance_now, governance_uid


@dataclass(frozen=True)
class MemoryScope:
    workspace_id: str
    user_id: str
    agent_id: str
    run_id: str | None = None
    worker_id: str | None = None

    def __post_init__(self) -> None:
        if not all((self.workspace_id, self.user_id, self.agent_id)):
            raise ValueError("workspace_id, user_id, and agent_id are required")


def _utc(value: datetime | None) -> datetime:
    if value is None:
        return governance_now()
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def append_event(
    db: Session,
    *,
    scope: MemoryScope,
    event_type: str,
    payload: dict[str, Any],
    idempotency_key: str,
    created_at: datetime | None = None,
) -> tuple[LedgerEvent, bool]:
    """Append an event once; returns ``(event, was_created)``.

    The idempotency key is global deliberately: accepting a key in a different
    tenant would turn an accidental retry into a cross-tenant write ambiguity.
    """
    if not event_type or not idempotency_key:
        raise ValueError("event_type and idempotency_key are required")
    existing = db.scalar(select(LedgerEvent).where(LedgerEvent.idempotency_key == idempotency_key))
    if existing is not None:
        return existing, False
    event = LedgerEvent(
        event_id=governance_uid(),
        idempotency_key=idempotency_key,
        workspace_id=scope.workspace_id,
        user_id=scope.user_id,
        agent_id=scope.agent_id,
        run_id=scope.run_id,
        worker_id=scope.worker_id,
        event_type=event_type,
        payload=payload,
        created_at=_utc(created_at),
    )
    db.add(event)
    db.flush()
    return event, True


def record_fact(
    db: Session,
    *,
    scope: MemoryScope,
    subject: str,
    predicate: str,
    object_value: str,
    idempotency_key: str,
    valid_from: datetime | None = None,
    valid_to: datetime | None = None,
    expires_at: datetime | None = None,
    supersedes_fact_id: str | None = None,
    evidence: str = "",
    confidence: float = 0.5,
    sensitivity: str = "internal",
    consent_status: str = "unknown",
    is_sensitive: bool = False,
    recorded_at: datetime | None = None,
) -> tuple[SemanticFact, bool]:
    """Record a semantic fact and its immutable source event atomically."""
    if not subject or not predicate:
        raise ValueError("subject and predicate are required")
    if not 0 <= confidence <= 1:
        raise ValueError("confidence must be between 0 and 1")
    recorded = _utc(recorded_at)
    existing_event = db.scalar(select(LedgerEvent).where(LedgerEvent.idempotency_key == idempotency_key))
    if existing_event is not None:
        fact = db.scalar(select(SemanticFact).where(SemanticFact.source_event_id == existing_event.event_id))
        if fact is None:
            raise ValueError("idempotency key belongs to a non-fact ledger event")
        return fact, False

    if supersedes_fact_id:
        prior = db.scalar(
            select(SemanticFact).where(
                SemanticFact.fact_id == supersedes_fact_id,
                SemanticFact.workspace_id == scope.workspace_id,
                SemanticFact.user_id == scope.user_id,
                SemanticFact.agent_id == scope.agent_id,
                SemanticFact.tombstoned_at.is_(None),
            )
        )
        if prior is None:
            raise LookupError("superseded fact not found in the supplied memory scope")
        # The correction becomes known now; the prior fact remains available to
        # historical transaction-time queries but no longer appears as current.
        prior.transaction_to = recorded

    fact_id = governance_uid()
    event, _ = append_event(
        db,
        scope=scope,
        event_type="semantic_fact.recorded",
        payload={"fact_id": fact_id, "subject": subject, "predicate": predicate},
        idempotency_key=idempotency_key,
        created_at=recorded,
    )
    fact = SemanticFact(
        fact_id=fact_id,
        workspace_id=scope.workspace_id,
        user_id=scope.user_id,
        agent_id=scope.agent_id,
        run_id=scope.run_id,
        worker_id=scope.worker_id,
        source_event_id=event.event_id,
        supersedes_fact_id=supersedes_fact_id,
        subject=subject,
        predicate=predicate,
        object_value=object_value,
        evidence=evidence,
        confidence=confidence,
        sensitivity=sensitivity,
        consent_status=consent_status,
        valid_from=_utc(valid_from) if valid_from else recorded,
        valid_to=_utc(valid_to) if valid_to else None,
        transaction_from=recorded,
        expires_at=_utc(expires_at) if expires_at else None,
        is_sensitive=is_sensitive,
    )
    db.add(fact)
    db.flush()
    return fact, True


def query_active_facts(
    db: Session,
    *,
    scope: MemoryScope,
    as_of_valid: datetime | None = None,
    as_of_transaction: datetime | None = None,
) -> list[SemanticFact]:
    """Read only facts active for an exact tenant scope at two time axes."""
    valid_at, transaction_at = _utc(as_of_valid), _utc(as_of_transaction)
    filters = (
        SemanticFact.workspace_id == scope.workspace_id,
        SemanticFact.user_id == scope.user_id,
        SemanticFact.agent_id == scope.agent_id,
        SemanticFact.valid_from <= valid_at,
        or_(SemanticFact.valid_to.is_(None), SemanticFact.valid_to > valid_at),
        or_(SemanticFact.expires_at.is_(None), SemanticFact.expires_at > valid_at),
        SemanticFact.transaction_from <= transaction_at,
        or_(SemanticFact.transaction_to.is_(None), SemanticFact.transaction_to > transaction_at),
        or_(SemanticFact.tombstoned_at.is_(None), SemanticFact.tombstoned_at > transaction_at),
    )
    return list(db.scalars(select(SemanticFact).where(and_(*filters)).order_by(SemanticFact.transaction_from.desc())))


def tombstone_fact(
    db: Session,
    *,
    scope: MemoryScope,
    fact_id: str,
    idempotency_key: str,
    reason: str = "",
    tombstoned_at: datetime | None = None,
) -> tuple[SemanticFact, bool]:
    """Soft-delete a fact via an append-only tombstone event."""
    fact = db.scalar(
        select(SemanticFact).where(
            SemanticFact.fact_id == fact_id,
            SemanticFact.workspace_id == scope.workspace_id,
            SemanticFact.user_id == scope.user_id,
            SemanticFact.agent_id == scope.agent_id,
        )
    )
    if fact is None:
        raise LookupError("fact not found in the supplied memory scope")
    existing = db.scalar(select(LedgerEvent).where(LedgerEvent.idempotency_key == idempotency_key))
    if existing is not None:
        if existing.event_type != "semantic_fact.tombstoned" or existing.payload.get("fact_id") != fact_id:
            raise ValueError("idempotency key belongs to another operation")
        return fact, False

    timestamp = _utc(tombstoned_at)
    event, _ = append_event(
        db,
        scope=scope,
        event_type="semantic_fact.tombstoned",
        payload={"fact_id": fact_id, "reason": reason},
        idempotency_key=idempotency_key,
        created_at=timestamp,
    )
    fact.tombstone_event_id = event.event_id
    fact.tombstoned_at = timestamp
    fact.transaction_to = timestamp
    db.flush()
    return fact, True


def explain_memory_retrieval(
    db: Session,
    *,
    scope: MemoryScope,
    as_of_valid: datetime | None = None,
    as_of_transaction: datetime | None = None,
) -> dict[str, Any]:
    """Return auditable, explicitly untrusted context suitable for prompt injection."""
    facts = query_active_facts(
        db, scope=scope, as_of_valid=as_of_valid, as_of_transaction=as_of_transaction
    )
    return {
        "scope": {"workspace_id": scope.workspace_id, "user_id": scope.user_id, "agent_id": scope.agent_id},
        "untrusted_context": True,
        "retrieved_at": governance_now().isoformat(),
        "facts": [
            {
                "fact_id": fact.fact_id,
                "statement": f"{fact.subject} {fact.predicate} {fact.object_value}",
                "evidence": fact.evidence,
                "confidence": fact.confidence,
                "sensitivity": fact.sensitivity,
                "source_event_id": fact.source_event_id,
                "valid_from": fact.valid_from.isoformat(),
                "transaction_from": fact.transaction_from.isoformat(),
            }
            for fact in facts
        ],
        "exclusions": ["tombstoned", "expired", "out_of_scope", "not_yet_valid", "not_known_at_transaction_time"],
    }
