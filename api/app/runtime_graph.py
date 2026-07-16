"""Bounded Phase 1 graph: direct responses and read-only tool calls only."""

from __future__ import annotations

import asyncio
import inspect
import json
from typing import Any, Awaitable, Callable, TypedDict

from pydantic import ConfigDict, Field, field_validator

from .runtime_contract import (
    RetryPolicy,
    RuntimeContractModel,
    RuntimeErrorCategory,
    RuntimeFailure,
    RuntimeIdentity,
    RuntimeState,
    RuntimeStatus,
    RuntimeTransition,
    classify_error,
)

try:  # Unit tests and legacy deployments do not need LangGraph installed.
    from langgraph.graph import END, StateGraph
    LANGGRAPH_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised when optional dependency is absent
    END = "__end__"
    StateGraph = None
    LANGGRAPH_AVAILABLE = False


class ToolAccess(str):
    READ = "read"
    WRITE = "write"


class RuntimeToolCall(RuntimeContractModel):
    name: str = Field(min_length=1, max_length=128)
    arguments: dict[str, Any] = Field(default_factory=dict)
    access: str = ToolAccess.READ

    @field_validator("access")
    @classmethod
    def known_access(cls, value: str) -> str:
        if value not in {ToolAccess.READ, ToolAccess.WRITE}:
            raise ValueError("tool access must be read or write")
        return value


class RuntimeDecision(RuntimeContractModel):
    response: str = ""
    tool_calls: tuple[RuntimeToolCall, ...] = ()


class AtlasAgentState(RuntimeContractModel):
    """Serializable graph state whose identity is immutable and model-unwritable."""

    model_config = ConfigDict(extra="forbid")

    identity: RuntimeIdentity
    input: str
    messages: tuple[dict[str, str], ...] = ()
    selected_capabilities: tuple[str, ...] = ()
    plan: dict[str, Any] = Field(default_factory=dict)
    node_status: dict[str, str] = Field(default_factory=dict)
    tool_calls: tuple[RuntimeToolCall, ...] = ()
    tool_results: tuple[dict[str, Any], ...] = ()
    retry_counters: dict[str, int] = Field(default_factory=dict)
    errors: tuple[dict[str, Any], ...] = ()
    output: str | None = None
    status: RuntimeStatus = RuntimeStatus.PENDING
    side_effects_started: bool = False
    transitions: tuple[RuntimeTransition, ...] = ()

    @classmethod
    def initial(cls, identity: RuntimeIdentity, input_text: str) -> "AtlasAgentState":
        return cls(identity=identity, input=input_text, messages=({"role": "user", "content": input_text},))

    def to_runtime_state(self) -> RuntimeState:
        return RuntimeState(
            identity=self.identity,
            status=self.status,
            node_status=self.node_status,
            retry_counters=self.retry_counters,
            errors=self.errors,
            output=self.output,
            side_effects_started=self.side_effects_started,
        )


class GraphExecutionResult(RuntimeContractModel):
    state: AtlasAgentState
    engine: str


ModelInvoker = Callable[[AtlasAgentState], RuntimeDecision | str | dict[str, Any] | Awaitable[RuntimeDecision | str | dict[str, Any]]]
ToolInvoker = Callable[[dict[str, Any]], Any | Awaitable[Any]]


class _GraphEnvelope(TypedDict):
    state: dict[str, Any]


class RuntimePhaseOneGraph:
    """Graph template with no provider dependency; callers inject a model/tool adapter."""

    def __init__(self, model: ModelInvoker, *, read_tools: dict[str, ToolInvoker] | None = None,
                 retry_policy: RetryPolicy | None = None, prefer_langgraph: bool = True,
                 checkpointer: Any | None = None) -> None:
        self.model = model
        self.read_tools = read_tools or {}
        self.retry_policy = retry_policy or RetryPolicy()
        self.checkpointer = checkpointer
        self._compiled = self._compile() if prefer_langgraph and LANGGRAPH_AVAILABLE else None

    @property
    def engine(self) -> str:
        return "langgraph" if self._compiled is not None else "legacy-shim"

    async def ainvoke(self, state: AtlasAgentState) -> GraphExecutionResult:
        if self._compiled is None:
            return GraphExecutionResult(state=await self._run_manually(state), engine=self.engine)
        raw = await self._compiled.ainvoke(
            {"state": state.model_dump(mode="json")},
            config={"configurable": {"thread_id": state.identity.thread_id}},
        )
        return GraphExecutionResult(state=self._from_json_state(raw["state"]), engine=self.engine)

    def _compile(self):
        graph = StateGraph(_GraphEnvelope)
        graph.add_node("load_package", self._envelope_node("load_package"))
        graph.add_node("load_memory", self._envelope_node("load_memory"))
        graph.add_node("route_skills", self._envelope_node("route_skills"))
        graph.add_node("plan", self._envelope_node("plan"))
        graph.add_node("direct_response", self._envelope_node("direct_response"))
        graph.add_node("react", self._envelope_node("react"))
        graph.add_node("dispatch_tools", self._envelope_node("dispatch_tools"))
        graph.add_node("validate", self._envelope_node("validate"))
        graph.add_node("finalize", self._envelope_node("finalize"))
        graph.set_entry_point("load_package")
        graph.add_edge("load_package", "load_memory")
        graph.add_edge("load_memory", "route_skills")
        graph.add_edge("route_skills", "plan")
        graph.add_conditional_edges("plan", self._after_plan, {"direct_response": "direct_response", "react": "react"})
        graph.add_conditional_edges("react", self._after_react, {"dispatch_tools": "dispatch_tools", "validate": "validate"})
        graph.add_edge("direct_response", "validate")
        graph.add_edge("dispatch_tools", "validate")
        graph.add_edge("validate", "finalize")
        graph.add_edge("finalize", END)
        return graph.compile(checkpointer=self.checkpointer)

    def _envelope_node(self, name: str):
        async def node(envelope: _GraphEnvelope) -> dict[str, Any]:
            state = self._from_json_state(envelope["state"])
            result = await self._run_node(name, state)
            return {"state": result.model_dump(mode="json")}
        return node

    def _after_plan(self, _envelope: _GraphEnvelope) -> str:
        return "react" if self.read_tools else "direct_response"

    def _after_react(self, envelope: _GraphEnvelope) -> str:
        state = self._from_json_state(envelope["state"])
        return "dispatch_tools" if state.tool_calls else "validate"

    async def _run_manually(self, state: AtlasAgentState) -> AtlasAgentState:
        for node in ("load_package", "load_memory", "route_skills", "plan"):
            state = await self._run_node(node, state)
        if self.read_tools:
            state = await self._run_node("react", state)
            if state.tool_calls:
                state = await self._run_node("dispatch_tools", state)
        else:
            state = await self._run_node("direct_response", state)
        state = await self._run_node("validate", state)
        return await self._run_node("finalize", state)

    async def _run_node(self, name: str, state: AtlasAgentState) -> AtlasAgentState:
        state = self._transition(state, "node.started", {"node": name})
        handler = getattr(self, f"_{name}")
        try:
            state = await handler(state)
        except Exception as exc:
            category = classify_error(exc)
            state = self._error(state, category, str(exc), node=name)
        return self._transition(state, "node.completed", {"node": name, "status": state.node_status.get(name, "failed")})

    async def _load_package(self, state: AtlasAgentState) -> AtlasAgentState:
        return self._update(state, node_status={**state.node_status, "load_package": "succeeded"})

    async def _load_memory(self, state: AtlasAgentState) -> AtlasAgentState:
        return self._update(state, node_status={**state.node_status, "load_memory": "succeeded"})

    async def _route_skills(self, state: AtlasAgentState) -> AtlasAgentState:
        capabilities = state.selected_capabilities or tuple(self.read_tools)
        return self._update(state, selected_capabilities=capabilities,
                            node_status={**state.node_status, "route_skills": "succeeded"})

    async def _plan(self, state: AtlasAgentState) -> AtlasAgentState:
        return self._update(
            state,
            plan={"template": "phase1", "max_tool_iterations": 1, "write_tools": "denied"},
            node_status={**state.node_status, "plan": "succeeded"},
            status=RuntimeStatus.RUNNING,
        )

    async def _direct_response(self, state: AtlasAgentState) -> AtlasAgentState:
        decision, state = await self._model_decision(state, "direct_response")
        if decision.tool_calls:
            state = self._policy_denial(state, decision.tool_calls[0], "direct response does not dispatch tools")
        return self._update(state, output=decision.response,
                            node_status={**state.node_status, "direct_response": "succeeded"})

    async def _react(self, state: AtlasAgentState) -> AtlasAgentState:
        decision, state = await self._model_decision(state, "react")
        return self._update(state, output=decision.response, tool_calls=decision.tool_calls,
                            node_status={**state.node_status, "react": "succeeded"})

    async def _dispatch_tools(self, state: AtlasAgentState) -> AtlasAgentState:
        for call in state.tool_calls[: state.plan.get("max_tool_iterations", 1)]:
            if call.access != ToolAccess.READ or call.name not in self.read_tools:
                state = self._policy_denial(state, call, "write and unknown tools are disabled in Phase 1")
                continue
            try:
                result, state = await self._tool_result(state, call)
                state = self._update(state, tool_results=(*state.tool_results, {"name": call.name, "result": result}))
                state = self._transition(state, "tool.completed", {"name": call.name})
            except Exception as exc:
                state = self._error(state, classify_error(exc), str(exc), node="dispatch_tools", tool=call.name)
        return self._update(state, node_status={**state.node_status, "dispatch_tools": "succeeded"})

    async def _validate(self, state: AtlasAgentState) -> AtlasAgentState:
        if not state.output and not state.errors:
            state = self._error(state, RuntimeErrorCategory.PLAN, "graph produced no response", node="validate")
        return self._update(state, node_status={**state.node_status, "validate": "succeeded"})

    async def _finalize(self, state: AtlasAgentState) -> AtlasAgentState:
        status = RuntimeStatus.FAILED if state.errors else RuntimeStatus.SUCCEEDED
        state = self._update(state, status=status, node_status={**state.node_status, "finalize": status.value})
        return self._transition(state, "run.completed" if status is RuntimeStatus.SUCCEEDED else "run.failed", {"status": status.value})

    async def _model_decision(self, state: AtlasAgentState, node: str) -> tuple[RuntimeDecision, AtlasAgentState]:
        for attempt in range(1, self.retry_policy.max_attempts + 1):
            try:
                value = self.model(state)
                if inspect.isawaitable(value):
                    value = await value
                if isinstance(value, RuntimeDecision):
                    return value, state
                if isinstance(value, str):
                    return RuntimeDecision(response=value), state
                return RuntimeDecision.model_validate(value), state
            except Exception as exc:
                category = classify_error(exc)
                if not self.retry_policy.allows(category, attempt=attempt, read_only=True, side_effects_started=state.side_effects_started):
                    raise
                delay = self.retry_policy.delay_for(attempt)
                state = self._retry(state, node, attempt, delay, str(exc))
                if delay:
                    await asyncio.sleep(delay)
        raise RuntimeFailure(RuntimeErrorCategory.TERMINAL, "model retry loop exhausted")

    async def _tool_result(self, state: AtlasAgentState, call: RuntimeToolCall) -> tuple[Any, AtlasAgentState]:
        for attempt in range(1, self.retry_policy.max_attempts + 1):
            try:
                value = self.read_tools[call.name](call.arguments)
                return (await value if inspect.isawaitable(value) else value), state
            except Exception as exc:
                category = classify_error(exc)
                if not self.retry_policy.allows(category, attempt=attempt, read_only=True, side_effects_started=state.side_effects_started):
                    raise
                delay = self.retry_policy.delay_for(attempt)
                state = self._retry(state, call.name, attempt, delay, str(exc))
                if delay:
                    await asyncio.sleep(delay)
        raise RuntimeFailure(RuntimeErrorCategory.TERMINAL, "tool retry loop exhausted")

    def _policy_denial(self, state: AtlasAgentState, call: RuntimeToolCall, message: str) -> AtlasAgentState:
        state = self._transition(state, "tool.denied", {"name": call.name, "reason": message})
        return self._error(state, RuntimeErrorCategory.PERMISSION, message, tool=call.name)

    def _retry(self, state: AtlasAgentState, target: str, attempt: int, delay: float, message: str) -> AtlasAgentState:
        counters = {**state.retry_counters, target: attempt}
        state = self._update(state, retry_counters=counters)
        return self._transition(state, "retry.scheduled", {"target": target, "attempt": attempt, "delay_seconds": delay, "error": message})

    def _error(self, state: AtlasAgentState, category: RuntimeErrorCategory, message: str, **context: str) -> AtlasAgentState:
        error = {"category": category.value, "message": message, **context}
        return self._update(state, errors=(*state.errors, error))

    def _transition(self, state: AtlasAgentState, event_type: str, payload: dict[str, Any]) -> AtlasAgentState:
        return self._update(state, transitions=(*state.transitions, RuntimeTransition(event_type=event_type, payload=payload)))

    @staticmethod
    def _update(state: AtlasAgentState, **changes: Any) -> AtlasAgentState:
        data = state.model_dump()
        data.update(changes)
        return AtlasAgentState.model_validate(data)

    @staticmethod
    def _from_json_state(value: dict[str, Any]) -> AtlasAgentState:
        return AtlasAgentState.model_validate_json(json.dumps(value))


class LegacyGraphShim(RuntimePhaseOneGraph):
    """Deterministic fallback used while LangGraph is absent or the flag is disabled."""

    def __init__(self, model: ModelInvoker, *, read_tools: dict[str, ToolInvoker] | None = None,
                 retry_policy: RetryPolicy | None = None) -> None:
        super().__init__(model, read_tools=read_tools, retry_policy=retry_policy, prefer_langgraph=False)
