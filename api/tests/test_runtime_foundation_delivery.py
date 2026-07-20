import asyncio
from datetime import timedelta
import json

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import RuntimeRun, User, WorkflowRun, WorkflowStep, Workspace
from app.runtime_contract import (
    RuntimeAccessDenied,
    RuntimeInterruptCreate,
    RuntimeResumeRequest,
    RuntimeSource,
    RuntimeStartRequest,
    utcnow,
)
from app.runtime_graph import RuntimePhaseOneGraph
from app.runtime_persistence import (
    SqlAlchemyRuntimeEventRepository,
    SqlAlchemyRuntimeInterruptRepository,
    SqlAlchemyRuntimeRunRepository,
)
from app.runtime_service import AgentRuntimeService, RuntimeFeatureFlags


def _request(key: str = "delivery") -> RuntimeStartRequest:
    return RuntimeStartRequest(
        workspace_id="ws",
        user_id="user",
        agent_id="agent",
        agent_version_id="v1",
        source=RuntimeSource.WORKBENCH,
        input="hello",
        idempotency_key=key,
    )


def _factory():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory() as db:
        db.add_all([
            Workspace(id="ws", name="Workspace"),
            Workspace(id="other", name="Other"),
            User(id="user", username="user", name="User"),
        ])
        db.commit()
    return factory


def test_start_returns_running_handle_and_events_reveal_final_output_once():
    async def scenario():
        entered = asyncio.Event()
        release = asyncio.Event()

        async def model(_state):
            entered.set()
            await release.wait()
            return "final answer"

        service = AgentRuntimeService(
            RuntimePhaseOneGraph(model, prefer_langgraph=False),
            flags=RuntimeFeatureFlags(langgraph_enabled=True, allow_legacy_fallback=False),
        )
        handle = await service.start(_request())
        assert handle.status.value == "running"
        await entered.wait()
        assert service.get_state(run_id=handle.run_id, workspace_id="ws").status.value == "running"
        release.set()
        completed = await service.wait(run_id=handle.run_id, workspace_id="ws")
        events = service.stream(run_id=handle.run_id, workspace_id="ws")
        return completed, events

    completed, events = asyncio.run(scenario())
    assert completed.status.value == "succeeded"
    assert len({event.event_id for event in events}) == len(events)
    assert [event.payload["token"] for event in events if event.type == "response.token"] == ["final answer"]
    terminal = [event for event in events if event.type == "run.completed"]
    assert len(terminal) == 1
    assert terminal[0].payload["output"] == "final answer"
    cursor_replay = [event for event in events if event.sequence > events[-2].sequence]
    assert [event.sequence for event in cursor_replay] == [events[-1].sequence]


def test_sql_projection_tracks_runtime_run_steps_and_terminal_output():
    factory = _factory()
    service = AgentRuntimeService(
        RuntimePhaseOneGraph(lambda _state: "projected", prefer_langgraph=False),
        event_repository=SqlAlchemyRuntimeEventRepository(factory),
        run_repository=SqlAlchemyRuntimeRunRepository(factory),
        interrupt_repository=SqlAlchemyRuntimeInterruptRepository(factory),
        flags=RuntimeFeatureFlags(langgraph_enabled=True, allow_legacy_fallback=False),
    )

    async def scenario():
        handle = await service.start(_request("projection"))
        return await service.wait(run_id=handle.run_id, workspace_id="ws")

    handle = asyncio.run(scenario())
    with factory() as db:
        runtime = db.get(RuntimeRun, handle.run_id)
        workflow = db.get(WorkflowRun, runtime.legacy_workflow_run_id)
        steps = db.scalars(select(WorkflowStep).where(WorkflowStep.run_id == workflow.id).order_by(WorkflowStep.index)).all()
        assert workflow.status == "succeeded"
        assert json.loads(workflow.output_json)["answer"] == "projected"
        assert {step.title for step in steps} >= {"load_package", "plan", "direct_response", "finalize"}
        assert all(step.status == "succeeded" for step in steps)


def test_interrupt_resolution_is_actor_bound_and_single_use():
    factory = _factory()
    service = AgentRuntimeService(
        RuntimePhaseOneGraph(lambda _state: "unused", prefer_langgraph=False),
        event_repository=SqlAlchemyRuntimeEventRepository(factory),
        run_repository=SqlAlchemyRuntimeRunRepository(factory),
        interrupt_repository=SqlAlchemyRuntimeInterruptRepository(factory),
        flags=RuntimeFeatureFlags(langgraph_enabled=True, allow_legacy_fallback=False),
    )

    async def scenario():
        handle = await service.start(_request("interrupt"))
        interrupt = service.request_interrupt(RuntimeInterruptCreate(
            run_id=handle.run_id,
            workspace_id="ws",
            user_id="user",
            parameter_digest="sha256:params",
            resource_version="tool:v1",
            scope=("tool:send",),
            nonce="0123456789abcdef",
            expires_at=utcnow() + timedelta(minutes=5),
            payload={"tool": "send"},
        ))
        request = RuntimeResumeRequest(
            run_id=handle.run_id,
            workspace_id="ws",
            user_id="user",
            interrupt_id=interrupt.interrupt_id,
            nonce=interrupt.nonce,
            decision="deny",
            parameter_digest=interrupt.parameter_digest,
            resource_version=interrupt.resource_version,
        )
        event = next(item for item in service.stream(
            run_id=handle.run_id, workspace_id="ws"
        ) if item.type == "interrupt.requested")
        denied = await service.resume(request)
        return interrupt, request, event, denied

    interrupt, request, event, denied = asyncio.run(scenario())
    assert denied.status.value == "cancelled"
    assert event.payload["nonce"] == interrupt.nonce
    assert event.payload["parameter_digest"] == interrupt.parameter_digest
    assert event.payload["resource_version"] == interrupt.resource_version
    with pytest.raises(ValueError, match="already consumed"):
        asyncio.run(service.resume(request))
    with pytest.raises(RuntimeAccessDenied):
        SqlAlchemyRuntimeInterruptRepository(factory).resolve(request.model_copy(update={"workspace_id": "other"}))
    assert interrupt.scope == ("tool:send",)


def test_interrupt_resume_contract_rejects_partial_binding():
    with pytest.raises(ValidationError, match="provided together"):
        RuntimeResumeRequest(run_id="run", workspace_id="ws", user_id="user", interrupt_id="interrupt")
