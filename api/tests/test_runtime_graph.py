import asyncio

from app.runtime_contract import RetryPolicy, RuntimeResumeRequest, RuntimeSource, RuntimeStartRequest
from app.runtime_graph import AtlasAgentState, LANGGRAPH_AVAILABLE, LegacyGraphShim, RuntimeDecision, RuntimePhaseOneGraph, RuntimeToolCall
from app.runtime_service import AgentRuntimeService, RuntimeFeatureFlags


def state():
    request = RuntimeStartRequest(workspace_id="ws", user_id="user", agent_id="agent", agent_version_id="v1", source=RuntimeSource.CHAT, input="hello", idempotency_key="key")
    return AtlasAgentState.initial(request.make_identity("run"), request.input)


def test_direct_response_uses_stub_without_external_model():
    result = asyncio.run(LegacyGraphShim(lambda _state: "stub response").ainvoke(state()))
    assert result.state.output == "stub response"
    assert result.state.status.value == "succeeded"
    assert result.engine == "legacy-shim"


def test_memory_context_is_injected_before_the_user_message():
    captured = {}

    async def model(runtime_state):
        captured["messages"] = runtime_state.messages
        captured["memory"] = runtime_state.memory_context
        return "memory-aware"

    graph = LegacyGraphShim(
        model,
        memory_loader=lambda _state: ({"source": "long_term", "items": [{"content": "客户偏好中文"}]},),
    )
    result = asyncio.run(graph.ainvoke(state()))

    assert result.state.output == "memory-aware"
    assert captured["memory"][0]["source"] == "long_term"
    assert captured["messages"][-1] == {"role": "user", "content": "hello"}
    assert "非可信记忆数据" in captured["messages"][-2]["content"]


def test_read_only_tool_retries_and_write_tool_is_denied():
    calls = {"count": 0, "write": 0}

    def flaky_tool(_args):
        calls["count"] += 1
        if calls["count"] == 1:
            raise TimeoutError("temporary")
        return "found"

    graph = LegacyGraphShim(
        lambda _state: RuntimeDecision(response="answer", tool_calls=(RuntimeToolCall(name="search"),)),
        read_tools={"search": flaky_tool}, retry_policy=RetryPolicy(base_delay_seconds=0),
    )
    result = asyncio.run(graph.ainvoke(state()))
    assert calls["count"] == 2
    assert result.state.tool_results[0]["result"] == "found"
    assert any(item.event_type == "retry.scheduled" for item in result.state.transitions)

    denied = asyncio.run(LegacyGraphShim(
        lambda _state: RuntimeDecision(response="", tool_calls=(RuntimeToolCall(name="send", access="write"),)),
        read_tools={"send": lambda _args: calls.__setitem__("write", 1)},
    ).ainvoke(state()))
    assert calls["write"] == 0
    assert denied.state.status.value == "failed"
    assert any(item.event_type == "tool.denied" for item in denied.state.transitions)


def test_service_is_idempotent_and_feature_flag_selects_graph():
    invocations = {"count": 0}

    async def model(_state):
        invocations["count"] += 1
        return "ok"

    service = AgentRuntimeService(RuntimePhaseOneGraph(model, prefer_langgraph=False), flags=RuntimeFeatureFlags(langgraph_enabled=True))
    request = RuntimeStartRequest(workspace_id="ws", user_id="user", agent_id="agent", agent_version_id="v1", source=RuntimeSource.CHAT, input="hello", idempotency_key="same")
    async def start_twice():
        return await asyncio.gather(service.start(request), service.start(request))

    first, second = asyncio.run(start_twice())
    assert first.run_id == second.run_id
    assert invocations["count"] == 1
    events = service.stream(run_id=first.run_id, workspace_id="ws")
    assert [event.sequence for event in events] == list(range(1, len(events) + 1))
    assert service.get_state(run_id=first.run_id, workspace_id="ws").status.value == "succeeded"


def test_real_langgraph_graph_runs_with_a_checkpoint_thread():
    if not LANGGRAPH_AVAILABLE:
        return
    from langgraph.checkpoint.memory import InMemorySaver

    result = asyncio.run(RuntimePhaseOneGraph(
        lambda _state: "checkpointed",
        checkpointer=InMemorySaver(),
    ).ainvoke(state()))
    assert result.engine == "langgraph"
    assert result.state.output == "checkpointed"
