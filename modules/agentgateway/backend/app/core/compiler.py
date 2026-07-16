"""Agent Compiler — compiles DAG graph JSON into a standalone AgentScope runtime agent."""

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from app.core.dag_executor import DAGParser, StateManager, DAGRunner, DAGExecutionResult
from app.core.nodes.base import NodeRegistry


@dataclass
class CompiledAgent:
    """A compiled DAG agent that can run independently of the workbench.

    Exposes the same `async run(user_input) -> final_output` interface
    as the development-mode DAGRunner but is self-contained.
    """

    name: str
    graph_json: str
    version: int
    node_configs: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    async def run(self, user_input: str, agent_id: int = 0) -> DAGExecutionResult:
        """Execute the compiled agent."""
        return await run_dag(self.graph_json, user_input, agent_id=agent_id)


@dataclass
class CompileResult:
    agent: Optional[CompiledAgent] = None
    errors: List[str] = field(default_factory=list)
    version: int = 0

    @property
    def success(self) -> bool:
        return len(self.errors) == 0 and self.agent is not None


class Compiler:
    """Compiles DAG graph JSON → CompiledAgent for publishing."""

    @staticmethod
    def compile(graph_json: str, agent_name: str, version: int = 1) -> CompileResult:
        errors: List[str] = []

        try:
            parser = DAGParser(graph_json)
        except Exception as exc:
            return CompileResult(errors=[f"Invalid graph JSON: {str(exc)}"])

        if parser.has_cycles():
            return CompileResult(errors=["DAG contains cycles, cannot compile"])

        # Verify all node types are registered
        node_configs: Dict[str, Dict[str, Any]] = {}
        for node in parser.nodes:
            node_cls = NodeRegistry.get(node.node_type)
            if not node_cls:
                errors.append(f"Unknown node type '{node.node_type}' for node '{node.id}'")
                continue
            node_configs[node.id] = node.config

        if errors:
            return CompileResult(errors=errors)

        agent = CompiledAgent(
            name=agent_name,
            graph_json=graph_json,
            version=version,
            node_configs=node_configs,
        )

        return CompileResult(agent=agent, version=version)


# Re-export for convenience
from app.core.dag_executor import run_dag  # noqa: E402, F811
