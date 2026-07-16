"""DAG Execution Engine — parse, validate, and run DAG graphs.

Components:
- DAGParser: graph → adjacency list, cycle detection, topological sort
- StateManager: shared State dict with input/output key resolution
- DAGRunner: orchestrates node execution with Langfuse tracing
"""

import asyncio
import json
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set, Tuple

from app.core.nodes.base import NodeRegistry
import app.core.nodes.core  # noqa: F401 — trigger registration
import app.core.nodes.extensions  # noqa: F401 — trigger registration
import app.core.nodes.agent_node  # noqa: F401 — trigger registration
from app.core.observability import (
    _get_langfuse,
    create_trace_url,
    set_active_span,
    reset_active_span,
    begin_usage_accumulation,
    record_run_summary,
)


@dataclass
class DAGNode:
    id: str
    node_type: str
    config: Dict[str, Any] = field(default_factory=dict)


@dataclass
class DAGEdge:
    id: str
    source: str
    target: str
    data: Dict[str, Any] = field(default_factory=dict)


class DAGParser:
    """Parse DAG graph JSON and produce execution-ready topological order."""

    def __init__(self, graph_json: str):
        raw = json.loads(graph_json) if isinstance(graph_json, str) else graph_json
        self.nodes: List[DAGNode] = []
        for n in raw.get("nodes", []):
            self.nodes.append(DAGNode(
                id=n["id"],
                node_type=n.get("type", n.get("node_type", "")),
                config=n.get("config", {}),
            ))
        self.edges: List[DAGEdge] = []
        for e in raw.get("edges", []):
            self.edges.append(DAGEdge(
                id=e.get("id", f"{e.get('source', '')}-{e.get('target', '')}"),
                source=e["source"],
                target=e["target"],
                data=e.get("data", {}),
            ))
        self._node_map: Dict[str, DAGNode] = {n.id: n for n in self.nodes}
        self._adjacency: Dict[str, List[str]] = defaultdict(list)
        self._in_degree: Dict[str, int] = defaultdict(int)
        self._outgoing_edges: Dict[str, List[DAGEdge]] = defaultdict(list)
        self._incoming_edges: Dict[str, List[DAGEdge]] = defaultdict(list)
        self._build_graph()

    def _build_graph(self):
        for edge in self.edges:
            self._adjacency[edge.source].append(edge.target)
            self._in_degree[edge.target] += 1
            self._in_degree.setdefault(edge.source, 0)
            self._outgoing_edges[edge.source].append(edge)
            self._incoming_edges[edge.target].append(edge)
        for node in self.nodes:
            self._in_degree.setdefault(node.id, 0)

    def has_cycles(self) -> bool:
        """Kahn's algorithm — returns True if graph contains a cycle."""
        in_deg = dict(self._in_degree)
        queue = deque([nid for nid, deg in in_deg.items() if deg == 0])
        visited = 0
        while queue:
            node_id = queue.popleft()
            visited += 1
            for neighbor in self._adjacency.get(node_id, []):
                in_deg[neighbor] -= 1
                if in_deg[neighbor] == 0:
                    queue.append(neighbor)
        return visited != len(self._node_map)

    def topological_sort(self) -> List[List[str]]:
        """Kahn's algorithm → list of levels, each level is a list of parallel-executable node IDs."""
        in_deg = dict(self._in_degree)
        queue = deque([nid for nid, deg in in_deg.items() if deg == 0])
        levels: List[List[str]] = []
        processed = set()

        while queue:
            level = []
            for _ in range(len(queue)):
                node_id = queue.popleft()
                if node_id in processed:
                    continue
                processed.add(node_id)
                if node_id in self._node_map:
                    level.append(node_id)
                for neighbor in self._adjacency.get(node_id, []):
                    in_deg[neighbor] -= 1
                    if in_deg[neighbor] == 0:
                        queue.append(neighbor)
            if level:
                levels.append(level)

        return levels

    def get_incoming_edges(self, node_id: str) -> List[DAGEdge]:
        return self._incoming_edges.get(node_id, [])

    def get_outgoing_edges(self, node_id: str) -> List[DAGEdge]:
        return self._outgoing_edges.get(node_id, [])

    def get_downstream_nodes(self, node_id: str, branch: Optional[str] = None) -> List[str]:
        """Get downstream node IDs, optionally filtered by branch label on edge data."""
        result = []
        for edge in self._outgoing_edges.get(node_id, []):
            edge_branch = edge.data.get("branch") if edge.data else None
            if branch is None or edge_branch is None or edge_branch == branch:
                result.append(edge.target)
        return result


class StateManager:
    """Shared State dict for inter-node data flow.

    Each node declares input_keys (what it reads) and output_keys (what it writes).
    The StateManager resolves inputs from State and writes outputs back to State.
    """

    def __init__(self, initial: Optional[Dict[str, Any]] = None):
        self._state: Dict[str, Any] = dict(initial or {})
        self._snapshots: List[Dict[str, Any]] = []

    def resolve_inputs(self, node_id: str, input_keys: List[str]) -> Dict[str, Any]:
        """Resolve a node's declared input_keys from current State."""
        inputs = {}
        for key in input_keys:
            if key in self._state:
                inputs[key] = self._state[key]
        inputs["_node_id"] = node_id
        return inputs

    def write_outputs(self, node_id: str, outputs: Dict[str, Any]):
        """Write a node's output key-values into State."""
        for key, value in outputs.items():
            if not key.startswith("_"):
                self._state[key] = value
        self._state[f"_last_output_{node_id}"] = outputs

    def snapshot(self):
        """Save current state snapshot (used on error for debugging)."""
        self._snapshots.append(dict(self._state))

    def get_state(self) -> Dict[str, Any]:
        return dict(self._state)

    @property
    def last_snapshot(self) -> Optional[Dict[str, Any]]:
        return self._snapshots[-1] if self._snapshots else None


@dataclass
class NodeExecutionResult:
    node_id: str
    node_type: str
    status: str  # "completed", "skipped", "error"
    output: Dict[str, Any] = field(default_factory=dict)
    duration_ms: float = 0
    error: Optional[str] = None
    trace_url: Optional[str] = None
    tokens: int = 0


@dataclass
class DAGExecutionResult:
    results: List[NodeExecutionResult] = field(default_factory=list)
    final_output: Any = None
    total_duration_ms: float = 0
    state_snapshot: Dict[str, Any] = field(default_factory=dict)
    trace_url: Optional[str] = None
    trace_id: Optional[str] = None
    token_input: int = 0
    token_output: int = 0
    error: Optional[str] = None

    @property
    def success(self) -> bool:
        return self.error is None


class DAGRunner:
    """Orchestrate DAG execution with Langfuse tracing.

    - Topologically sorts nodes
    - Executes each level in parallel (asyncio.gather)
    - Handles conditional branching (CNode _active_path)
    - Wraps each node execution in a Langfuse child span
    - Error stops downstream, preserves State snapshot
    """

    def __init__(self, parser: DAGParser, state_manager: Optional[StateManager] = None):
        self.parser = parser
        self.state = state_manager or StateManager()
        self._active_path: Optional[str] = None
        self._results: List[NodeExecutionResult] = []
        self._root_trace_id: Optional[str] = None
        self._root_span: Any = None
        self._on_event: Optional[Callable[[dict], Awaitable[None]]] = None

    def _nodes_feeding_agents(self) -> Set[str]:
        """M nodes that feed directly into an agent node are skipped: the agent
        runs the model internally, so executing the M node would be a redundant
        (and unstreamed) LLM call. Mirrors the skip logic the chat WS used to do
        inline."""
        agent_node_ids = {
            n_id for n_id, n in self.parser._node_map.items() if n.node_type == "agent"
        }
        skip: Set[str] = set()
        for n_id, n in self.parser._node_map.items():
            if n.node_type == "m":
                for edge in self.parser.get_outgoing_edges(n_id):
                    if edge.target in agent_node_ids:
                        skip.add(n_id)
                        break
        return skip

    async def _emit(self, event: dict) -> None:
        if self._on_event is not None:
            try:
                await self._on_event(event)
            except Exception:
                pass

    async def run(
        self,
        user_input: str,
        agent_id: int = 0,
        on_event: Optional[Callable[[dict], Awaitable[None]]] = None,
        dag_version: int = 0,
    ) -> DAGExecutionResult:
        """Execute the full DAG.

        When ``on_event`` is provided the runner emits per-node lifecycle events
        (node_start / node_complete / node_error / node_skipped) plus any
        streaming events nodes push through the ``_event_hook`` in state. This
        lets the chat WebSocket delegate execution here while preserving its
        message contract.
        """
        t_start = time.time()
        self.state._state["_raw_input"] = user_input
        self.state._state["user_query"] = user_input
        self._on_event = on_event
        usage_acc = begin_usage_accumulation()

        lf = _get_langfuse()
        root_span = None
        self._root_trace_id = None
        self._root_span = None

        if lf:
            try:
                self._root_trace_id = lf.create_trace_id()
                root_span = lf.start_observation(
                    trace_context={"trace_id": self._root_trace_id},
                    name=f"dag-execution-agent-{agent_id}",
                    input={"user_input": user_input},
                )
                self._root_span = root_span
            except Exception:
                pass

        run_error: Optional[str] = None
        skip_nodes = self._nodes_feeding_agents()

        try:
            levels = self.parser.topological_sort()
            for level in levels:
                tasks = []
                for node_id in level:
                    if node_id in skip_nodes or self._should_skip(node_id):
                        tasks.append(self._skip_node(node_id))
                    else:
                        tasks.append(self._execute_node(node_id, agent_id))
                await asyncio.gather(*tasks)

            final_output = self.state._state.get("final_output") or self.state._state.get("raw_response", "")
        except Exception as exc:
            run_error = str(exc)
            self.state.snapshot()
            final_output = None

        elapsed = round((time.time() - t_start) * 1000)
        token_input = sum(u[0] for u in usage_acc)
        token_output = sum(u[1] for u in usage_acc)
        executed = [r for r in self._results if r.status != "skipped"]
        node_count = len(executed)
        status = "error" if run_error else "completed"

        result = DAGExecutionResult(
            results=self._results,
            final_output=final_output,
            total_duration_ms=elapsed,
            state_snapshot=(self.state.last_snapshot or self.state.get_state()) if run_error else self.state.get_state(),
            trace_url=create_trace_url(self._root_trace_id) if self._root_trace_id else None,
            trace_id=self._root_trace_id or None,
            token_input=token_input,
            token_output=token_output,
            error=run_error,
        )

        if root_span and lf:
            try:
                out = {"error": run_error} if run_error else {"final_output": str(final_output)[:500]}
                out["elapsed_ms"] = elapsed
                root_span.update(output=out)
                root_span.end()
                lf.flush()
            except Exception as exc:
                from app.core.observability import _note_export_failure
                _note_export_failure(exc)

        record_run_summary(
            agent_id=agent_id,
            trace_id=self._root_trace_id or "",
            status=status,
            total_duration_ms=elapsed,
            token_input=token_input,
            token_output=token_output,
            node_count=node_count,
            error=run_error or "",
            dag_version=dag_version,
        )

        return result

    def _should_skip(self, node_id: str) -> bool:
        """Check if node should be skipped due to inactive conditional branch."""
        incoming = self.parser.get_incoming_edges(node_id)
        if not incoming:
            return False
        for edge in incoming:
            branch = edge.data.get("branch") if edge.data else None
            if branch is not None and self._active_path is not None and branch != self._active_path:
                return True
        return False

    async def _execute_node(self, node_id: str, agent_id: int) -> None:
        node = self.parser._node_map.get(node_id)
        if not node:
            self._results.append(NodeExecutionResult(
                node_id=node_id, node_type="unknown", status="error",
                error=f"Node {node_id} not found in graph",
            ))
            await self._emit({"type": "node_error", "node_id": node_id, "node_type": "unknown",
                              "error": f"Node {node_id} not found in graph"})
            return

        node_cls = NodeRegistry.get(node.node_type)
        if not node_cls:
            self._results.append(NodeExecutionResult(
                node_id=node_id, node_type=node.node_type, status="error",
                error=f"Unknown node type: {node.node_type}",
            ))
            await self._emit({"type": "node_error", "node_id": node_id, "node_type": node.node_type,
                              "error": f"Unknown node type: {node.node_type}"})
            return

        await self._emit({"type": "node_start", "node_id": node_id,
                          "node_type": node.node_type, "label": node.node_type})

        t_start = time.time()
        lf = _get_langfuse()
        span = None
        trace_url = None

        if lf and self._root_span is not None:
            try:
                # SDK 4.x: nest under the root by creating the child observation
                # FROM the parent span object. There is no `parent_observation_id`
                # kwarg on Langfuse.start_observation — passing one raises
                # TypeError and (previously) silently dropped every node span.
                span = self._root_span.start_observation(
                    name=f"node-{node_id}-{node.node_type}",
                    input={"node_type": node.node_type, "config": node.config},
                )
            except Exception:
                pass

        # Hand the event hook (for token streaming) and the node span (for LLM
        # generation observations) down to the node implementation / adapter.
        # contextvars are copied per task, so this is safe under asyncio.gather.
        self.state._state["_event_hook"] = self._on_event
        span_token = set_active_span(span)
        try:
            instance = node_cls(config=node.config)
            inputs = self.state.resolve_inputs(node_id, node_cls.input_keys)
            outputs = await instance.run(inputs, self.state.get_state())
            self.state.write_outputs(node_id, outputs)

            # Track active path for conditional branching
            if "_active_path" in outputs:
                self._active_path = outputs["_active_path"]

            elapsed = round((time.time() - t_start) * 1000)
            output_keys = [k for k in outputs.keys() if not k.startswith("_")]

            if span and lf:
                try:
                    span.update(output={"status": "completed", "elapsed_ms": elapsed, "output_keys": output_keys})
                    span.end()
                    trace_url = create_trace_url(self._root_trace_id) if self._root_trace_id else None
                except Exception:
                    pass

            self._results.append(NodeExecutionResult(
                node_id=node_id, node_type=node.node_type, status="completed",
                output=outputs, duration_ms=elapsed, trace_url=trace_url,
            ))
            await self._emit({"type": "node_complete", "node_id": node_id,
                              "node_type": node.node_type, "output_keys": output_keys})

        except Exception as exc:
            elapsed = round((time.time() - t_start) * 1000)

            if span and lf:
                try:
                    span.update(output={"status": "error", "error": str(exc), "elapsed_ms": elapsed})
                    span.end()
                except Exception:
                    pass

            self._results.append(NodeExecutionResult(
                node_id=node_id, node_type=node.node_type, status="error",
                error=str(exc), duration_ms=elapsed,
            ))
            await self._emit({"type": "node_error", "node_id": node_id,
                              "node_type": node.node_type, "error": str(exc)})
            self.state.snapshot()
            raise
        finally:
            reset_active_span(span_token)
            self.state._state.pop("_event_hook", None)

    async def _skip_node(self, node_id: str) -> None:
        node = self.parser._node_map.get(node_id)
        node_type = node.node_type if node else "unknown"
        self._results.append(NodeExecutionResult(
            node_id=node_id, node_type=node_type, status="skipped",
        ))
        await self._emit({"type": "node_skipped", "node_id": node_id, "node_type": node_type})


async def run_dag(graph_json: str, user_input: str, agent_id: int = 0, initial_state: Optional[Dict[str, Any]] = None) -> DAGExecutionResult:
    """Convenience function: parse + run a DAG graph in one call."""
    parser = DAGParser(graph_json)
    if parser.has_cycles():
        return DAGExecutionResult(error="DAG contains cycles")

    state = StateManager(initial=initial_state)
    runner = DAGRunner(parser, state_manager=state)
    return await runner.run(user_input, agent_id=agent_id)
