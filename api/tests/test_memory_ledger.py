from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session, sessionmaker

from app.governance_models import GovernanceBase, LedgerEvent, MemoryCheckpoint
from app.memory_ledger import (
    MemoryScope,
    MemoryRetrievalPolicy,
    WorkingStateConflict,
    append_event,
    checkpoint_state,
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


def test_move_correction_preserves_valid_and_transaction_time_history(db, scope):
    """A late-reported move must not pollute old or current address history."""
    first_known = datetime(2026, 1, 1, tzinfo=timezone.utc)
    moved_at = datetime(2026, 3, 1, tzinfo=timezone.utc)
    reported_at = datetime(2026, 4, 1, tzinfo=timezone.utc)
    original, _ = record_fact(
        db,
        scope=scope,
        subject="customer",
        predicate="address",
        object_value="Hangzhou",
        idempotency_key="address-original",
        valid_from=first_known,
        recorded_at=first_known,
    )
    corrected, _ = record_fact(
        db,
        scope=scope,
        subject="customer",
        predicate="address",
        object_value="Shanghai",
        idempotency_key="address-move",
        supersedes_fact_id=original.fact_id,
        valid_from=moved_at,
        recorded_at=reported_at,
    )

    def values(valid_at, transaction_at):
        return {
            item.object_value
            for item in query_active_facts(
                db,
                scope=scope,
                as_of_valid=valid_at,
                as_of_transaction=transaction_at,
            )
            if item.subject == "customer" and item.predicate == "address"
        }

    # Before the report, the ledger truthfully reconstructs what Atlas knew.
    assert values(moved_at + timedelta(days=1), reported_at - timedelta(days=1)) == {"Hangzhou"}
    # After the report, valid time selects the old address before the move...
    assert values(moved_at - timedelta(days=1), reported_at + timedelta(days=1)) == {"Hangzhou"}
    # ...and only the new address after the move, without old/new contamination.
    assert values(moved_at + timedelta(days=1), reported_at + timedelta(days=1)) == {"Shanghai"}
    assert corrected.valid_from.replace(tzinfo=timezone.utc) == moved_at


def test_bounded_correction_preserves_both_unaffected_valid_intervals(db, scope):
    valid_start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    correction_start = datetime(2026, 2, 1, tzinfo=timezone.utc)
    correction_end = datetime(2026, 3, 1, tzinfo=timezone.utc)
    learned_at = datetime(2026, 4, 1, tzinfo=timezone.utc)
    original, _ = record_fact(
        db,
        scope=scope,
        subject="customer",
        predicate="service_region",
        object_value="east",
        idempotency_key="region-original",
        valid_from=valid_start,
        recorded_at=valid_start,
    )
    record_fact(
        db,
        scope=scope,
        subject="customer",
        predicate="service_region",
        object_value="north",
        idempotency_key="region-temporary-correction",
        supersedes_fact_id=original.fact_id,
        valid_from=correction_start,
        valid_to=correction_end,
        recorded_at=learned_at,
    )

    def current_value(valid_at):
        return [
            item.object_value
            for item in query_active_facts(
                db,
                scope=scope,
                as_of_valid=valid_at,
                as_of_transaction=learned_at + timedelta(days=1),
            )
            if item.predicate == "service_region"
        ]

    assert current_value(correction_start - timedelta(days=1)) == ["east"]
    assert current_value(correction_start + timedelta(days=1)) == ["north"]
    assert current_value(correction_end + timedelta(days=1)) == ["east"]


def test_fact_write_supersession_and_tombstone_never_cross_scope(db, scope):
    original, _ = record_fact(
        db,
        scope=scope,
        subject="customer",
        predicate="private_note",
        object_value="tenant-a",
        idempotency_key="same-fact-key",
    )
    other_workspace = MemoryScope(
        workspace_id="ws-b", user_id=scope.user_id, agent_id=scope.agent_id, run_id="run-b"
    )
    isolated, created = record_fact(
        db,
        scope=other_workspace,
        subject="customer",
        predicate="private_note",
        object_value="tenant-b",
        idempotency_key="same-fact-key",
    )
    assert created is True
    assert isolated.fact_id != original.fact_id
    assert isolated.object_value == "tenant-b"

    with pytest.raises(LookupError, match="superseded fact"):
        record_fact(
            db,
            scope=other_workspace,
            subject="customer",
            predicate="private_note",
            object_value="attempted leak",
            idempotency_key="cross-scope-correction",
            supersedes_fact_id=original.fact_id,
        )
    with pytest.raises(LookupError, match="fact not found"):
        tombstone_fact(
            db,
            scope=other_workspace,
            fact_id=original.fact_id,
            idempotency_key="cross-scope-delete",
        )


def test_retrieval_enforces_consent_sensitivity_and_persisted_vector_order(db, scope):
    record_fact(
        db, scope=scope, subject="user", predicate="topic", object_value="billing",
        idempotency_key="public-vector", sensitivity="internal", consent_status="unknown",
        embedding=[1.0, 0.0],
    )
    record_fact(
        db, scope=scope, subject="user", predicate="account", object_value="secret",
        idempotency_key="restricted-vector", sensitivity="restricted", consent_status="granted",
        is_sensitive=True, embedding=[0.0, 1.0],
    )
    record_fact(
        db, scope=scope, subject="user", predicate="health", object_value="revoked",
        idempotency_key="revoked-vector", sensitivity="internal", consent_status="revoked",
        embedding=[0.0, 1.0],
    )

    default = explain_memory_retrieval(db, scope=scope, query_vector=[0.0, 1.0])
    assert [fact["statement"] for fact in default["facts"]] == ["user topic billing"]
    assert default["policy_exclusions"] == {
        "sensitivity_not_authorized": 1,
        "consent_denied_or_revoked": 1,
    }
    privileged = query_active_facts(
        db,
        scope=scope,
        policy=MemoryRetrievalPolicy(
            allowed_sensitivities=frozenset({"public", "internal", "restricted"}),
            allow_sensitive=True,
        ),
        query_vector=[0.0, 1.0],
    )
    assert [fact.object_value for fact in privileged] == ["secret", "billing"]


def test_postgresql_semantic_retrieval_uses_pgvector_order_and_limit(scope):
    class PostgreSQLCapture:
        statement = None

        class Bind:
            class Dialect:
                name = "postgresql"
            dialect = Dialect()

        def get_bind(self):
            return self.Bind()

        def scalars(self, statement):
            self.statement = statement
            return []

    capture = PostgreSQLCapture()
    assert query_active_facts(
        capture,
        scope=scope,
        policy=MemoryRetrievalPolicy(
            allowed_sensitivities=frozenset({"internal"}),
            allow_sensitive=False,
        ),
        query_vector=[1.0] * 1024,
        minimum_similarity=0.72,
        limit=7,
    ) == []
    sql = str(capture.statement.compile(dialect=postgresql.dialect()))
    assert "<=>" in sql
    assert "ORDER BY (memory_semantic_facts.embedding <=>" in sql
    assert "LIMIT" in sql
    assert "memory_semantic_facts.workspace_id" in sql
    assert "memory_semantic_facts.user_id" in sql
    assert "memory_semantic_facts.agent_id" in sql
    assert "memory_semantic_facts.sensitivity IN" in sql
    assert "memory_semantic_facts.consent_status NOT IN" in sql


def test_postgresql_non_vector_retrieval_applies_limit_in_sql(scope):
    class PostgreSQLCapture:
        statement = None

        class Bind:
            class Dialect:
                name = "postgresql"
            dialect = Dialect()

        def get_bind(self):
            return self.Bind()

        def scalars(self, statement):
            self.statement = statement
            return []

    capture = PostgreSQLCapture()
    assert query_active_facts(capture, scope=scope, limit=5) == []
    sql = str(capture.statement.compile(dialect=postgresql.dialect()))
    assert "LIMIT" in sql
    assert "<=>" not in sql


def test_non_postgresql_vector_retrieval_fails_closed_outside_sqlite(scope):
    class UnsupportedDialectCapture:
        queried = False

        class Bind:
            class Dialect:
                name = "mysql"
            dialect = Dialect()

        def get_bind(self):
            return self.Bind()

        def scalars(self, _statement):
            self.queried = True
            raise AssertionError("unsupported vector dialect must fail before executing SQL")

    capture = UnsupportedDialectCapture()
    with pytest.raises(RuntimeError, match="requires PostgreSQL/pgvector"):
        query_active_facts(capture, scope=scope, query_vector=[1.0, 0.0], limit=5)
    assert capture.queried is False


def test_sqlite_vector_fallback_preserves_threshold_and_limit(db, scope):
    for key, vector in (
        ("sqlite-near", [1.0, 0.0]),
        ("sqlite-middle", [0.7, 0.7]),
        ("sqlite-far", [0.0, 1.0]),
    ):
        record_fact(
            db, scope=scope, subject="topic", predicate="similarity", object_value=key,
            idempotency_key=key, embedding=vector,
        )
    rows = query_active_facts(
        db, scope=scope, query_vector=[1.0, 0.0],
        minimum_similarity=0.6, limit=2,
    )
    assert [item.object_value for item in rows] == ["sqlite-near", "sqlite-middle"]


def test_working_state_compare_and_swap_rejects_stale_session(tmp_path, scope):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'checkpoint.db'}")
    GovernanceBase.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    with sessions() as first:
        checkpoint_state(
            first, scope=scope, state_kind="working", state_key="task",
            snapshot={"step": 1}, expected_version=0, idempotency_key="checkpoint-1",
        )
        first.commit()

    with sessions() as winner, sessions() as stale:
        # Both writers observe v1 before either attempts the next CAS.
        assert stale.scalar(select(MemoryCheckpoint)).version == 1
        checkpoint_state(
            winner, scope=scope, state_kind="working", state_key="task",
            snapshot={"step": 2}, expected_version=1, idempotency_key="checkpoint-2",
        )
        winner.commit()
        with pytest.raises(WorkingStateConflict):
            checkpoint_state(
                stale, scope=scope, state_kind="working", state_key="task",
                snapshot={"step": 3}, expected_version=1, idempotency_key="checkpoint-stale",
            )
