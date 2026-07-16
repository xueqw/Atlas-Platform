import asyncio

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import Agent, AgentVersion, User, Workspace
from app.runtime_api import RuntimeRunCreate, _owned_state, _resolve_version, _service_for_snapshot, _to_start_request
from app.runtime_contract import RuntimeSource
from app.runtime_persistence import SqlAlchemyRuntimeEventRepository, SqlAlchemyRuntimeRunRepository
from app.runtime_service import AgentRuntimeService, RuntimeFeatureFlags


def _factory():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory() as db:
        db.add_all([
            Workspace(id="ws", name="Workspace"),
            User(id="user", username="user", name="User"),
            Agent(id="agent", workspace_id="ws", name="Agent", current_version_id="v1"),
            AgentVersion(id="v1", agent_id="agent", version_no=1, snapshot_json='{"system_prompt":"be concise","model":"missing-model"}'),
        ])
        db.commit()
    return factory


def test_runtime_api_resolves_workspace_owned_immutable_version():
    factory = _factory()
    with factory() as db:
        agent, version, snapshot = _resolve_version(db, workspace_id="ws", agent_id="agent", version_id=None)
    assert agent.id == "agent"
    assert version.id == "v1"
    assert snapshot["system_prompt"] == "be concise"


def test_runtime_api_request_is_server_resolved_and_replays_sse_payload():
    factory = _factory()
    payload = RuntimeRunCreate(agent_id="agent", input="hello", idempotency_key="runtime-api-key", source=RuntimeSource.WORKBENCH)
    request = _to_start_request(payload, workspace_id="ws", user_id="user", version_id="v1")
    service = _service_for_snapshot({"system_prompt": "test", "model": "missing-model"})
    # Substitute this test's isolated durable stores, while retaining the same
    # model adapter used by the HTTP boundary.
    service = AgentRuntimeService(
        service.graph,
        event_repository=SqlAlchemyRuntimeEventRepository(factory),
        run_repository=SqlAlchemyRuntimeRunRepository(factory),
        legacy_adapter=service.legacy_adapter,
        flags=RuntimeFeatureFlags(langgraph_enabled=False),
    )
    handle = asyncio.run(service.start(request))
    events = service.stream(run_id=handle.run_id, workspace_id="ws")
    assert handle.execution_mode == "legacy-shim"
    assert events[0].type == "run.started"
    assert events[-1].sequence == len(events)


def test_runtime_api_hides_unknown_or_unowned_run_ids():
    factory = _factory()
    service = AgentRuntimeService(
        _service_for_snapshot({"system_prompt": "test"}).graph,
        event_repository=SqlAlchemyRuntimeEventRepository(factory),
        run_repository=SqlAlchemyRuntimeRunRepository(factory),
        flags=RuntimeFeatureFlags(langgraph_enabled=False),
    )
    with pytest.raises(HTTPException) as error:
        _owned_state(service, run_id="missing", workspace_id="ws", user_id="user")
    assert error.value.status_code == 404
