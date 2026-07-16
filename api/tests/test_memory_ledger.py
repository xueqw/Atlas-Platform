from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.governance_models import GovernanceBase, LedgerEvent
from app.memory_ledger import (
    MemoryScope,
    append_event,
    explain_memory_retrieval,
    query_active_facts,
    record_fact,
    tombstone_fact,
)


@pytest.fixture()
def db():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    GovernanceBase.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


@pytest.fixture()
def scope():
    return MemoryScope(workspace_id="ws-a", user_id="user-a", agent_id="agent-a", run_id="run-a")


def test_event_and_fact_idempotency(db, scope):
    first, created = append_event(db, scope=scope, event_type="memory.note", payload={"x": 1}, idempotency_key="event-1")
    retry, retried_created = append_event(db, scope=scope, event_type="memory.note", payload={"x": 2}, idempotency_key="event-1")
    assert created is True
    assert retried_created is False
    assert retry.event_id == first.event_id
    assert db.query(LedgerEvent).count() == 1

    fact, made = record_fact(db, scope=scope, subject="customer", predicate="prefers", object_value="email", idempotency_key="fact-1")
    retried_fact, retry_made = record_fact(db, scope=scope, subject="customer", predicate="prefers", object_value="sms", idempotency_key="fact-1")
    assert made is True
    assert retry_made is False
    assert retried_fact.fact_id == fact.fact_id


def test_bitemporal_query_respects_valid_and_transaction_time(db, scope):
    known = datetime(2026, 7, 1, tzinfo=timezone.utc)
    valid_from = known + timedelta(days=2)
    valid_to = known + timedelta(days=4)
    record_fact(
        db, scope=scope, subject="project", predicate="status", object_value="active",
        idempotency_key="bitemporal", valid_from=valid_from, valid_to=valid_to, recorded_at=known,
    )
    assert not query_active_facts(db, scope=scope, as_of_valid=known + timedelta(days=1), as_of_transaction=known)
    assert query_active_facts(db, scope=scope, as_of_valid=known + timedelta(days=3), as_of_transaction=known)
    assert not query_active_facts(db, scope=scope, as_of_valid=known + timedelta(days=5), as_of_transaction=known)
    assert not query_active_facts(db, scope=scope, as_of_valid=known + timedelta(days=3), as_of_transaction=known - timedelta(seconds=1))


def test_query_never_crosses_workspace_user_or_agent_boundary(db, scope):
    record_fact(db, scope=scope, subject="account", predicate="tier", object_value="gold", idempotency_key="tenant-a")
    for other in (
        MemoryScope(workspace_id="ws-b", user_id="user-a", agent_id="agent-a"),
        MemoryScope(workspace_id="ws-a", user_id="user-b", agent_id="agent-a"),
        MemoryScope(workspace_id="ws-a", user_id="user-a", agent_id="agent-b"),
    ):
        assert query_active_facts(db, scope=other) == []


def test_tombstone_is_append_only_and_explained(db, scope):
    at = datetime(2026, 7, 1, tzinfo=timezone.utc)
    fact, _ = record_fact(db, scope=scope, subject="customer", predicate="phone", object_value="123", idempotency_key="fact-delete", recorded_at=at)
    deleted, created = tombstone_fact(db, scope=scope, fact_id=fact.fact_id, idempotency_key="delete-1", reason="consent withdrawn", tombstoned_at=at + timedelta(days=1))
    retry, retry_created = tombstone_fact(db, scope=scope, fact_id=fact.fact_id, idempotency_key="delete-1")
    assert created is True
    assert retry_created is False
    assert retry.fact_id == deleted.fact_id
    assert fact.tombstone_event_id
    assert query_active_facts(db, scope=scope, as_of_transaction=at + timedelta(days=2)) == []
    assert query_active_facts(db, scope=scope, as_of_transaction=at + timedelta(hours=12)) == [fact]
    explanation = explain_memory_retrieval(db, scope=scope, as_of_transaction=at + timedelta(days=2))
    assert explanation["untrusted_context"] is True
    assert explanation["facts"] == []
    assert "tombstoned" in explanation["exclusions"]


def test_correction_closes_prior_transaction_version(db, scope):
    first_time = datetime(2026, 7, 1, tzinfo=timezone.utc)
    original, _ = record_fact(
        db, scope=scope, subject="customer", predicate="tier", object_value="silver",
        idempotency_key="tier-original", recorded_at=first_time,
    )
    corrected, _ = record_fact(
        db, scope=scope, subject="customer", predicate="tier", object_value="gold",
        idempotency_key="tier-corrected", supersedes_fact_id=original.fact_id,
        valid_from=first_time, recorded_at=first_time + timedelta(days=1),
    )
    current = query_active_facts(db, scope=scope, as_of_valid=first_time + timedelta(days=1), as_of_transaction=first_time + timedelta(days=2))
    historical = query_active_facts(db, scope=scope, as_of_valid=first_time + timedelta(hours=12), as_of_transaction=first_time + timedelta(hours=12))
    assert [item.fact_id for item in current] == [corrected.fact_id]
    assert [item.fact_id for item in historical] == [original.fact_id]
