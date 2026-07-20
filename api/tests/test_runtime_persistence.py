import asyncio
import pytest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import User, Workspace
from app.runtime_contract import ExecutionStrategy, RuntimeSource, RuntimeStartRequest, RuntimeTransition
from app.runtime_graph import AtlasAgentState
from app.runtime_graph import RuntimePhaseOneGraph
from app.runtime_persistence import SqlAlchemyRuntimeEventRepository, SqlAlchemyRuntimeRunRepository
from app.runtime_service import AgentRuntimeService, RuntimeFeatureFlags


def _session_factory():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory() as db:
        db.add_all([Workspace(id="ws", name="Workspace"), User(id="user", username="user", name="User")])
        db.commit()
    return factory


def _request(key: str = "key") -> RuntimeStartRequest:
    return RuntimeStartRequest(
        workspace_id="ws",
        user_id="user",
        agent_id="agent",
        agent_version_id="v1",
        source=RuntimeSource.CHAT,
        input="hello",
        idempotency_key=key,
    )


def test_sql_repositories_survive_a_new_service_instance_and_replay_cursor():
    factory = _session_factory()
    events = SqlAlchemyRuntimeEventRepository(factory)
    runs = SqlAlchemyRuntimeRunRepository(factory)
    graph = RuntimePhaseOneGraph(lambda _state: "persisted", prefer_langgraph=False)
    flags = RuntimeFeatureFlags(langgraph_enabled=True, allow_legacy_fallback=False)

    first = AgentRuntimeService(graph, event_repository=events, run_repository=runs, flags=flags)
    async def execute():
        handle = await first.start(_request())
        return await first.wait(run_id=handle.run_id, workspace_id="ws")

    handle = asyncio.run(execute())
    initial_events = first.stream(run_id=handle.run_id, workspace_id="ws")
    assert initial_events

    # A separately constructed service represents a new API worker/process.
    second = AgentRuntimeService(graph, event_repository=SqlAlchemyRuntimeEventRepository(factory),
                                 run_repository=SqlAlchemyRuntimeRunRepository(factory), flags=flags)
    restored = second.get_state(run_id=handle.run_id, workspace_id="ws")
    assert restored.status.value == "succeeded"
    replay = second.stream(run_id=handle.run_id, workspace_id="ws", after_sequence=initial_events[-2].sequence)
    assert [event.sequence for event in replay] == [initial_events[-1].sequence]


def test_sql_event_repository_allocates_sequences_after_restart():
    factory = _session_factory()
    runs = SqlAlchemyRuntimeRunRepository(factory)
    record, _ = runs.create_or_get(_request(), "langgraph")
    first = SqlAlchemyRuntimeEventRepository(factory)
    first.append(run_id=record.state.identity.run_id, workspace_id="ws", transition=RuntimeTransition(event_type="run.started"))

    second = SqlAlchemyRuntimeEventRepository(factory)
    event = second.append(run_id=record.state.identity.run_id, workspace_id="ws", transition=RuntimeTransition(event_type="node.started"))
    assert event.sequence == 2


def test_strategy_selection_is_idempotency_bound_and_survives_repository_restart():
    factory = _session_factory()
    runs = SqlAlchemyRuntimeRunRepository(factory)
    request = _request("adaptive-key").model_copy(update={
        "execution_strategy": ExecutionStrategy.MULTI_AGENT_PLAN_EXECUTE_REVIEW,
    })
    record, created = runs.create_or_get(request, "langgraph")
    assert created
    data = record.state.model_dump()
    data.update({
        "execution_strategy": ExecutionStrategy.MULTI_AGENT_PLAN_EXECUTE_REVIEW.value,
        "strategy_decision": {"policy_version": "atlas.strategy-policy.v1"},
        "plan_version": 2,
        "review_round": 1,
    })
    record = record.__class__(
        record.request, AtlasAgentState.model_validate(data), record.execution_mode, record.created_at,
    )
    runs.save(record)

    restored = SqlAlchemyRuntimeRunRepository(factory).get(
        run_id=record.state.identity.run_id, workspace_id="ws",
    )
    assert restored.state.execution_strategy == "multi-agent-plan-execute-review"
    assert restored.state.plan_version == 2
    assert restored.state.review_round == 1

    changed = request.model_copy(update={"execution_strategy": ExecutionStrategy.REACT})
    with pytest.raises(ValueError, match="idempotency"):
        runs.create_or_get(changed, "langgraph")
