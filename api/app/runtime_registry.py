"""Versioned graph-template registry for the runtime migration.

The registry is intentionally small in Phase 1.  Its job is to give every
entry point the same graph construction seam before planning, subagent, and
builder templates are introduced in later phases.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .runtime_graph import ModelInvoker, RuntimePhaseOneGraph


class RuntimeGraphRegistry:
    PHASE_ONE_REACT = "phase1-react"

    def __init__(self) -> None:
        self._templates: dict[str, Callable[[ModelInvoker, Any | None, Any | None], RuntimePhaseOneGraph]] = {
            self.PHASE_ONE_REACT: lambda model, checkpointer, memory_loader: RuntimePhaseOneGraph(
                model,
                prefer_langgraph=True,
                checkpointer=checkpointer,
                memory_loader=memory_loader,
            ),
        }

    def create(self, template: str, model: ModelInvoker, *, checkpointer: Any | None = None,
               memory_loader: Any | None = None) -> RuntimePhaseOneGraph:
        factory = self._templates.get(template)
        if factory is None:
            raise ValueError(f"unknown runtime graph template: {template}")
        return factory(model, checkpointer, memory_loader)


runtime_graph_registry = RuntimeGraphRegistry()
