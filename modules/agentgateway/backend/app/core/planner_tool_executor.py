"""Planner Tool Executor — wrapper over planner_agent_loop.py's ReAct logic.

This is a thin adapter that exposes the existing ReAct loop as a step executor.
The original planner_agent_loop.py is NOT renamed (per design D6) — this wrapper
imports from it and adapts the interface.

Only used for local exploration steps where the model needs to autonomously
decide which tools to call (e.g., inspect_missing_context).
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from .planner_loop import StepResult
from .planner_plan import PlanStep
from .planner_steps import ExecutionContext

logger = logging.getLogger(__name__)


async def execute_tool_react_step(
    step: PlanStep,
    context: ExecutionContext,
    max_iterations: int = 5,
) -> StepResult:
    """Execute a Tool/ReAct step using the existing planner_agent_loop machinery.

    This is the handler passed to ToolReactExecutor. It bridges the new
    Plan+Loop interface with the existing ReAct implementation.

    Currently a placeholder — full integration with planner_agent_loop's
    run_agentic_turn() will be wired in §5 (ws.py integration phase).
    """
    # ToolReactExecutor is not yet wired to real ReAct logic.
    # Explicit failure prevents silent placeholder success.
    logger.warning(f"ToolReact step {step.id}: executor not wired, failing explicitly")
    return StepResult(
        status="failed",
        error="ToolReactExecutor is not wired yet — ReAct integration pending",
    )
