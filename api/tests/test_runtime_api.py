import asyncio

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import Agent, AgentVersion, Document, DocumentChunk, KnowledgeBase, User, Workspace
from app.runtime_api import RuntimeRunCreate, RuntimeRunResume, _owned_state, _resolve_version, _runtime_read_tools, _service_for_snapshot, _to_start_request
from app.runtime_contract import RuntimeAccessDenied, RuntimeSource
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
            Agent(
                id="agent",
                workspace_id="ws",
                name="Agent",
                status="published",
                current_version_id="draft-v2",
                published_version_id="published-v1",
            ),
            AgentVersion(
                id="published-v1",
                agent_id="agent",
                version_no=1,
                label="published",
                snapshot_json='{"system_prompt":"published","model":"missing-model"}',
            ),
            AgentVersion(
                id="draft-v2",
                agent_id="agent",
                version_no=2,
                label="draft",
                snapshot_json='{"system_prompt":"draft","model":"missing-model"}',
            ),
        ])
        db.commit()
    return factory


@pytest.mark.parametrize("source", [RuntimeSource.WORKBENCH, RuntimeSource.CHAT, RuntimeSource.API])
def test_runtime_api_production_sources_resolve_published_version(source):
    factory = _factory()
    with factory() as db:
        agent, version, snapshot = _resolve_version(
            db,
            workspace_id="ws",
            agent_id="agent",
            source=source,
            version_id=None,
        )
    assert agent.id == "agent"
    assert version.id == "published-v1"
    assert snapshot["system_prompt"] == "published"


@pytest.mark.parametrize("source", [RuntimeSource.PREVIEW, RuntimeSource.BUILDER, RuntimeSource.EVALUATION])
def test_runtime_api_authoring_sources_default_to_current_draft(source):
    factory = _factory()
    with factory() as db:
        _, version, snapshot = _resolve_version(
            db,
            workspace_id="ws",
            agent_id="agent",
            source=source,
            version_id=None,
        )
    assert version.id == "draft-v2"
    assert snapshot["system_prompt"] == "draft"


def test_runtime_api_authoring_source_can_select_owned_version():
    factory = _factory()
    with factory() as db:
        _, version, _ = _resolve_version(
            db,
            workspace_id="ws",
            agent_id="agent",
            source=RuntimeSource.PREVIEW,
            version_id="published-v1",
        )
    assert version.id == "published-v1"


@pytest.mark.parametrize("source", [RuntimeSource.WORKBENCH, RuntimeSource.CHAT, RuntimeSource.API])
def test_runtime_api_production_sources_reject_draft_override(source):
    factory = _factory()
    with factory() as db, pytest.raises(HTTPException) as error:
        _resolve_version(
            db,
            workspace_id="ws",
            agent_id="agent",
            source=source,
            version_id="draft-v2",
        )
    assert error.value.status_code == 409


def test_runtime_api_production_source_requires_a_published_version():
    factory = _factory()
    with factory() as db:
        agent = db.get(Agent, "agent")
        agent.published_version_id = None
        db.commit()
        with pytest.raises(HTTPException) as error:
            _resolve_version(
                db,
                workspace_id="ws",
                agent_id="agent",
                source=RuntimeSource.WORKBENCH,
                version_id=None,
            )
    assert error.value.status_code == 409


def test_runtime_api_request_is_server_resolved_and_replays_sse_payload():
    factory = _factory()
    payload = RuntimeRunCreate(agent_id="agent", input="hello", idempotency_key="runtime-api-key", source=RuntimeSource.WORKBENCH)
    request = _to_start_request(payload, workspace_id="ws", user_id="user", version_id="published-v1")
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
    async def execute():
        handle = await service.start(request)
        assert handle.status.value == "running"
        return await service.wait(run_id=handle.run_id, workspace_id="ws")

    handle = asyncio.run(execute())
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


def test_runtime_resume_outer_contract_requires_atomic_interrupt_binding():
    with pytest.raises(ValidationError, match="provided together"):
        RuntimeRunResume(interrupt_id="interrupt-1", decision="approve")

    unbound = RuntimeRunResume()
    assert unbound.interrupt_id is None
    bound = RuntimeRunResume(
        interrupt_id="interrupt-1",
        nonce="nonce-1",
        decision="approve",
        parameter_digest="sha256:parameters",
        resource_version="tool:v1",
    )
    assert bound.decision == "approve"


def test_runtime_resume_http_rejects_partial_binding_as_normalized_422(auth_client):
    response = auth_client.post(
        "/api/runtime/runs/unknown-run/resume",
        json={"interrupt_id": "interrupt-1", "decision": "approve"},
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail[0]["type"] == "value_error"
    assert "provided together" in detail[0]["msg"]


def test_runtime_read_tool_searches_only_the_snapshot_knowledge_base_in_the_bound_workspace(monkeypatch):
    factory = _factory()
    with factory() as db:
        db.add_all([
            Workspace(id="other-workspace", name="Other Workspace"),
            KnowledgeBase(id="kb-owned", workspace_id="ws", name="Owned"),
            KnowledgeBase(id="kb-other", workspace_id="other-workspace", name="Other"),
            Document(id="doc-owned", knowledge_base_id="kb-owned", name="atlas.md", status="ready"),
            DocumentChunk(
                id="chunk-owned",
                document_id="doc-owned",
                chunk_index=0,
                page=1,
                content="Atlas runtime checkpoint recovery uses PostgreSQL.",
            ),
        ])
        db.commit()

    async def no_embedding(_query):
        return None

    monkeypatch.setattr("app.runtime_api.SessionLocal", factory)
    monkeypatch.setattr("app.runtime_api.embed_query", no_embedding)

    tool = _runtime_read_tools({"knowledge_base_id": "kb-owned"}, "ws")["knowledge_search"]
    result = asyncio.run(tool({"query": "Atlas checkpoint", "limit": 4}))
    assert result["knowledge_base_id"] == "kb-owned"
    assert result["matches"][0]["document"] == "atlas.md"
    assert "PostgreSQL" in result["matches"][0]["quote"]

    outside = _runtime_read_tools({"knowledge_base_id": "kb-other"}, "ws")["knowledge_search"]
    with pytest.raises(RuntimeAccessDenied):
        asyncio.run(outside({"query": "secret"}))
