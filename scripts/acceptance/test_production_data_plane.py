"""Real PostgreSQL/pgvector/Redis acceptance tests for Atlas production.

This suite intentionally lives outside ``api/tests`` so ``api/conftest.py``
cannot replace the caller-supplied PostgreSQL URL with SQLite.  It is disabled
unless the release operator explicitly opts in and supplies dedicated service
URLs.  The Redis URL must select a non-zero database because one test performs
``FLUSHDB`` to simulate complete loss of the non-authoritative hot tier.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from urllib.parse import urlparse
import uuid

import pytest


RUN_ENV = "RUN_ATLAS_PRODUCTION_DATA_PLANE_TESTS"
POSTGRES_ENV = "ATLAS_ACCEPTANCE_POSTGRES_URL"
REDIS_ENV = "ATLAS_ACCEPTANCE_REDIS_URL"

pytestmark = pytest.mark.skipif(
    os.getenv(RUN_ENV, "").strip().lower() not in {"1", "true", "yes", "on"},
    reason=f"set {RUN_ENV}=1 to run destructive real-service acceptance tests",
)

ROOT = Path(__file__).resolve().parents[2]
API_ROOT = ROOT / "api"
ACCEPTANCE_PREFIX = "acc-"


def _sqlalchemy_postgres_url(value: str) -> str:
    if value.startswith("postgresql+psycopg://"):
        return value
    if value.startswith("postgresql://"):
        return value.replace("postgresql://", "postgresql+psycopg://", 1)
    raise pytest.UsageError(f"{POSTGRES_ENV} must be a PostgreSQL URL")


def _psycopg_postgres_url(value: str) -> str:
    return value.replace("postgresql+psycopg://", "postgresql://", 1)


def _required_url(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise pytest.UsageError(f"{name} is required when {RUN_ENV}=1")
    return value


def _acceptance_id(label: str) -> str:
    return f"{ACCEPTANCE_PREFIX}{label[:8]}-{uuid.uuid4().hex[:12]}"[:36]


def _basis(index: int, value: float = 1.0) -> list[float]:
    vector = [0.0] * 1024
    vector[index] = value
    return vector


@pytest.fixture(scope="session")
def data_plane():
    postgres_url = _sqlalchemy_postgres_url(_required_url(POSTGRES_ENV))
    checkpoint_url = _psycopg_postgres_url(postgres_url)
    redis_url = _required_url(REDIS_ENV)
    parsed_redis = urlparse(redis_url)
    try:
        redis_database = int((parsed_redis.path or "/0").lstrip("/") or "0")
    except ValueError as exc:
        raise pytest.UsageError(f"{REDIS_ENV} must select a numeric Redis database") from exc
    if redis_database == 0:
        raise pytest.UsageError(
            f"{REDIS_ENV} must select a dedicated non-zero database; the suite executes FLUSHDB"
        )

    # These assignments happen before importing Atlas.  Unlike api/conftest.py,
    # this suite never rewrites the URL to SQLite.
    os.environ["DATABASE_URL"] = postgres_url
    os.environ["LANGGRAPH_CHECKPOINT_DATABASE_URL"] = checkpoint_url
    sys.path.insert(0, str(API_ROOT))

    from redis import Redis
    from sqlalchemy import create_engine, delete, text
    from sqlalchemy.orm import sessionmaker

    from app.database import Base, _ensure_postgresql_additive_schema
    from app.governance_models import GovernanceBase, LedgerEvent, MemoryCheckpoint, SemanticFact
    from app.models import Skill, User, Workspace

    engine = create_engine(postgres_url, pool_pre_ping=True)
    with engine.begin() as connection:
        major = int(connection.execute(text("SHOW server_version_num")).scalar_one()) // 10000
        assert major == 16, f"acceptance requires PostgreSQL 16, got major version {major}"
        connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        assert connection.execute(
            text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
        ).scalar_one()
        Base.metadata.create_all(connection)
        GovernanceBase.metadata.create_all(connection)
        _ensure_postgresql_additive_schema(connection)

    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    redis = Redis.from_url(redis_url, decode_responses=True)
    assert redis.ping()

    context = SimpleNamespace(
        engine=engine,
        factory=factory,
        postgres_url=postgres_url,
        checkpoint_url=checkpoint_url,
        redis=redis,
    )
    try:
        yield context
    finally:
        # Clean only rows created by this acceptance suite. Governance tables
        # have their own metadata and therefore need explicit tenant cleanup.
        with factory() as db:
            pattern = f"{ACCEPTANCE_PREFIX}%"
            db.execute(delete(MemoryCheckpoint).where(MemoryCheckpoint.workspace_id.like(pattern)))
            db.execute(delete(SemanticFact).where(SemanticFact.workspace_id.like(pattern)))
            db.execute(delete(LedgerEvent).where(LedgerEvent.workspace_id.like(pattern)))
            db.execute(delete(Skill).where(Skill.workspace_id.like(pattern)))
            db.execute(delete(Workspace).where(Workspace.id.like(pattern)))
            db.execute(delete(User).where(User.id.like(pattern)))
            db.commit()
        redis.close()
        engine.dispose()


def _create_runtime_identity(data_plane, *, workspace_id: str, user_id: str) -> None:
    from app.models import User, Workspace

    with data_plane.factory() as db:
        db.add_all([
            Workspace(id=workspace_id, name="Atlas acceptance workspace"),
            User(id=user_id, username=f"{user_id}@acceptance.invalid", name="Acceptance User"),
        ])
        db.commit()


def test_postgres_pgvector_skill_and_semantic_memory_are_tenant_and_time_scoped(data_plane):
    """Exercise real vector(1024), DB ordering/limit, tenant scope and both time axes."""
    from app.memory_ledger import MemoryScope, query_active_facts, record_fact
    from app.models import Skill
    from app.skill_router import persisted_vector_scores_from_db

    workspace_id = _acceptance_id("vector")
    other_workspace_id = _acceptance_id("other")
    user_id = _acceptance_id("user")
    agent_id = _acceptance_id("agent")
    scope = MemoryScope(workspace_id=workspace_id, user_id=user_id, agent_id=agent_id)
    other_scope = MemoryScope(
        workspace_id=other_workspace_id,
        user_id=user_id,
        agent_id=agent_id,
    )
    _create_runtime_identity(data_plane, workspace_id=workspace_id, user_id=user_id)
    with data_plane.factory() as db:
        from app.models import Workspace

        db.add(Workspace(id=other_workspace_id, name="Other acceptance tenant"))
        # No ORM relationship connects the workspace object to the Skill rows,
        # so make the FK parent durable before the executemany skill insert.
        db.flush()
        exact_skill = Skill(
            id=_acceptance_id("skill"), workspace_id=workspace_id, name="Exact",
            description="exact vector", embedding=_basis(0), embedding_model="acceptance-1024",
        )
        near_vector = _basis(0, 0.99)
        near_vector[1] = 0.01
        near_skill = Skill(
            id=_acceptance_id("skill"), workspace_id=workspace_id, name="Near",
            description="near vector", embedding=near_vector, embedding_model="acceptance-1024",
        )
        far_skill = Skill(
            id=_acceptance_id("skill"), workspace_id=workspace_id, name="Far",
            description="far vector", embedding=_basis(1), embedding_model="acceptance-1024",
        )
        foreign_skill = Skill(
            id=_acceptance_id("skill"), workspace_id=other_workspace_id, name="Foreign exact",
            description="must never cross tenant", embedding=_basis(0), embedding_model="acceptance-1024",
        )
        db.add_all([exact_skill, near_skill, far_skill, foreign_skill])
        db.commit()

        scores = persisted_vector_scores_from_db(
            db,
            Skill,
            workspace_id=workspace_id,
            query_vector=_basis(0),
            limit=2,
            minimum_similarity=0.5,
        )
        assert list(scores) == [exact_skill.id, near_skill.id]
        assert foreign_skill.id not in scores
        assert far_skill.id not in scores

        january_1 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        january_2 = datetime(2026, 1, 2, tzinfo=timezone.utc)
        february_1 = datetime(2026, 2, 1, tzinfo=timezone.utc)
        february_15 = datetime(2026, 2, 15, tzinfo=timezone.utc)
        march_1 = datetime(2026, 3, 1, tzinfo=timezone.utc)
        april_1 = datetime(2026, 4, 1, tzinfo=timezone.utc)

        old_address, _ = record_fact(
            db,
            scope=scope,
            subject="user",
            predicate="address",
            object_value="Hangzhou",
            idempotency_key=_acceptance_id("fact"),
            valid_from=january_1,
            recorded_at=january_2,
            embedding=_basis(4),
            embedding_model="acceptance-1024",
        )
        record_fact(
            db,
            scope=scope,
            subject="user",
            predicate="address",
            object_value="Shanghai",
            idempotency_key=_acceptance_id("fact"),
            valid_from=february_1,
            recorded_at=march_1,
            supersedes_fact_id=old_address.fact_id,
            embedding=_basis(4),
            embedding_model="acceptance-1024",
        )
        # A closer vector in another tenant must never enter this tenant's recall.
        record_fact(
            db,
            scope=other_scope,
            subject="user",
            predicate="address",
            object_value="FOREIGN",
            idempotency_key=_acceptance_id("fact"),
            valid_from=january_1,
            recorded_at=january_2,
            embedding=_basis(4),
            embedding_model="acceptance-1024",
        )
        db.commit()

        before_correction = query_active_facts(
            db,
            scope=scope,
            as_of_valid=february_15,
            as_of_transaction=february_15,
            query_vector=_basis(4),
            minimum_similarity=0.9,
            limit=1,
        )
        after_correction = query_active_facts(
            db,
            scope=scope,
            as_of_valid=february_15,
            as_of_transaction=april_1,
            query_vector=_basis(4),
            minimum_similarity=0.9,
            limit=1,
        )
        historical_valid_time = query_active_facts(
            db,
            scope=scope,
            as_of_valid=datetime(2026, 1, 15, tzinfo=timezone.utc),
            as_of_transaction=april_1,
            query_vector=_basis(4),
            minimum_similarity=0.9,
            limit=1,
        )

        assert [fact.object_value for fact in before_correction] == ["Hangzhou"]
        assert [fact.object_value for fact in after_correction] == ["Shanghai"]
        assert [fact.object_value for fact in historical_valid_time] == ["Hangzhou"]


def test_langgraph_checkpoint_restores_with_a_new_postgres_saver(data_plane):
    """Release spike: saver/graph instance B resumes instance A without replaying a completed node."""
    from app.runtime_checkpoint import open_postgres_checkpointer, setup_postgres_checkpoints
    from app.runtime_contract import RuntimeIdentity, RuntimeSource
    from app.runtime_graph import AtlasAgentState, RuntimePhaseOneGraph

    async def exercise() -> None:
        await setup_postgres_checkpoints(data_plane.checkpoint_url)
        suffix = uuid.uuid4().hex
        identity = RuntimeIdentity(
            run_id=f"{ACCEPTANCE_PREFIX}checkpoint-{suffix[:10]}",
            thread_id=f"{ACCEPTANCE_PREFIX}workspace:{suffix}",
            workspace_id=f"{ACCEPTANCE_PREFIX}workspace",
            user_id=f"{ACCEPTANCE_PREFIX}user",
            agent_id=f"{ACCEPTANCE_PREFIX}agent",
            agent_version_id="v1",
            source=RuntimeSource.API,
        )
        initial = AtlasAgentState.initial(identity, "real PostgreSQL checkpoint acceptance")
        calls = {"first": 0, "restart": 0}

        def first_model(_state):
            calls["first"] += 1
            return "checkpoint persisted"

        async with open_postgres_checkpointer(data_plane.checkpoint_url) as first_saver:
            first_graph = RuntimePhaseOneGraph(first_model, checkpointer=first_saver)
            completed = await first_graph.ainvoke(initial)
            assert await first_saver.aget_tuple(
                {"configurable": {"thread_id": identity.thread_id}}
            ) is not None

        def duplicate_node(_state):
            calls["restart"] += 1
            raise AssertionError("a completed node was replayed after PostgreSQL recovery")

        async with open_postgres_checkpointer(data_plane.checkpoint_url) as second_saver:
            second_graph = RuntimePhaseOneGraph(duplicate_node, checkpointer=second_saver)
            restored = await second_graph.aresume(completed.state)
            assert await second_saver.aget_tuple(
                {"configurable": {"thread_id": identity.thread_id}}
            ) is not None

        assert restored.state.output == "checkpoint persisted"
        assert calls == {"first": 1, "restart": 0}

    asyncio.run(exercise())


def test_runtime_same_idempotency_is_atomic_and_cursor_replay_is_ordered(data_plane):
    """Concurrent callers converge on one RuntimeRun; event cursors remain monotonic."""
    from app.runtime_contract import RuntimeSource, RuntimeStartRequest, RuntimeTransition
    from app.runtime_persistence import SqlAlchemyRuntimeEventRepository, SqlAlchemyRuntimeRunRepository

    workspace_id = _acceptance_id("runtime")
    user_id = _acceptance_id("user")
    _create_runtime_identity(data_plane, workspace_id=workspace_id, user_id=user_id)
    request = RuntimeStartRequest(
        workspace_id=workspace_id,
        user_id=user_id,
        agent_id=_acceptance_id("agent"),
        agent_version_id="v1",
        source=RuntimeSource.API,
        input="concurrent idempotency acceptance",
        idempotency_key=_acceptance_id("idem"),
    )
    runs = SqlAlchemyRuntimeRunRepository(data_plane.factory)

    def create_or_get(_index: int):
        record, created = runs.create_or_get(request, "langgraph")
        return record.state.identity.run_id, created

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(create_or_get, range(8)))
    assert len({run_id for run_id, _created in outcomes}) == 1
    assert sum(1 for _run_id, created in outcomes if created) == 1
    run_id = outcomes[0][0]

    events = SqlAlchemyRuntimeEventRepository(data_plane.factory)

    def append_event(index: int):
        return events.append(
            run_id=run_id,
            workspace_id=workspace_id,
            transition=RuntimeTransition(
                event_type="acceptance.concurrent",
                payload={"index": index},
            ),
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        appended = list(pool.map(append_event, range(8)))
    assert sorted(event.sequence for event in appended) == list(range(1, 9))
    assert [event.sequence for event in events.replay(
        run_id=run_id,
        workspace_id=workspace_id,
        after_sequence=3,
    )] == [4, 5, 6, 7, 8]


def test_redis_ttl_and_flush_rebuild_session_and_runtime_from_postgres(data_plane):
    """Redis loss must not erase session checkpoints, Runtime state, or event cursor replay."""
    from app.memory_ledger import MemoryScope
    from app.memory_session import DurableSessionContextStore
    from app.runtime_contract import RuntimeSource, RuntimeStartRequest, RuntimeTransition
    from app.runtime_persistence import SqlAlchemyRuntimeEventRepository, SqlAlchemyRuntimeRunRepository

    redis = data_plane.redis
    redis.flushdb()
    workspace_id = _acceptance_id("recovery")
    user_id = _acceptance_id("user")
    agent_id = _acceptance_id("agent")
    _create_runtime_identity(data_plane, workspace_id=workspace_id, user_id=user_id)
    scope = MemoryScope(
        workspace_id=workspace_id,
        user_id=user_id,
        agent_id=agent_id,
        run_id=_acceptance_id("run"),
    )
    store = DurableSessionContextStore(data_plane.factory, redis, ttl_seconds=30)
    written = store.write(
        scope=scope,
        session_id="acceptance-session",
        snapshot={"messages": [{"role": "user", "content": "durable"}]},
        expected_version=0,
        idempotency_key=_acceptance_id("session"),
    )
    session_key = store._key(scope, "acceptance-session")
    assert written["version"] == 1
    assert 0 < redis.ttl(session_key) <= 30

    request = RuntimeStartRequest(
        workspace_id=workspace_id,
        user_id=user_id,
        agent_id=agent_id,
        agent_version_id="v1",
        source=RuntimeSource.API,
        input="Redis loss runtime authority",
        idempotency_key=_acceptance_id("runtime"),
    )
    run_repository = SqlAlchemyRuntimeRunRepository(data_plane.factory)
    event_repository = SqlAlchemyRuntimeEventRepository(data_plane.factory)
    runtime_record, created = run_repository.create_or_get(request, "langgraph")
    assert created
    emitted = event_repository.append(
        run_id=runtime_record.state.identity.run_id,
        workspace_id=workspace_id,
        transition=RuntimeTransition(event_type="run.started", payload={"mode": "langgraph"}),
    )
    redis.setex(
        f"atlas:runtime-cursor:{runtime_record.state.identity.run_id}",
        30,
        emitted.sequence,
    )
    assert redis.dbsize() >= 2

    # Simulate total loss of the dedicated Redis hot tier.
    redis.flushdb()
    assert redis.dbsize() == 0

    restarted_store = DurableSessionContextStore(data_plane.factory, redis, ttl_seconds=30)
    rebuilt = restarted_store.read(scope=scope, session_id="acceptance-session")
    assert rebuilt == written
    assert 0 < redis.ttl(session_key) <= 30

    restarted_runs = SqlAlchemyRuntimeRunRepository(data_plane.factory)
    restarted_events = SqlAlchemyRuntimeEventRepository(data_plane.factory)
    restored_state = restarted_runs.get(
        run_id=runtime_record.state.identity.run_id,
        workspace_id=workspace_id,
    )
    replay = restarted_events.replay(
        run_id=runtime_record.state.identity.run_id,
        workspace_id=workspace_id,
        after_sequence=0,
    )
    assert restored_state.request == request
    assert [event.sequence for event in replay] == [1]
    assert replay[0].type == "run.started"
