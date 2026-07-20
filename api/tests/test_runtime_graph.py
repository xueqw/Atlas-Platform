import asyncio

from app.runtime_contract import RetryPolicy, RuntimeResumeRequest, RuntimeSource, RuntimeStartRequest
from app.runtime_graph import AtlasAgentState, GraphExecutionResult, LANGGRAPH_AVAILABLE, LegacyGraphShim, RuntimeDecision, RuntimePhaseOneGraph, RuntimeToolCall
from app.runtime_service import AgentRuntimeService, GraphLegacyAdapter, RuntimeFeatureFlags


def state():
    request = RuntimeStartRequest(workspace_id="ws", user_id="user", agent_id="agent", agent_version_id="v1", source=RuntimeSource.CHAT, input="hello", idempotency_key="key")
    return AtlasAgentState.initial(request.make_identity("run"), request.input)


def test_direct_response_uses_stub_without_external_model():
    result = asyncio.run(LegacyGraphShim(lambda _state: "stub response").ainvoke(state()))
    assert result.state.output == "stub response"
    assert result.state.status.value == "succeeded"
    assert result.engine == "legacy-shim"


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


def test_react_returns_tool_observation_to_model_before_final_answer():
    observations = []

    def model(runtime_state):
        observations.append(runtime_state.tool_results)
        if not runtime_state.tool_results:
            return RuntimeDecision(tool_calls=(RuntimeToolCall(name="lookup", arguments={"query": "Atlas"}),))
        return RuntimeDecision(response=f"grounded: {runtime_state.tool_results[-1]['result']}")

    result = asyncio.run(LegacyGraphShim(
        model,
        read_tools={"lookup": lambda arguments: {"query": arguments["query"], "matches": ["document-1"]}},
    ).ainvoke(state()))

    assert result.state.status.value == "succeeded"
    assert result.state.output == "grounded: {'query': 'Atlas', 'matches': ['document-1']}"
    assert len(observations) == 2
    assert observations[0] == ()
    assert observations[1][0]["name"] == "lookup"
    events = [item.event_type for item in result.state.transitions]
    assert events.index("tool.started") < events.index("tool.completed")


def test_transient_exhaustion_enters_bounded_recovery_then_terminates_cleanly():
    calls = {"count": 0}

    def unavailable(_state):
        calls["count"] += 1
        raise TimeoutError("provider unavailable")

    result = asyncio.run(LegacyGraphShim(
        unavailable,
        retry_policy=RetryPolicy(max_attempts=2, base_delay_seconds=0),
    ).ainvoke(state()))

    assert result.state.status.value == "failed"
    assert calls["count"] == 4  # two model attempts, one bounded recovery, two final attempts
    assert result.state.retry_counters == {"direct_response": 1, "recovery": 1}
    events = [item.event_type for item in result.state.transitions]
    assert events.count("recovery.scheduled") == 1
    assert events[-1] == "run.failed"
    assert any(item.event_type == "node.failed" and item.payload["node"] == "direct_response" for item in result.state.transitions)


def test_tool_registry_policy_rejects_unknown_and_write_calls_before_invocation():
    invoked = {"count": 0}
    decisions = iter([
        RuntimeDecision(tool_calls=(RuntimeToolCall(name="unknown", access="read"),)),
    ])
    result = asyncio.run(LegacyGraphShim(
        lambda _state: next(decisions),
        read_tools={"lookup": lambda _arguments: invoked.__setitem__("count", invoked["count"] + 1)},
    ).ainvoke(state()))

    assert result.state.status.value == "failed"
    assert invoked["count"] == 0
    assert result.state.errors[-1]["category"] == "permission"
    assert any(item.event_type == "tool.denied" for item in result.state.transitions)


def test_service_is_idempotent_and_feature_flag_selects_graph():
    invocations = {"count": 0}

    async def model(_state):
        invocations["count"] += 1
        return "ok"

    service = AgentRuntimeService(RuntimePhaseOneGraph(model, prefer_langgraph=False), flags=RuntimeFeatureFlags(langgraph_enabled=True))
    request = RuntimeStartRequest(workspace_id="ws", user_id="user", agent_id="agent", agent_version_id="v1", source=RuntimeSource.CHAT, input="hello", idempotency_key="same")
    async def start_twice():
        first, second = await asyncio.gather(service.start(request), service.start(request))
        await service.wait(run_id=first.run_id, workspace_id="ws")
        return first, second

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


def test_governed_memory_and_selected_skill_adapters_are_model_visible_without_metadata_leak():
    observed = {}

    def model(runtime_state):
        observed["messages"] = runtime_state.messages
        observed["capabilities"] = runtime_state.selected_capabilities
        return "governed"

    graph = RuntimePhaseOneGraph(
        model,
        prefer_langgraph=False,
        memory_loader=lambda _state: {
            "untrusted_context": True,
            "facts": [{"fact_id": "fact-1", "statement": "user city Hangzhou"}],
        },
        skill_selector=lambda _state: {
            "decision_id": "decision-1",
            "decision": "selected",
            "selected_skills": [{"id": "support", "name": "Support", "content": "Use approved response format"}],
        },
    )
    result = asyncio.run(graph.ainvoke(state()))
    assert result.state.output == "governed"
    assert observed["capabilities"] == ("support",)
    assert any("不可信数据" in message["content"] and "fact-1" in message["content"] for message in observed["messages"])
    assert any("已授权并选中" in message["content"] and "Use approved" in message["content"] for message in observed["messages"])
    skill_events = [item for item in result.state.transitions if item.event_type == "skills.selected"]
    assert skill_events[0].payload == {
        "decision_id": "decision-1", "selected_ids": ["support"], "decision": "selected",
    }


def test_stream_failure_after_persisted_side_effect_never_falls_back_to_legacy():
    legacy_calls = 0

    class FailsAfterWrite:
        async def astream(self, initial):
            data = initial.model_dump()
            data["side_effects_started"] = True
            yield GraphExecutionResult(
                state=AtlasAgentState.model_validate(data), engine="langgraph",
            )
            raise RuntimeError("crash after write")

    async def legacy(_state):
        nonlocal legacy_calls
        legacy_calls += 1
        return "unsafe replay"

    service = AgentRuntimeService(
        FailsAfterWrite(),
        legacy_adapter=GraphLegacyAdapter(RuntimePhaseOneGraph(legacy, prefer_langgraph=False)),
        flags=RuntimeFeatureFlags(langgraph_enabled=True, allow_legacy_fallback=True),
    )
    request = RuntimeStartRequest(
        workspace_id="ws", user_id="user", agent_id="agent", agent_version_id="v1",
        source=RuntimeSource.CHAT, input="write", idempotency_key="write-once",
    )

    async def execute():
        handle = await service.start(request)
        return await service.wait(run_id=handle.run_id, workspace_id="ws")

    handle = asyncio.run(execute())
    assert handle.status.value == "failed"
    assert legacy_calls == 0
    events = service.stream(run_id=handle.run_id, workspace_id="ws")
    assert all(event.type != "runtime.fallback" for event in events)
