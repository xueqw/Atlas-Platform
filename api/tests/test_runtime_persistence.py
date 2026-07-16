import asyncio

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import User, Workspace
from app.runtime_contract import RuntimeSource, RuntimeStartRequest, RuntimeTransition
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
    handle = asyncio.run(first.start(_request()))
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
