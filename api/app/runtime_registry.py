"""Versioned graph-template registry for the runtime migration.

The registry is intentionally small in Phase 1.  Its job is to give every
entry point the same graph construction seam before planning, subagent, and
builder templates are introduced in later phases.
"""

from __future__ import annotations

from typing import Any

from .adaptive_runtime import PlanExecuteReviewGraph, PlanExecutor, PlannerInvoker, ReviewerInvoker
from .runtime_contract import ExecutionStrategy
from .runtime_graph import MemoryLoader, ModelInvoker, RuntimePhaseOneGraph, SkillSelector, ToolInvoker


class RuntimeGraphRegistry:
    PHASE_ONE_REACT = "phase1-react"
    REACT_V1 = "react-v1"
    PLAN_EXECUTE_REVIEW_V1 = "plan-execute-review-v1"
    MULTI_AGENT_PLAN_EXECUTE_REVIEW_V1 = "multi-agent-plan-execute-review-v1"

    def __init__(self) -> None:
        self._templates = frozenset({
            self.PHASE_ONE_REACT,
            self.REACT_V1,
            self.PLAN_EXECUTE_REVIEW_V1,
            self.MULTI_AGENT_PLAN_EXECUTE_REVIEW_V1,
        })

    def create(
        self,
        template: str,
        model: ModelInvoker | None,
        *,
        checkpointer: Any | None = None,
        memory_loader: MemoryLoader | None = None,
        skill_selector: SkillSelector | None = None,
        read_tools: dict[str, ToolInvoker] | None = None,
        planner: PlannerInvoker | None = None,
        executor: PlanExecutor | None = None,
        reviewer: ReviewerInvoker | None = None,
    ) -> RuntimePhaseOneGraph | PlanExecuteReviewGraph:
        if template not in self._templates:
            raise ValueError(f"unknown runtime graph template: {template}")
        if template in {self.PHASE_ONE_REACT, self.REACT_V1}:
            if model is None:
                raise ValueError("ReAct template requires a model invoker")
            return RuntimePhaseOneGraph(
                model,
                read_tools=read_tools or {},
                prefer_langgraph=True,
                checkpointer=checkpointer,
                memory_loader=memory_loader,
                skill_selector=skill_selector,
            )
        if planner is None or executor is None or reviewer is None:
            raise ValueError("Plan-Execute-Review template requires planner, executor, and reviewer")
        strategy = (
            ExecutionStrategy.PLAN_EXECUTE_REVIEW
            if template == self.PLAN_EXECUTE_REVIEW_V1
            else ExecutionStrategy.MULTI_AGENT_PLAN_EXECUTE_REVIEW
        )
        return PlanExecuteReviewGraph(
            planner,
            executor,
            reviewer,
            strategy=strategy,
            checkpointer=checkpointer,
            prefer_langgraph=True,
        )


runtime_graph_registry = RuntimeGraphRegistry()
