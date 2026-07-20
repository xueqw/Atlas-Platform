"""Integration gate for the production LangGraph PostgreSQL checkpointer."""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest

from app.adaptive_runtime import (
    PlanEnvelope,
    PlanExecuteReviewGraph,
    PlanExecutionBatch,
    PlannedTask,
    PlannedWorker,
    ReviewCriterionFinding,
    ReviewResultEnvelope,
    ReviewVerdict,
    WorkerResultEnvelope,
)
from app.runtime_checkpoint import open_postgres_checkpointer, setup_postgres_checkpoints
from app.runtime_contract import ExecutionStrategy, RuntimeIdentity, RuntimeSource
from app.runtime_graph import AtlasAgentState, RuntimePhaseOneGraph


def test_langgraph_persists_checkpoint_to_postgres() -> None:
    if not os.getenv("RUN_POSTGRES_RUNTIME_TESTS"):
        pytest.skip("set RUN_POSTGRES_RUNTIME_TESTS=1 for the real PostgreSQL checkpointer probe")

    async def exercise() -> None:
        url = os.environ["LANGGRAPH_CHECKPOINT_DATABASE_URL"]
        await setup_postgres_checkpoints(url)
        suffix = uuid.uuid4().hex
        identity = RuntimeIdentity(
            run_id=f"checkpoint-acceptance-run-{suffix}",
            thread_id=f"checkpoint-acceptance-workspace:checkpoint-acceptance-run-{suffix}",
            workspace_id="checkpoint-acceptance-workspace",
            user_id="checkpoint-acceptance-user",
            agent_id="checkpoint-acceptance-agent",
            agent_version_id="v1",
            source=RuntimeSource.API,
        )
        state = AtlasAgentState.initial(identity, "验证 PostgreSQL checkpoint")

        calls = {"first_process": 0, "restarted_process": 0}

        def first_model(_state):
            calls["first_process"] += 1
            return "checkpoint durable"

        # Process A creates the durable checkpoint and then releases every
        # in-memory graph/checkpointer object.
        async with open_postgres_checkpointer(url) as checkpointer:
            graph = RuntimePhaseOneGraph(first_model, checkpointer=checkpointer)
            result = await graph.ainvoke(state)
            persisted = await checkpointer.aget_tuple({"configurable": {"thread_id": identity.thread_id}})

        assert result.state.output == "checkpoint durable"
        assert persisted is not None
        assert calls["first_process"] == 1

        def must_not_repeat_completed_node(_state):
            calls["restarted_process"] += 1
            raise AssertionError("completed model node was duplicated after restart")

        # Process B is a fresh graph and saver connection. Redis is deliberately
        # absent from this recovery path: PostgreSQL plus the Atlas state/event
        # records remain authoritative.
        async with open_postgres_checkpointer(url) as restarted_checkpointer:
            restarted_graph = RuntimePhaseOneGraph(
                must_not_repeat_completed_node,
                checkpointer=restarted_checkpointer,
            )
            resumed = await restarted_graph.aresume(result.state)
            recovered = await restarted_checkpointer.aget_tuple({
                "configurable": {"thread_id": identity.thread_id},
            })

        assert resumed.state.output == "checkpoint durable"
        assert recovered is not None
        assert calls["restarted_process"] == 0

    asyncio.run(exercise())


def test_adaptive_plan_review_checkpoint_round_trip_to_postgres() -> None:
    """The complex graph must survive the production JSON checkpoint boundary."""
    if not os.getenv("RUN_POSTGRES_RUNTIME_TESTS"):
        pytest.skip("set RUN_POSTGRES_RUNTIME_TESTS=1 for the real PostgreSQL checkpointer probe")

    async def exercise() -> None:
        url = os.environ["LANGGRAPH_CHECKPOINT_DATABASE_URL"]
        await setup_postgres_checkpoints(url)
        suffix = uuid.uuid4().hex
        identity = RuntimeIdentity(
            run_id=f"adaptive-checkpoint-run-{suffix}",
            thread_id=f"adaptive-checkpoint-workspace:adaptive-checkpoint-run-{suffix}",
            workspace_id="adaptive-checkpoint-workspace",
            user_id="adaptive-checkpoint-user",
            agent_id="adaptive-checkpoint-agent",
            agent_version_id="v1",
            source=RuntimeSource.API,
        )
        state = AtlasAgentState.initial(identity, "plan, execute, and review")
        calls = {"planner": 0, "executor": 0, "reviewer": 0, "restarted": 0}

        def planner(current, strategy, version):
            calls["planner"] += 1
            return PlanEnvelope(
                run_id=current.identity.run_id,
                workspace_id=current.identity.workspace_id,
                user_id=current.identity.user_id,
                agent_id=current.identity.agent_id,
                strategy=strategy,
                version=version,
                goal=current.input,
                acceptance_criteria=("correct",),
                workers=(PlannedWorker(worker_id="executor", role="executor", objective="work"),),
                tasks=(PlannedTask(task_id="task", worker_id="executor", objective="work"),),
            )

        def executor(current, _plan, task_ids, _revision):
            calls["executor"] += 1
            return PlanExecutionBatch(results=tuple(
                WorkerResultEnvelope(
                    envelope_id=str(uuid.uuid4()),
                    run_id=current.identity.run_id,
                    task_id=task_id,
                    worker_id="executor",
                    workspace_id=current.identity.workspace_id,
                    user_id=current.identity.user_id,
                    agent_id=current.identity.agent_id,
                    status="succeeded",
                    result={"answer": "ok"},
                )
                for task_id in task_ids
            ))

        def reviewer(request):
            calls["reviewer"] += 1
            return ReviewResultEnvelope(
                request_envelope_id=request.envelope_id,
                run_id=request.run_id,
                workspace_id=request.workspace_id,
                user_id=request.user_id,
                agent_id=request.agent_id,
                reviewer_id=request.reviewer_id,
                verdict=ReviewVerdict.PASS,
                criteria=(ReviewCriterionFinding(criterion="correct", status="passed"),),
                reviewed_task_ids=request.reviewed_task_ids,
                confidence=1.0,
            )

        async with open_postgres_checkpointer(url) as checkpointer:
            graph = PlanExecuteReviewGraph(
                planner,
                executor,
                reviewer,
                strategy=ExecutionStrategy.PLAN_EXECUTE_REVIEW,
                checkpointer=checkpointer,
            )
            result = await graph.ainvoke(state)
            persisted = await checkpointer.aget_tuple({"configurable": {"thread_id": identity.thread_id}})

        assert result.engine == "langgraph"
        assert result.state.status.value == "succeeded"
        assert persisted is not None
        assert calls == {"planner": 1, "executor": 1, "reviewer": 1, "restarted": 0}

        def must_not_restart(*_args):
            calls["restarted"] += 1
            raise AssertionError("completed complex graph node was duplicated after restart")

        async with open_postgres_checkpointer(url) as restarted_checkpointer:
            restarted = PlanExecuteReviewGraph(
                must_not_restart,
                must_not_restart,
                must_not_restart,
                strategy=ExecutionStrategy.PLAN_EXECUTE_REVIEW,
                checkpointer=restarted_checkpointer,
            )
            resumed = await restarted.aresume(result.state)

        assert resumed.state.status.value == "succeeded"
        assert calls["restarted"] == 0

    asyncio.run(exercise())
