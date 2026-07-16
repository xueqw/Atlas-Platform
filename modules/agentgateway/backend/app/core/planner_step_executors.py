"""Planner Step Executors — four executor types implementing the BaseExecutor interface.

- DeterministicExecutor: direct service calls, no LLM
- LLMExecutor: single LLM call with structured output
- UserConfirmationExecutor: waiting for user interaction
- ToolReactExecutor: multi-turn tool calling (wrapper over planner_agent_loop)
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any, Optional

from .planner_loop import StepResult
from .planner_plan import Plan, PlanStep, StepStatus
from .planner_steps import ExecutionContext

logger = logging.getLogger(__name__)


# ─── Base Executor ─────────────────────────────────────────────────────────────

class BaseExecutor(ABC):
    """Abstract interface for all step executors."""

    @abstractmethod
    async def execute(self, step: PlanStep, context: ExecutionContext) -> StepResult:
        """Execute a plan step and return the result."""
        ...


# ─── Deterministic Executor ────────────────────────────────────────────────────

class DeterministicExecutor(BaseExecutor):
    """Executor for deterministic service calls — no LLM involved.

    Handles: collect_context, recall_memory, retrieve_experts,
    match_capabilities, compile_draft, generate_draft, apply_to_workbench.
    """

    def __init__(self, services: Optional[dict[str, Any]] = None):
        """Services dict maps step_id → callable service function."""
        self._services = services or {}

    async def execute(self, step: PlanStep, context: ExecutionContext) -> StepResult:
        handler = self._services.get(step.id)
        if handler is None:
            # Fallback: try a generic dispatch based on step.id
            handler = self._services.get("_default")
        if handler is None:
            logger.warning(f"No handler registered for deterministic step: {step.id}")
            return StepResult(
                status="success",
                outputs={"warning": f"No handler for step {step.id}, skipped"},
            )

        try:
            # Handler can be sync or async
            import asyncio
            if asyncio.iscoroutinefunction(handler):
                result = await handler(step, context)
            else:
                result = handler(step, context)

            if isinstance(result, StepResult):
                return result
            # If handler returns a dict, wrap it
            if isinstance(result, dict):
                return StepResult(status="success", outputs=result)
            return StepResult(status="success", outputs={"result": result})
        except Exception as exc:
            logger.exception(f"Deterministic step {step.id} failed")
            return StepResult(
                status="failed",
                error=str(exc),
                can_auto_repair=step.id == "compile_draft",
                repair_config={
                    "title": f"修复: {step.title}",
                    "executor_type": "deterministic",
                    "inputs": {"error": str(exc)},
                } if step.id == "compile_draft" else None,
            )


# ─── LLM Executor ─────────────────────────────────────────────────────────────

class LLMExecutor(BaseExecutor):
    """Executor for single LLM calls with structured output.

    Handles: understand_requirement, design_architecture, generate_proposal.
    Supports retry (max 2 attempts) on parse failures.
    """

    MAX_RETRIES = 2

    def __init__(self, llm_handler=None):
        """llm_handler: async callable(step, context) -> dict | StepResult."""
        self._handler = llm_handler

    async def execute(self, step: PlanStep, context: ExecutionContext) -> StepResult:
        if self._handler is None:
            logger.warning(f"No LLM handler registered for step: {step.id}")
            return StepResult(
                status="success",
                outputs={"warning": f"No LLM handler for step {step.id}, placeholder"},
            )

        last_error = None
        for attempt in range(self.MAX_RETRIES):
            try:
                import asyncio
                if asyncio.iscoroutinefunction(self._handler):
                    result = await self._handler(step, context)
                else:
                    result = self._handler(step, context)

                if isinstance(result, StepResult):
                    return result
                if isinstance(result, dict):
                    return StepResult(status="success", outputs=result)
                return StepResult(status="success", outputs={"result": result})
            except Exception as exc:
                last_error = str(exc)
                logger.warning(f"LLM step {step.id} attempt {attempt+1} failed: {exc}")
                continue

        return StepResult(
            status="failed",
            error=f"LLM step failed after {self.MAX_RETRIES} attempts: {last_error}",
        )


# ─── User Confirmation Executor ───────────────────────────────────────────────

class UserConfirmationExecutor(BaseExecutor):
    """Executor for steps that require user interaction.

    Does not execute any automated logic — sets step to waiting_user
    and returns immediately. The loop will pause and wait for user response.
    """

    async def execute(self, step: PlanStep, context: ExecutionContext) -> StepResult:
        # Ensure confirmation is set
        if not step.confirmation:
            logger.warning(f"UserConfirmation step {step.id} has no confirmation request")
            return StepResult(
                status="failed",
                error="Missing confirmation request on user_confirmation step",
            )

        # Return waiting_user — loop will pause
        return StepResult(
            status="waiting_user",
            outputs={"confirmation_request_id": step.confirmation.request_id},
        )


# ─── Tool/ReAct Executor ──────────────────────────────────────────────────────

class ToolReactExecutor(BaseExecutor):
    """Executor for multi-turn tool calling loops.

    Wraps the existing planner_agent_loop.py ReAct implementation.
    Only used for local exploration steps where the model needs to
    autonomously decide which tools to call.
    """

    MAX_REACT_ITERATIONS = 5

    def __init__(self, react_handler=None):
        """react_handler: async callable(step, context, max_iterations) -> dict | StepResult."""
        self._handler = react_handler

    async def execute(self, step: PlanStep, context: ExecutionContext) -> StepResult:
        if self._handler is None:
            logger.warning(f"No ReAct handler registered for step: {step.id}")
            return StepResult(
                status="success",
                outputs={"warning": f"No ReAct handler for step {step.id}, skipped"},
            )

        try:
            import asyncio
            if asyncio.iscoroutinefunction(self._handler):
                result = await self._handler(step, context, self.MAX_REACT_ITERATIONS)
            else:
                result = self._handler(step, context, self.MAX_REACT_ITERATIONS)

            if isinstance(result, StepResult):
                return result
            if isinstance(result, dict):
                return StepResult(status="success", outputs=result)
            return StepResult(status="success", outputs={"result": result})
        except Exception as exc:
            logger.exception(f"ToolReact step {step.id} failed")
            # ReAct timeout is not a hard failure — return what we have
            return StepResult(
                status="success",
                outputs={"partial": True, "error_info": str(exc)},
            )


# ─── Executor Dispatcher ──────────────────────────────────────────────────────

class ExecutorDispatcher:
    """Dispatches step execution to the appropriate executor based on executor_type.

    Implements the StepExecutorDispatcher protocol expected by the Loop Runner.
    """

    def __init__(
        self,
        deterministic: Optional[DeterministicExecutor] = None,
        llm: Optional[LLMExecutor] = None,
        user_confirmation: Optional[UserConfirmationExecutor] = None,
        tool_react: Optional[ToolReactExecutor] = None,
    ):
        from .planner_plan import ExecutorType
        self._executors = {
            ExecutorType.deterministic: deterministic or DeterministicExecutor(),
            ExecutorType.llm: llm or LLMExecutor(),
            ExecutorType.user_confirmation: user_confirmation or UserConfirmationExecutor(),
            ExecutorType.tool_react: tool_react or ToolReactExecutor(),
        }

    async def execute(self, step: PlanStep, plan: Plan, session: Any) -> StepResult:
        """Dispatch to the correct executor based on step.executor_type."""
        executor = self._executors.get(step.executor_type)
        if executor is None:
            logger.error(f"No executor for type: {step.executor_type}")
            return StepResult(status="failed", error=f"Unknown executor type: {step.executor_type}")

        # Build execution context from plan state + any session_data attached
        context = ExecutionContext.from_plan(plan)
        # If session carries extra context (set by ws.py), propagate it
        if hasattr(session, "_exec_context") and session._exec_context:
            ec = session._exec_context
            context.conversation_id = ec.get("conversation_id", "")
            context.run_id = ec.get("run_id", "")
            context.user_input = ec.get("user_input", "")
            context.model_cfg = ec.get("model_cfg")
            context.memory = ec.get("memory")
            context.system_prompt = ec.get("system_prompt", "")
            context.planning_context = ec.get("planning_context")
        return await executor.execute(step, context)
