"""Planner Step definitions — step_id constants, ExecutionContext, and step→executor mapping.

This module defines the vocabulary of plan steps and maps each to its
executor type and target service.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from .planner_plan import ExecutorType


# ─── Step ID Constants ─────────────────────────────────────────────────────────

STEP_UNDERSTAND_REQUIREMENT = "understand_requirement"
STEP_COLLECT_CONTEXT = "collect_context"
STEP_RECALL_MEMORY = "recall_memory"
STEP_RETRIEVE_EXPERTS = "retrieve_experts"
STEP_MATCH_CAPABILITIES = "match_capabilities"
STEP_DESIGN_ARCHITECTURE = "design_architecture"
STEP_CONFIRM_PROPOSAL = "confirm_proposal"
STEP_GENERATE_DRAFT = "generate_draft"
STEP_COMPILE_DRAFT = "compile_draft"
STEP_REPAIR_PREFIX = "repair_"
STEP_APPLY_TO_WORKBENCH = "apply_to_workbench"
STEP_INSPECT_MISSING_CONTEXT = "inspect_missing_context"


# ─── Step → Executor Mapping ──────────────────────────────────────────────────

STEP_EXECUTOR_MAP: dict[str, ExecutorType] = {
    STEP_UNDERSTAND_REQUIREMENT: ExecutorType.llm,
    STEP_COLLECT_CONTEXT: ExecutorType.deterministic,
    STEP_RECALL_MEMORY: ExecutorType.deterministic,
    STEP_RETRIEVE_EXPERTS: ExecutorType.deterministic,
    STEP_MATCH_CAPABILITIES: ExecutorType.deterministic,
    STEP_DESIGN_ARCHITECTURE: ExecutorType.llm,
    STEP_CONFIRM_PROPOSAL: ExecutorType.user_confirmation,
    STEP_GENERATE_DRAFT: ExecutorType.deterministic,
    STEP_COMPILE_DRAFT: ExecutorType.deterministic,
    STEP_APPLY_TO_WORKBENCH: ExecutorType.deterministic,
    STEP_INSPECT_MISSING_CONTEXT: ExecutorType.tool_react,
}


def get_executor_type(step_id: str) -> ExecutorType:
    """Resolve executor type for a step_id.

    For repair steps (prefixed with 'repair_'), defaults to deterministic.
    For unknown steps, defaults to deterministic.
    """
    if step_id.startswith(STEP_REPAIR_PREFIX):
        return ExecutorType.deterministic
    return STEP_EXECUTOR_MAP.get(step_id, ExecutorType.deterministic)


# ─── Execution Context ─────────────────────────────────────────────────────────

@dataclass
class ExecutionContext:
    """Context passed to step executors for the current execution."""
    conversation_id: str = ""
    run_id: str = ""
    user_input: str = ""
    planning_context: Optional[dict[str, Any]] = None
    memory_context: Optional[Any] = None
    session_data: Optional[dict[str, Any]] = None
    model_cfg: Optional[dict[str, Any]] = None
    memory: Optional[dict[str, Any]] = None
    system_prompt: str = ""
    # Accumulated outputs from prior steps (step_id → outputs dict)
    prior_outputs: dict[str, dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def from_plan(cls, plan, session_data: Optional[dict] = None) -> "ExecutionContext":
        """Build context from plan's completed step outputs."""
        prior = {}
        for step in plan.steps:
            if step.outputs:
                prior[step.id] = step.outputs
        return cls(
            prior_outputs=prior,
            session_data=session_data,
        )
