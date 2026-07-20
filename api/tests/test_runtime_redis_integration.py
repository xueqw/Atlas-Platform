"""Real Redis loss probe; enabled explicitly by release verification."""

import asyncio
import os

import pytest
from redis import Redis
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import User, Workspace
from app.runtime_contract import RuntimeSource, RuntimeStartRequest
from app.runtime_graph import RuntimePhaseOneGraph
from app.runtime_persistence import (
    SqlAlchemyRuntimeEventRepository,
    SqlAlchemyRuntimeInterruptRepository,
    SqlAlchemyRuntimeRunRepository,
)
from app.runtime_service import AgentRuntimeService, RuntimeFeatureFlags


@pytest.mark.skipif(not os.getenv("ATLAS_TEST_REDIS_URL"), reason="set ATLAS_TEST_REDIS_URL for real Redis loss probe")
def test_redis_loss_does_not_destroy_durable_runtime_state(tmp_path):
    redis = Redis.from_url(os.environ["ATLAS_TEST_REDIS_URL"], decode_responses=True)
    redis.ping()
    redis.flushdb()

    engine = create_engine(f"sqlite:///{tmp_path / 'runtime-authority.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        db.add_all([Workspace(id="ws", name="Workspace"), User(id="user", username="redis-probe")])
        db.commit()

    def service():
        return AgentRuntimeService(
            RuntimePhaseOneGraph(lambda _state: "durable answer", prefer_langgraph=False),
            event_repository=SqlAlchemyRuntimeEventRepository(factory),
            run_repository=SqlAlchemyRuntimeRunRepository(factory),
            interrupt_repository=SqlAlchemyRuntimeInterruptRepository(factory),
            flags=RuntimeFeatureFlags(langgraph_enabled=True, allow_legacy_fallback=False),
        )

    async def execute():
        runtime = service()
        handle = await runtime.start(RuntimeStartRequest(
            workspace_id="ws", user_id="user", agent_id="agent", agent_version_id="v1",
            source=RuntimeSource.WORKBENCH, input="hello", idempotency_key="redis-loss",
        ))
        return await runtime.wait(run_id=handle.run_id, workspace_id="ws"), runtime

    handle, first = asyncio.run(execute())
    cursor = first.stream(run_id=handle.run_id, workspace_id="ws")[-1].sequence
    redis.setex(f"atlas:runtime-cursor:{handle.run_id}", 60, cursor)
    assert redis.get(f"atlas:runtime-cursor:{handle.run_id}") == str(cursor)

    # Simulate complete Redis loss. A fresh service must restore the terminal
    # state and replay events from SQL without consulting the cache.
    redis.flushdb()
    restored = service()
    state = restored.get_state(run_id=handle.run_id, workspace_id="ws")
    replay = restored.stream(run_id=handle.run_id, workspace_id="ws", after_sequence=0)
    assert state.status.value == "succeeded"
    assert state.output == "durable answer"
    assert replay[-1].sequence == cursor
    assert redis.dbsize() == 0
