"""Ledger-first, bitemporal semantic memory service.

All public reads require an exact workspace/user/agent scope.  Memory values
are returned with provenance and must be treated as untrusted model context.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
from typing import Any, Sequence

from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .governance_models import (
    EpisodicEvidence,
    LedgerEvent,
    MemoryCheckpoint,
    ProceduralCandidate,
    SemanticFact,
    UserProfileCard,
    governance_now,
    governance_uid,
)


STATE_KINDS = frozenset({"session", "working"})
CANDIDATE_TYPES = frozenset({"procedure", "skill", "agent"})
MEMORY_SENSITIVITIES = frozenset({"public", "internal", "confidential", "restricted"})
MEMORY_CONSENT_STATUSES = frozenset({"unknown", "granted", "denied", "revoked"})


class WorkingStateConflict(RuntimeError):
    """Raised when a caller attempts to overwrite a newer projection."""


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


@dataclass(frozen=True)
class MemoryRetrievalPolicy:
    """Caller authority applied after temporal and tenant isolation."""

    allowed_sensitivities: frozenset[str] = frozenset({"public", "internal"})
    allow_sensitive: bool = False
    enforce_consent: bool = True
    require_granted_consent_for: frozenset[str] = frozenset({"confidential", "restricted"})

    def exclusion_reason(self, fact: SemanticFact) -> str | None:
        if self.enforce_consent and fact.consent_status in {"denied", "revoked"}:
            return "consent_denied_or_revoked"
        if fact.sensitivity not in self.allowed_sensitivities:
            return "sensitivity_not_authorized"
        if fact.is_sensitive and not self.allow_sensitive:
            return "sensitive_memory_not_authorized"
        if self.enforce_consent and fact.sensitivity in self.require_granted_consent_for and fact.consent_status != "granted":
            return "explicit_consent_required"
        return None


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

    Idempotency is scoped to the exact tenant/user/agent boundary. Reusing a key
    in another tenant creates an independent event and can never return the
    original tenant's payload.
    """
    if not event_type or not idempotency_key:
        raise ValueError("event_type and idempotency_key are required")
    existing = db.scalar(
        select(LedgerEvent).where(
            LedgerEvent.workspace_id == scope.workspace_id,
            LedgerEvent.user_id == scope.user_id,
            LedgerEvent.agent_id == scope.agent_id,
            LedgerEvent.idempotency_key == idempotency_key,
        )
    )
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
    try:
        with db.begin_nested():
            db.add(event)
            db.flush()
    except IntegrityError:
        existing = db.scalar(
            select(LedgerEvent).where(
                LedgerEvent.workspace_id == scope.workspace_id,
                LedgerEvent.user_id == scope.user_id,
                LedgerEvent.agent_id == scope.agent_id,
                LedgerEvent.idempotency_key == idempotency_key,
            )
        )
        if existing is None:
            raise
        return existing, False
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
    embedding: Sequence[float] | str | None = None,
    embedding_model: str = "",
    recorded_at: datetime | None = None,
) -> tuple[SemanticFact, bool]:
    """Record a semantic fact and its immutable source event atomically."""
    if not subject or not predicate:
        raise ValueError("subject and predicate are required")
    if not 0 <= confidence <= 1:
        raise ValueError("confidence must be between 0 and 1")
    if sensitivity not in MEMORY_SENSITIVITIES:
        raise ValueError("invalid memory sensitivity")
    if consent_status not in MEMORY_CONSENT_STATUSES:
        raise ValueError("invalid memory consent status")
    recorded = _utc(recorded_at)
    stored_embedding: Sequence[float] | str | None = embedding
    if embedding is not None and db.bind is not None and db.bind.dialect.name == "sqlite" and not isinstance(embedding, str):
        stored_embedding = json.dumps([float(value) for value in embedding])
    fact_valid_from = _utc(valid_from) if valid_from else recorded
    fact_valid_to = _utc(valid_to) if valid_to else None
    if fact_valid_to is not None and fact_valid_to <= fact_valid_from:
        raise ValueError("valid_to must be later than valid_from")
    existing_event = db.scalar(
        select(LedgerEvent).where(
            LedgerEvent.workspace_id == scope.workspace_id,
            LedgerEvent.user_id == scope.user_id,
            LedgerEvent.agent_id == scope.agent_id,
            LedgerEvent.idempotency_key == idempotency_key,
        )
    )
    if existing_event is not None:
        fact = db.scalar(select(SemanticFact).where(SemanticFact.source_event_id == existing_event.event_id))
        if fact is None:
            raise ValueError("idempotency key belongs to a non-fact ledger event")
        return fact, False

    prior: SemanticFact | None = None
    if supersedes_fact_id:
        prior = db.scalar(
            select(SemanticFact).where(
                SemanticFact.fact_id == supersedes_fact_id,
                SemanticFact.workspace_id == scope.workspace_id,
                SemanticFact.user_id == scope.user_id,
                SemanticFact.agent_id == scope.agent_id,
                SemanticFact.tombstoned_at.is_(None),
                SemanticFact.transaction_to.is_(None),
            )
        )
        if prior is None:
            raise LookupError("superseded fact not found in the supplied memory scope")
        if prior.subject != subject or prior.predicate != predicate:
            raise ValueError("a correction must supersede the same subject and predicate")
        if recorded < _utc(prior.transaction_from):
            raise ValueError("recorded_at cannot precede the superseded transaction version")

    fact_id = governance_uid()
    event, event_created = append_event(
        db,
        scope=scope,
        event_type="semantic_fact.recorded",
        payload={"fact_id": fact_id, "subject": subject, "predicate": predicate},
        idempotency_key=idempotency_key,
        created_at=recorded,
    )
    if not event_created:
        fact = db.scalar(select(SemanticFact).where(SemanticFact.source_event_id == event.event_id))
        if fact is None:
            raise WorkingStateConflict("fact idempotency event exists without a committed projection")
        return fact, False

    if prior is not None:
        # Closing only transaction time would make the old value disappear from
        # current-knowledge queries for valid instants outside the corrected
        # interval. Preserve those non-overlapping valid-time segments as new
        # transaction versions. This is the essential distinction between
        # "what was true when" and "what the system knew when".
        prior_valid_from = _utc(prior.valid_from)
        prior_valid_to = _utc(prior.valid_to) if prior.valid_to else None
        remaining_intervals: list[tuple[datetime, datetime | None, str]] = []

        before_to = min(
            (end for end in (prior_valid_to, fact_valid_from) if end is not None),
            default=None,
        )
        if before_to is not None and prior_valid_from < before_to:
            remaining_intervals.append((prior_valid_from, before_to, "before"))

        if fact_valid_to is not None:
            after_from = max(prior_valid_from, fact_valid_to)
            if prior_valid_to is None or after_from < prior_valid_to:
                remaining_intervals.append((after_from, prior_valid_to, "after"))

        prior.transaction_to = recorded
        for segment_from, segment_to, position in remaining_intervals:
            segment_id = governance_uid()
            segment_event, _ = append_event(
                db,
                scope=scope,
                event_type="semantic_fact.valid_interval.revised",
                payload={
                    "fact_id": segment_id,
                    "supersedes_fact_id": prior.fact_id,
                    "correction_fact_id": fact_id,
                    "position": position,
                },
                idempotency_key=f"__bitemporal__:{event.event_id}:{position}",
                created_at=recorded,
            )
            db.add(
                SemanticFact(
                    fact_id=segment_id,
                    workspace_id=scope.workspace_id,
                    user_id=scope.user_id,
                    agent_id=scope.agent_id,
                    run_id=scope.run_id,
                    worker_id=scope.worker_id,
                    source_event_id=segment_event.event_id,
                    supersedes_fact_id=prior.fact_id,
                    subject=prior.subject,
                    predicate=prior.predicate,
                    object_value=prior.object_value,
                    evidence=prior.evidence,
                    confidence=prior.confidence,
                    sensitivity=prior.sensitivity,
                    consent_status=prior.consent_status,
                    valid_from=segment_from,
                    valid_to=segment_to,
                    transaction_from=recorded,
                    expires_at=prior.expires_at,
                    is_sensitive=prior.is_sensitive,
                    embedding=prior.embedding,
                    embedding_model=prior.embedding_model,
                )
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
        valid_from=fact_valid_from,
        valid_to=fact_valid_to,
        transaction_from=recorded,
        expires_at=_utc(expires_at) if expires_at else None,
        is_sensitive=is_sensitive,
        embedding=stored_embedding,
        embedding_model=embedding_model,
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
    policy: MemoryRetrievalPolicy | None = None,
    query_vector: Sequence[float] | None = None,
    limit: int | None = None,
    minimum_similarity: float = 0.0,
) -> list[SemanticFact]:
    """Read only facts active for an exact tenant scope at two time axes.

    PostgreSQL retrieval deliberately keeps vector distance, ordering, and the
    result bound inside the database.  The Python cosine implementation is a
    portability fallback for SQLite tests only; production callers must not
    materialize the tenant's complete semantic-memory set just to rank it.
    """
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
    policy = policy or MemoryRetrievalPolicy()
    policy_filters = [
        SemanticFact.sensitivity.in_(tuple(policy.allowed_sensitivities)),
    ]
    if policy.enforce_consent:
        policy_filters.append(SemanticFact.consent_status.not_in(("denied", "revoked")))
        if policy.require_granted_consent_for:
            policy_filters.append(or_(
                SemanticFact.sensitivity.not_in(tuple(policy.require_granted_consent_for)),
                SemanticFact.consent_status == "granted",
            ))
    if not policy.allow_sensitive:
        policy_filters.append(SemanticFact.is_sensitive.is_(False))

    statement = select(SemanticFact).where(and_(*filters, *policy_filters))
    dialect_name = db.get_bind().dialect.name
    if query_vector and dialect_name == "postgresql":
        # pgvector's cosine-distance comparator compiles to ``<=>``. Applying
        # both the similarity threshold and LIMIT here is important: neither
        # ranking nor candidate-set bounding may fall back to application RAM.
        distance = SemanticFact.embedding.cosine_distance(
            [float(value) for value in query_vector]
        )
        statement = statement.where(SemanticFact.embedding.is_not(None))
        statement = statement.where(distance <= 1.0 - minimum_similarity)
        statement = statement.order_by(distance.asc(), SemanticFact.transaction_from.desc())
        if limit is not None:
            statement = statement.limit(limit)
        return list(db.scalars(statement))

    if query_vector and dialect_name != "sqlite":
        raise RuntimeError(
            "semantic vector retrieval requires PostgreSQL/pgvector; "
            "the Python cosine fallback is restricted to SQLite tests"
        )

    statement = statement.order_by(SemanticFact.transaction_from.desc())
    # A non-vector query has no application-side ranking step, so every
    # dialect (including PostgreSQL) must enforce the caller's bound in SQL.
    # SQLite vector tests are the sole exception because their JSON vectors
    # must be ranked in Python before the final slice is applied.
    if not query_vector and limit is not None:
        statement = statement.limit(limit)
    rows = list(db.scalars(statement))
    if query_vector:
        def similarity(fact: SemanticFact) -> float:
            value: Any = fact.embedding
            if value is None:
                return 0.0
            if isinstance(value, str):
                value = json.loads(value)
            if hasattr(value, "tolist"):
                value = value.tolist()
            if not isinstance(value, Sequence) or len(value) != len(query_vector):
                return 0.0
            dot = sum(float(a) * float(b) for a, b in zip(value, query_vector))
            norm = math.sqrt(sum(float(a) ** 2 for a in value)) * math.sqrt(sum(float(b) ** 2 for b in query_vector))
            return dot / norm if norm else 0.0

        # SQLite stores vectors as JSON text and has no distance operator.
        scored = [(similarity(fact), fact) for fact in rows]
        rows = [fact for score, fact in sorted(scored, key=lambda item: item[0], reverse=True) if score >= minimum_similarity]
    return rows[:limit] if limit is not None else rows


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
    existing = db.scalar(
        select(LedgerEvent).where(
            LedgerEvent.workspace_id == scope.workspace_id,
            LedgerEvent.user_id == scope.user_id,
            LedgerEvent.agent_id == scope.agent_id,
            LedgerEvent.idempotency_key == idempotency_key,
        )
    )
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
    policy: MemoryRetrievalPolicy | None = None,
    query_vector: Sequence[float] | None = None,
    limit: int | None = None,
    minimum_similarity: float = 0.0,
) -> dict[str, Any]:
    """Return auditable, explicitly untrusted context suitable for prompt injection."""
    retrieval_policy = policy or MemoryRetrievalPolicy()
    temporally_active = query_active_facts(
        db, scope=scope, as_of_valid=as_of_valid, as_of_transaction=as_of_transaction,
        policy=MemoryRetrievalPolicy(
            allowed_sensitivities=MEMORY_SENSITIVITIES,
            allow_sensitive=True,
            enforce_consent=False,
            require_granted_consent_for=frozenset(),
        ),
    )
    policy_exclusions: dict[str, int] = {}
    for fact in temporally_active:
        reason = retrieval_policy.exclusion_reason(fact)
        if reason:
            policy_exclusions[reason] = policy_exclusions.get(reason, 0) + 1
    facts = query_active_facts(
        db, scope=scope, as_of_valid=as_of_valid, as_of_transaction=as_of_transaction,
        policy=retrieval_policy, query_vector=query_vector, limit=limit,
        minimum_similarity=minimum_similarity,
    )
    profile = load_profile_card(db, scope=scope)
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
                "consent_status": fact.consent_status,
                "source_event_id": fact.source_event_id,
                "valid_from": fact.valid_from.isoformat(),
                "transaction_from": fact.transaction_from.isoformat(),
            }
            for fact in facts
        ],
        "profile_card": (
            {"version": profile.version, "fields": dict(profile.fields), "source_event_id": profile.source_event_id}
            if profile is not None and profile.tombstoned_at is None else None
        ),
        "policy": {
            "allowed_sensitivities": sorted(retrieval_policy.allowed_sensitivities),
            "allow_sensitive": retrieval_policy.allow_sensitive,
        },
        "policy_exclusions": policy_exclusions,
        "exclusions": [
            "tombstoned", "expired", "out_of_scope", "not_yet_valid",
            "not_known_at_transaction_time", "consent_denied_or_revoked",
            "sensitivity_not_authorized", "sensitive_memory_not_authorized", "explicit_consent_required",
        ],
    }


def load_profile_card(db: Session, *, scope: MemoryScope) -> UserProfileCard | None:
    """Load the structured profile view for one exact tenant/user/agent scope."""
    return db.scalar(select(UserProfileCard).where(
        UserProfileCard.workspace_id == scope.workspace_id,
        UserProfileCard.user_id == scope.user_id,
        UserProfileCard.agent_id == scope.agent_id,
    ))


def update_profile_card(
    db: Session,
    *,
    scope: MemoryScope,
    fields: dict[str, Any],
    expected_version: int,
    idempotency_key: str,
    updated_at: datetime | None = None,
) -> tuple[UserProfileCard, bool]:
    """Replace the structured card through ledger-first compare-and-swap."""
    if expected_version < 0 or not isinstance(fields, dict):
        raise ValueError("expected_version must be non-negative and fields must be an object")
    existing_event = db.scalar(select(LedgerEvent).where(
        LedgerEvent.workspace_id == scope.workspace_id,
        LedgerEvent.user_id == scope.user_id,
        LedgerEvent.agent_id == scope.agent_id,
        LedgerEvent.idempotency_key == idempotency_key,
    ))
    current = load_profile_card(db, scope=scope)
    if existing_event is not None:
        if existing_event.event_type != "memory.profile.updated" or current is None:
            raise ValueError("idempotency key belongs to another operation")
        if current.source_event_id != existing_event.event_id:
            raise WorkingStateConflict("idempotent profile update has since been superseded")
        return current, False
    current_version = current.version if current is not None else 0
    if current_version != expected_version:
        raise WorkingStateConflict(
            f"profile version mismatch: expected {expected_version}, current {current_version}"
        )
    timestamp = _utc(updated_at)
    try:
        with db.begin_nested():
            event, created = append_event(
                db,
                scope=scope,
                event_type="memory.profile.updated",
                payload={"version": current_version + 1, "fields": dict(fields)},
                idempotency_key=idempotency_key,
                created_at=timestamp,
            )
            if not created:
                raise WorkingStateConflict("profile update was completed concurrently; retry the read")
            if current is None:
                db.add(UserProfileCard(
                    card_id=governance_uid(), workspace_id=scope.workspace_id,
                    user_id=scope.user_id, agent_id=scope.agent_id,
                    version=1, fields=dict(fields), source_event_id=event.event_id,
                    tombstoned_at=None, updated_at=timestamp,
                ))
                db.flush()
            else:
                changed = db.execute(update(UserProfileCard).where(
                    UserProfileCard.card_id == current.card_id,
                    UserProfileCard.version == expected_version,
                ).values(
                    version=current_version + 1, fields=dict(fields),
                    source_event_id=event.event_id, tombstoned_at=None, updated_at=timestamp,
                ))
                if changed.rowcount != 1:
                    raise WorkingStateConflict(
                        f"profile version mismatch: expected {expected_version}"
                    )
    except IntegrityError as exc:
        raise WorkingStateConflict("profile card was created concurrently") from exc
    refreshed = load_profile_card(db, scope=scope)
    if refreshed is None:
        raise WorkingStateConflict("profile projection was not persisted")
    db.refresh(refreshed)
    return refreshed, True


def tombstone_profile_card(
    db: Session,
    *,
    scope: MemoryScope,
    expected_version: int,
    idempotency_key: str,
    tombstoned_at: datetime | None = None,
) -> tuple[UserProfileCard, bool]:
    """Delete the active profile projection without erasing its ledger history."""
    current = load_profile_card(db, scope=scope)
    existing_event = db.scalar(select(LedgerEvent).where(
        LedgerEvent.workspace_id == scope.workspace_id,
        LedgerEvent.user_id == scope.user_id,
        LedgerEvent.agent_id == scope.agent_id,
        LedgerEvent.idempotency_key == idempotency_key,
    ))
    if existing_event is not None:
        if existing_event.event_type != "memory.profile.tombstoned" or current is None:
            raise ValueError("idempotency key belongs to another operation")
        return current, False
    if current is None or current.tombstoned_at is not None:
        raise LookupError("profile card not found in supplied memory scope")
    if current.version != expected_version:
        raise WorkingStateConflict(
            f"profile version mismatch: expected {expected_version}, current {current.version}"
        )
    timestamp = _utc(tombstoned_at)
    next_version = current.version + 1
    with db.begin_nested():
        event, created = append_event(
            db,
            scope=scope,
            event_type="memory.profile.tombstoned",
            payload={"version": next_version},
            idempotency_key=idempotency_key,
            created_at=timestamp,
        )
        if not created:
            raise WorkingStateConflict("profile deletion was completed concurrently; retry the read")
        changed = db.execute(update(UserProfileCard).where(
            UserProfileCard.card_id == current.card_id,
            UserProfileCard.version == expected_version,
        ).values(
            version=next_version, fields={}, source_event_id=event.event_id,
            tombstoned_at=timestamp, updated_at=timestamp,
        ))
        if changed.rowcount != 1:
            raise WorkingStateConflict(
                f"profile version mismatch: expected {expected_version}"
            )
    refreshed = load_profile_card(db, scope=scope)
    if refreshed is None:
        raise WorkingStateConflict("profile projection was not persisted")
    db.refresh(refreshed)
    return refreshed, True


def _checkpoint_query(scope: MemoryScope, state_kind: str, state_key: str):
    if state_kind not in STATE_KINDS:
        raise ValueError("state_kind must be session or working")
    if not state_key:
        raise ValueError("state_key is required")
    return select(MemoryCheckpoint).where(
        MemoryCheckpoint.workspace_id == scope.workspace_id,
        MemoryCheckpoint.user_id == scope.user_id,
        MemoryCheckpoint.agent_id == scope.agent_id,
        MemoryCheckpoint.scope_key == f"{scope.run_id or '-'}:{scope.worker_id or '-'}",
        MemoryCheckpoint.state_kind == state_kind,
        MemoryCheckpoint.state_key == state_key,
    )


def load_checkpoint(
    db: Session, *, scope: MemoryScope, state_kind: str, state_key: str
) -> MemoryCheckpoint | None:
    """Load an exact-scope checkpoint; worker state is invisible to siblings."""
    return db.scalar(_checkpoint_query(scope, state_kind, state_key))


def checkpoint_state(
    db: Session,
    *,
    scope: MemoryScope,
    state_kind: str,
    state_key: str,
    snapshot: dict[str, Any],
    expected_version: int,
    idempotency_key: str,
    updated_at: datetime | None = None,
) -> tuple[MemoryCheckpoint, bool]:
    """Create/update a projection with compare-and-swap version semantics."""
    if expected_version < 0:
        raise ValueError("expected_version must be non-negative")
    existing_event = db.scalar(
        select(LedgerEvent).where(
            LedgerEvent.workspace_id == scope.workspace_id,
            LedgerEvent.user_id == scope.user_id,
            LedgerEvent.agent_id == scope.agent_id,
            LedgerEvent.idempotency_key == idempotency_key,
        )
    )
    if existing_event is not None:
        if existing_event.event_type != f"memory.{state_kind}.checkpointed":
            raise ValueError("idempotency key belongs to another operation")
        existing = load_checkpoint(db, scope=scope, state_kind=state_kind, state_key=state_key)
        if existing is None or existing.source_event_id != existing_event.event_id:
            raise WorkingStateConflict("idempotent checkpoint has since been superseded")
        return existing, False

    current = load_checkpoint(db, scope=scope, state_kind=state_kind, state_key=state_key)
    current_version = current.version if current is not None else 0
    if current_version != expected_version:
        raise WorkingStateConflict(
            f"working state version mismatch: expected {expected_version}, current {current_version}"
        )

    timestamp = _utc(updated_at)
    next_version = current_version + 1
    scope_key = f"{scope.run_id or '-'}:{scope.worker_id or '-'}"
    try:
        with db.begin_nested():
            event, created = append_event(
                db,
                scope=scope,
                event_type=f"memory.{state_kind}.checkpointed",
                payload={
                    "state_kind": state_kind,
                    "state_key": state_key,
                    "version": next_version,
                    "snapshot": snapshot,
                },
                idempotency_key=idempotency_key,
                created_at=timestamp,
            )
            if not created:
                existing = load_checkpoint(db, scope=scope, state_kind=state_kind, state_key=state_key)
                if existing is None or existing.source_event_id != event.event_id:
                    raise WorkingStateConflict("idempotent checkpoint has since been superseded")
                return existing, False
            if current is None:
                db.add(MemoryCheckpoint(
                    checkpoint_id=governance_uid(),
                    workspace_id=scope.workspace_id,
                    user_id=scope.user_id,
                    agent_id=scope.agent_id,
                    run_id=scope.run_id,
                    worker_id=scope.worker_id,
                    scope_key=scope_key,
                    state_kind=state_kind,
                    state_key=state_key,
                    version=next_version,
                    snapshot=dict(snapshot),
                    source_event_id=event.event_id,
                    updated_at=timestamp,
                ))
                db.flush()
            else:
                changed = db.execute(
                    update(MemoryCheckpoint).where(
                        MemoryCheckpoint.checkpoint_id == current.checkpoint_id,
                        MemoryCheckpoint.version == expected_version,
                    ).values(
                        version=next_version, snapshot=dict(snapshot),
                        source_event_id=event.event_id, updated_at=timestamp,
                    )
                )
                if changed.rowcount != 1:
                    raise WorkingStateConflict(
                        f"working state version mismatch: expected {expected_version}"
                    )
    except IntegrityError as exc:
        raise WorkingStateConflict("working state was created concurrently") from exc
    refreshed = load_checkpoint(db, scope=scope, state_kind=state_kind, state_key=state_key)
    if refreshed is None:
        raise WorkingStateConflict("checkpoint projection was not persisted")
    db.refresh(refreshed)
    return refreshed, True


def rebuild_checkpoint(
    db: Session, *, scope: MemoryScope, state_kind: str, state_key: str
) -> dict[str, Any] | None:
    """Rebuild a checkpoint from its append-only ledger events."""
    event_type = f"memory.{state_kind}.checkpointed"
    events = list(
        db.scalars(
            select(LedgerEvent).where(
                LedgerEvent.workspace_id == scope.workspace_id,
                LedgerEvent.user_id == scope.user_id,
                LedgerEvent.agent_id == scope.agent_id,
                LedgerEvent.run_id.is_(None) if scope.run_id is None else LedgerEvent.run_id == scope.run_id,
                LedgerEvent.worker_id.is_(None) if scope.worker_id is None else LedgerEvent.worker_id == scope.worker_id,
                LedgerEvent.event_type == event_type,
            ).order_by(LedgerEvent.created_at, LedgerEvent.event_id)
        )
    )
    matching = [event.payload for event in events if event.payload.get("state_key") == state_key]
    if not matching:
        return None
    latest = max(matching, key=lambda payload: int(payload.get("version", 0)))
    return {"version": int(latest["version"]), "snapshot": dict(latest.get("snapshot", {}))}


def record_episodic_evidence(
    db: Session,
    *,
    scope: MemoryScope,
    task_type: str,
    outcome: str,
    trajectory: dict[str, Any],
    metrics: dict[str, Any],
    idempotency_key: str,
    redaction_status: str = "pending",
) -> tuple[EpisodicEvidence, bool]:
    """Persist trajectory evidence; pending redaction evidence cannot seed publish."""
    if not task_type or outcome not in {"succeeded", "failed", "partial"}:
        raise ValueError("task_type and a valid outcome are required")
    if redaction_status not in {"pending", "redacted", "rejected"}:
        raise ValueError("invalid redaction_status")
    event, created = append_event(
        db,
        scope=scope,
        event_type="memory.episodic_evidence.recorded",
        payload={"task_type": task_type, "outcome": outcome},
        idempotency_key=idempotency_key,
    )
    if not created and event.event_type != "memory.episodic_evidence.recorded":
        raise ValueError("idempotency key belongs to another operation")
    existing = db.scalar(select(EpisodicEvidence).where(EpisodicEvidence.source_event_id == event.event_id))
    if existing is not None:
        return existing, False
    evidence = EpisodicEvidence(
        evidence_id=governance_uid(),
        workspace_id=scope.workspace_id,
        user_id=scope.user_id,
        agent_id=scope.agent_id,
        run_id=scope.run_id,
        worker_id=scope.worker_id,
        source_event_id=event.event_id,
        task_type=task_type,
        outcome=outcome,
        trajectory=dict(trajectory),
        metrics=dict(metrics),
        redaction_status=redaction_status,
    )
    db.add(evidence)
    db.flush()
    return evidence, created


def propose_procedural_candidate(
    db: Session,
    *,
    scope: MemoryScope,
    candidate_type: str,
    name: str,
    content: dict[str, Any],
    evidence_ids: list[str],
    idempotency_key: str,
    allow_cross_run_evidence: bool = False,
) -> tuple[ProceduralCandidate, bool]:
    """Create a non-public candidate backed only by successful redacted evidence."""
    if candidate_type not in CANDIDATE_TYPES or not name or not evidence_ids:
        raise ValueError("valid candidate_type, name, and evidence_ids are required")
    filters = [
        EpisodicEvidence.evidence_id.in_(evidence_ids),
        EpisodicEvidence.workspace_id == scope.workspace_id,
        EpisodicEvidence.user_id == scope.user_id,
        EpisodicEvidence.agent_id == scope.agent_id,
    ]
    # Direct candidate proposals are bound to one execution. The autonomous
    # consolidation service is the sole caller allowed to learn across runs,
    # while retaining the exact workspace/user/Agent boundary.
    if not allow_cross_run_evidence:
        filters.extend([
            EpisodicEvidence.run_id.is_(None)
            if scope.run_id is None else EpisodicEvidence.run_id == scope.run_id,
            EpisodicEvidence.worker_id.is_(None)
            if scope.worker_id is None else EpisodicEvidence.worker_id == scope.worker_id,
        ])
    evidence = list(db.scalars(select(EpisodicEvidence).where(*filters)))
    if len(evidence) != len(set(evidence_ids)):
        raise LookupError("candidate evidence not found in supplied memory scope")
    if any(item.outcome != "succeeded" or item.redaction_status != "redacted" for item in evidence):
        raise ValueError("candidate evidence must be successful and redacted")
    event, created = append_event(
        db,
        scope=scope,
        event_type="memory.procedural_candidate.proposed",
        payload={
            "candidate_type": candidate_type,
            "name": name,
            "evidence_ids": evidence_ids,
            # This server-authored ledger marker is the authority for later
            # cross-run replay. Client-created candidates never receive it.
            "evidence_scope": "agent_history" if allow_cross_run_evidence else "run",
        },
        idempotency_key=idempotency_key,
    )
    if not created and event.event_type != "memory.procedural_candidate.proposed":
        raise ValueError("idempotency key belongs to another operation")
    existing = db.scalar(select(ProceduralCandidate).where(ProceduralCandidate.source_event_id == event.event_id))
    if existing is not None:
        return existing, False
    candidate = ProceduralCandidate(
        candidate_id=governance_uid(),
        workspace_id=scope.workspace_id,
        user_id=scope.user_id,
        agent_id=scope.agent_id,
        run_id=scope.run_id,
        worker_id=scope.worker_id,
        source_event_id=event.event_id,
        candidate_type=candidate_type,
        name=name,
        content=dict(content),
        evidence_ids=list(dict.fromkeys(evidence_ids)),
        status="candidate",
    )
    db.add(candidate)
    db.flush()
    return candidate, created


def transition_procedural_candidate(
    db: Session,
    *,
    scope: MemoryScope,
    candidate_id: str,
    action: str,
    actor_id: str,
    idempotency_key: str,
    evaluation: dict[str, Any] | None = None,
    asset_version: str | None = None,
) -> tuple[ProceduralCandidate, bool]:
    """Apply evaluated -> approved -> published lifecycle, or rollback/reject."""
    candidate = db.scalar(
        select(ProceduralCandidate).where(
            ProceduralCandidate.candidate_id == candidate_id,
            ProceduralCandidate.workspace_id == scope.workspace_id,
            ProceduralCandidate.user_id == scope.user_id,
            ProceduralCandidate.agent_id == scope.agent_id,
            ProceduralCandidate.run_id.is_(None)
            if scope.run_id is None else ProceduralCandidate.run_id == scope.run_id,
            ProceduralCandidate.worker_id.is_(None)
            if scope.worker_id is None else ProceduralCandidate.worker_id == scope.worker_id,
        )
    )
    if candidate is None:
        raise LookupError("candidate not found in supplied memory scope")
    transitions = {
        "evaluate": ({"candidate"}, "evaluated"),
        "approve": ({"evaluated"}, "approved"),
        "publish": ({"approved"}, "published"),
        "reject": ({"candidate", "evaluated"}, "rejected"),
        "rollback": ({"published"}, "rolled_back"),
    }
    if action not in transitions:
        raise ValueError("invalid candidate lifecycle action")
    existing = db.scalar(
        select(LedgerEvent).where(
            LedgerEvent.workspace_id == scope.workspace_id,
            LedgerEvent.user_id == scope.user_id,
            LedgerEvent.agent_id == scope.agent_id,
            LedgerEvent.idempotency_key == idempotency_key,
        )
    )
    if existing is not None:
        if existing.payload.get("candidate_id") != candidate_id or existing.payload.get("action") != action:
            raise ValueError("idempotency key belongs to another operation")
        return candidate, False
    allowed, target = transitions[action]
    if candidate.status not in allowed:
        raise ValueError(f"cannot {action} candidate from {candidate.status}")
    if action == "evaluate" and not evaluation:
        raise ValueError("evaluation evidence is required")
    if action == "publish" and not asset_version:
        raise ValueError("versioned publish requires asset_version")
    timestamp = governance_now()
    append_event(
        db,
        scope=scope,
        event_type=f"memory.procedural_candidate.{target}",
        payload={"candidate_id": candidate_id, "action": action, "actor_id": actor_id},
        idempotency_key=idempotency_key,
        created_at=timestamp,
    )
    candidate.status = target
    candidate.updated_at = timestamp
    if evaluation:
        candidate.evaluation = dict(evaluation)
    if action == "approve":
        candidate.approved_by = actor_id
    elif action == "publish":
        candidate.asset_version = asset_version
        candidate.published_at = timestamp
    elif action == "rollback":
        candidate.rolled_back_at = timestamp
    db.flush()
    return candidate, True
