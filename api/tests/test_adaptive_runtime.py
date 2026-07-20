import asyncio
import uuid
import pytest
from pydantic import ValidationError

from app.adaptive_runtime import (
    PlanEnvelope,
    AdaptiveRuntimeRunner,
    PlanExecutionBatch,
    PlanExecuteReviewGraph,
    PlannedTask,
    PlannedWorker,
    ReviewCriterionFinding,
    ReviewRequestEnvelope,
    ReviewResultEnvelope,
    ReviewVerdict,
    StrategySignals,
    TaskStrategyRouter,
    WorkerResultEnvelope,
    adaptive_contract_schemas,
    validate_review_result,
)
from app.runtime_contract import ExecutionStrategy
from app.runtime_contract import RuntimeResumeRequest, RuntimeSource, RuntimeStartRequest
from app.runtime_graph import AtlasAgentState, LANGGRAPH_AVAILABLE
from app.runtime_graph import LegacyGraphShim
from app.runtime_service import AgentRuntimeService, RuntimeFeatureFlags


def plan(*, strategy=ExecutionStrategy.PLAN_EXECUTE_REVIEW, workers=None, tasks=None):
    workers = workers or (
        PlannedWorker(worker_id="executor", role="executor", objective="complete task"),
    )
    tasks = tasks or (
        PlannedTask(task_id="task-1", worker_id=workers[0].worker_id, objective="do work"),
    )
    return PlanEnvelope(
        run_id="run", workspace_id="ws", user_id="user", agent_id="agent",
        strategy=strategy, goal="deliver result", acceptance_criteria=("correct",),
        workers=workers, tasks=tasks,
    )


def review_request(value=None):
    value = value or plan()
    return ReviewRequestEnvelope(
        run_id="run", workspace_id="ws", user_id="user", agent_id="agent",
        reviewer_id="reviewer-1", plan=value,
        results=(worker_result_for_scope("task-1", "executor"),),
        reviewed_task_ids=("task-1",),
    )


def worker_result(state, task_id, worker_id, *, result=None, status="succeeded", **changes):
    return WorkerResultEnvelope(
        envelope_id=str(uuid.uuid4()),
        run_id=state.identity.run_id,
        task_id=task_id,
        worker_id=worker_id,
        workspace_id=state.identity.workspace_id,
        user_id=state.identity.user_id,
        agent_id=state.identity.agent_id,
        status=status,
        result=result or {},
        **changes,
    )


def worker_result_for_scope(task_id, worker_id, *, status="succeeded"):
    return WorkerResultEnvelope(
        envelope_id=str(uuid.uuid4()), run_id="run", task_id=task_id,
        worker_id=worker_id, workspace_id="ws", user_id="user", agent_id="agent",
        status=status, result={},
    )


def review_result(request, verdict=ReviewVerdict.PASS, **changes):
    values = dict(
        request_envelope_id=request.envelope_id,
        run_id=request.run_id, workspace_id=request.workspace_id,
        user_id=request.user_id, agent_id=request.agent_id,
        reviewer_id=request.reviewer_id, verdict=verdict,
        criteria=(ReviewCriterionFinding(criterion="correct", status="passed"),),
        reviewed_task_ids=request.reviewed_task_ids, confidence=0.9,
    )
    values.update(changes)
    return ReviewResultEnvelope(**values)


def test_router_selects_react_sequential_and_multi_agent_with_reason_codes():
    router = TaskStrategyRouter()
    simple = router.route("What is Atlas?")
    assert simple.selected is ExecutionStrategy.REACT
    assert "bounded_react_sufficient" in simple.reason_codes

    sequential = router.route("Implement a migration, then test and publish a reviewed report")
    assert sequential.selected is ExecutionStrategy.PLAN_EXECUTE_REVIEW
    assert sequential.policy_version == "atlas.strategy-policy.v1"

    multi = router.route("Use multiple specialist workers in parallel to analyze security and operations")
    assert multi.selected is ExecutionStrategy.MULTI_AGENT_PLAN_EXECUTE_REVIEW
    assert "parallel_workstreams" in multi.reason_codes


def test_router_model_signals_and_explicit_override_cannot_lower_safety_floor():
    router = TaskStrategyRouter()
    decision = router.route(
        "Answer briefly",
        requested=ExecutionStrategy.REACT,
        model_signals=StrategySignals(requires_write=True, high_risk=True),
    )
    assert decision.selected is ExecutionStrategy.PLAN_EXECUTE_REVIEW
    assert "override_raised_by_policy" in decision.reason_codes

    explicit = router.route(
        "Summarize this",
        requested=ExecutionStrategy.MULTI_AGENT_PLAN_EXECUTE_REVIEW,
    )
    assert explicit.selected is ExecutionStrategy.MULTI_AGENT_PLAN_EXECUTE_REVIEW


def test_plan_contract_rejects_unknown_worker_cycles_and_sequential_multi_worker():
    with pytest.raises(ValidationError, match="unknown worker"):
        plan(tasks=(PlannedTask(task_id="x", worker_id="missing", objective="x"),))
    worker = PlannedWorker(worker_id="one", role="one", objective="one")
    with pytest.raises(ValidationError, match="acyclic"):
        plan(workers=(worker,), tasks=(
            PlannedTask(task_id="a", worker_id="one", objective="a", depends_on=("b",)),
            PlannedTask(task_id="b", worker_id="one", objective="b", depends_on=("a",)),
        ))
    with pytest.raises(ValidationError, match="exactly one worker"):
        plan(workers=(
            worker,
            PlannedWorker(worker_id="two", role="two", objective="two"),
        ))


def test_review_contract_requires_independent_identity_exact_scope_and_full_coverage():
    request = review_request()
    accepted = validate_review_result(request, review_result(request))
    assert accepted.verdict is ReviewVerdict.PASS

    with pytest.raises(PermissionError, match="scope"):
        validate_review_result(request, review_result(request, workspace_id="other"))
    with pytest.raises(ValueError, match="every requested task"):
        validate_review_result(request, review_result(request, reviewed_task_ids=("other",)))
    with pytest.raises(ValueError, match="criterion"):
        validate_review_result(request, review_result(
            request,
            criteria=(ReviewCriterionFinding(criterion="unknown", status="passed"),),
        ))
    with pytest.raises(ValidationError, match="independent"):
        ReviewRequestEnvelope(
            run_id="run", workspace_id="ws", user_id="user", agent_id="agent",
            reviewer_id="executor", plan=plan(),
            results=(worker_result_for_scope("task-1", "executor"),),
            reviewed_task_ids=("task-1",),
        )


def test_review_verdict_fields_and_contract_schemas_fail_closed():
    request = review_request()
    with pytest.raises(ValidationError, match="requires actions"):
        review_result(request, ReviewVerdict.REPLAN)
    with pytest.raises(ValidationError, match="scoped task"):
        review_result(request, ReviewVerdict.REVISE, required_actions=("fix",))
    with pytest.raises(ValidationError):
        ReviewResultEnvelope(**review_result(request).model_dump(), unknown=True)
    schemas = adaptive_contract_schemas()
    assert schemas["PlanEnvelope"]["additionalProperties"] is False
    assert schemas["WorkerResultEnvelope"]["additionalProperties"] is False
    assert schemas["ReviewResultEnvelope"]["additionalProperties"] is False


def test_reviewer_pass_cannot_override_failed_deterministic_findings():
    request = ReviewRequestEnvelope(
        run_id="run", workspace_id="ws", user_id="user", agent_id="agent",
        reviewer_id="reviewer-1", plan=plan(),
        results=(worker_result_for_scope("task-1", "executor", status="failed"),),
        reviewed_task_ids=("task-1",),
        deterministic_findings=(ReviewCriterionFinding(
            criterion="correct", status="failed", finding="worker failed",
        ),),
    )
    with pytest.raises(ValueError, match="deterministic"):
        validate_review_result(request, review_result(request, ReviewVerdict.PASS))


def complex_state():
    request = RuntimeStartRequest(
        workspace_id="ws", user_id="user", agent_id="agent", agent_version_id="v1",
        source=RuntimeSource.CHAT, input="complex task", idempotency_key="complex-key",
        execution_strategy=ExecutionStrategy.PLAN_EXECUTE_REVIEW,
    )
    return AtlasAgentState.initial(request.make_identity("run"), request.input)


def make_reviewer(verdicts):
    values = iter(verdicts)

    def reviewer(request):
        verdict = next(values)
        kwargs = {}
        if verdict is ReviewVerdict.REVISE:
            kwargs = {"required_actions": ("fix task",), "revise_task_ids": ("task-1",)}
        elif verdict is ReviewVerdict.REPLAN:
            kwargs = {"required_actions": ("change plan",)}
        elif verdict is ReviewVerdict.ESCALATE:
            kwargs = {"required_actions": ("user decision",)}
        criteria_status = "passed" if verdict is ReviewVerdict.PASS else "failed"
        return ReviewResultEnvelope(
            request_envelope_id=request.envelope_id,
            run_id=request.run_id, workspace_id=request.workspace_id,
            user_id=request.user_id, agent_id=request.agent_id,
            reviewer_id=request.reviewer_id, verdict=verdict,
            criteria=(ReviewCriterionFinding(criterion="correct", status=criteria_status),),
            reviewed_task_ids=request.reviewed_task_ids,
            confidence=0.9,
            **kwargs,
        )

    return reviewer


def test_plan_execute_review_requires_independent_pass_before_success():
    reviewer_requests = []

    def planner(state, strategy, version):
        return PlanEnvelope(
            run_id=state.identity.run_id, workspace_id=state.identity.workspace_id,
            user_id=state.identity.user_id, agent_id=state.identity.agent_id,
            strategy=strategy, version=version, goal=state.input,
            acceptance_criteria=("correct",),
            workers=(PlannedWorker(worker_id="executor", role="executor", objective="work"),),
            tasks=(PlannedTask(task_id="task-1", worker_id="executor", objective="work"),),
        )

    async def executor(_state, _plan, task_ids, _revision):
        return PlanExecutionBatch(results=tuple(
            worker_result(
                _state, task_id, "executor",
                result={"answer": "done"}, runtime_run_id="child-1",
            )
            for task_id in task_ids
        ))

    base_reviewer = make_reviewer([ReviewVerdict.PASS])

    def reviewer(request):
        reviewer_requests.append(request)
        return base_reviewer(request)

    result = asyncio.run(PlanExecuteReviewGraph(
        planner, executor, reviewer,
        strategy=ExecutionStrategy.PLAN_EXECUTE_REVIEW,
        prefer_langgraph=False,
    ).ainvoke(complex_state()))
    assert result.state.status.value == "succeeded"
    assert reviewer_requests[0].reviewer_id == "reviewer-1"
    assert reviewer_requests[0].reviewer_id not in {worker.worker_id for worker in reviewer_requests[0].plan.workers}
    events = [item.event_type for item in result.state.transitions]
    assert events.index("worker.completed") < events.index("review.requested") < events.index("review.completed") < events.index("run.completed")


@pytest.mark.parametrize(
    ("verdict", "expected"),
    [(ReviewVerdict.REJECT, "failed"), (ReviewVerdict.ESCALATE, "paused")],
)
def test_complex_graph_maps_terminal_review_verdicts(verdict, expected):
    def planner(state, strategy, version):
        value = plan(strategy=strategy)
        return PlanEnvelope.model_validate({
            **value.model_dump(), "run_id": state.identity.run_id, "version": version,
        })

    def executor(_state, _plan, task_ids, _revision):
        return PlanExecutionBatch(results=tuple(
            worker_result(_state, item, "executor")
            for item in task_ids
        ))

    result = asyncio.run(PlanExecuteReviewGraph(
        planner, executor, make_reviewer([verdict]),
        strategy=ExecutionStrategy.PLAN_EXECUTE_REVIEW,
        prefer_langgraph=False,
    ).ainvoke(complex_state()))
    assert result.state.status.value == expected


def test_complex_graph_bounds_revision_and_replan_loops():
    planner_versions = []
    execution_rounds = []

    def planner(state, strategy, version):
        planner_versions.append(version)
        value = plan(strategy=strategy)
        return PlanEnvelope.model_validate({
            **value.model_dump(), "run_id": state.identity.run_id, "version": version,
        })

    def executor(_state, _plan, task_ids, revision):
        execution_rounds.append((task_ids, revision.revision_round if revision else 0))
        return PlanExecutionBatch(results=tuple(
            worker_result(
                _state, item, "executor", result={"round": len(execution_rounds)},
            )
            for item in task_ids
        ))

    revised = asyncio.run(PlanExecuteReviewGraph(
        planner, executor, make_reviewer([ReviewVerdict.REVISE, ReviewVerdict.PASS]),
        strategy=ExecutionStrategy.PLAN_EXECUTE_REVIEW,
        prefer_langgraph=False,
    ).ainvoke(complex_state()))
    assert revised.state.status.value == "succeeded"
    assert execution_rounds == [(('task-1',), 0), (('task-1',), 1)]

    replanned = asyncio.run(PlanExecuteReviewGraph(
        planner, executor, make_reviewer([ReviewVerdict.REPLAN, ReviewVerdict.PASS]),
        strategy=ExecutionStrategy.PLAN_EXECUTE_REVIEW,
        prefer_langgraph=False,
    ).ainvoke(complex_state()))
    assert replanned.state.status.value == "succeeded"
    assert planner_versions[-2:] == [1, 2]

    def no_revision_plan(state, strategy, version):
        value = planner(state, strategy, version)
        return PlanEnvelope.model_validate({**value.model_dump(), "max_revision_rounds": 0})

    exhausted = asyncio.run(PlanExecuteReviewGraph(
        no_revision_plan, executor, make_reviewer([ReviewVerdict.REVISE]),
        strategy=ExecutionStrategy.PLAN_EXECUTE_REVIEW,
        prefer_langgraph=False,
    ).ainvoke(complex_state()))
    assert exhausted.state.status.value == "failed"
    assert exhausted.state.complex_context["terminal_reason"] == "revision_budget_exhausted"


def test_adaptive_runner_routes_once_and_delegates_to_registered_strategy_graph():
    def state_for(text):
        request = RuntimeStartRequest(
            workspace_id="ws", user_id="user", agent_id="agent", agent_version_id="v1",
            source=RuntimeSource.CHAT, input=text, idempotency_key=f"key-{len(text)}",
        )
        return AtlasAgentState.initial(
            request.make_identity(f"run-{len(text)}"), request.input,
            requested_execution_strategy=request.execution_strategy.value,
        )

    react = LegacyGraphShim(lambda _state: "simple")

    def complex_factory(strategy):
        def planner(state, selected, version):
            if selected is ExecutionStrategy.PLAN_EXECUTE_REVIEW:
                workers = (PlannedWorker(worker_id="executor", role="executor", objective="work"),)
                tasks = (PlannedTask(task_id="task", worker_id="executor", objective="work"),)
            else:
                workers = (
                    PlannedWorker(worker_id="one", role="one", objective="one"),
                    PlannedWorker(worker_id="two", role="two", objective="two"),
                )
                tasks = (
                    PlannedTask(task_id="one", worker_id="one", objective="one"),
                    PlannedTask(task_id="two", worker_id="two", objective="two"),
                )
            return PlanEnvelope(
                run_id=state.identity.run_id, workspace_id="ws", user_id="user", agent_id="agent",
                strategy=selected, version=version, goal=state.input,
                acceptance_criteria=("correct",), workers=workers, tasks=tasks,
            )

        def executor(_state, plan, task_ids, _revision):
            workers = {task.task_id: task.worker_id for task in plan.tasks}
            return PlanExecutionBatch(results=tuple(
                worker_result(
                    _state, item, workers[item], result={"answer": item},
                )
                for item in task_ids
            ))

        return PlanExecuteReviewGraph(
            planner, executor, make_reviewer([ReviewVerdict.PASS]),
            strategy=strategy, prefer_langgraph=False,
        )

    runner = AdaptiveRuntimeRunner(react, complex_factory)
    simple = asyncio.run(runner.ainvoke(state_for("What is Atlas?")))
    assert simple.state.execution_strategy == "react"
    assert simple.state.output == "simple"
    assert [event.event_type for event in simple.state.transitions].count("strategy.selected") == 1

    complex_result = asyncio.run(runner.ainvoke(state_for(
        "Use multiple specialist workers in parallel to analyze security and operations"
    )))
    assert complex_result.state.execution_strategy == "multi-agent-plan-execute-review"
    assert complex_result.state.status.value == "succeeded"


def test_escalation_requires_actor_decision_and_approved_resume_replans():
    verdicts = iter([ReviewVerdict.ESCALATE, ReviewVerdict.PASS])

    def planner(state, strategy, version):
        return PlanEnvelope(
            run_id=state.identity.run_id, workspace_id="ws", user_id="user", agent_id="agent",
            strategy=strategy, version=version, goal=state.input,
            acceptance_criteria=("correct",),
            workers=(PlannedWorker(worker_id="executor", role="executor", objective="work"),),
            tasks=(PlannedTask(task_id="task", worker_id="executor", objective="work"),),
        )

    def executor(_state, _plan, task_ids, _revision):
        return PlanExecutionBatch(results=tuple(
            worker_result(_state, item, "executor", result={"answer": "ok"})
            for item in task_ids
        ))

    def reviewer(request):
        verdict = next(verdicts)
        return ReviewResultEnvelope(
            request_envelope_id=request.envelope_id,
            run_id=request.run_id, workspace_id=request.workspace_id,
            user_id=request.user_id, agent_id=request.agent_id,
            reviewer_id=request.reviewer_id, verdict=verdict,
            criteria=(ReviewCriterionFinding(
                criterion="correct", status="passed" if verdict is ReviewVerdict.PASS else "uncertain",
            ),),
            reviewed_task_ids=request.reviewed_task_ids,
            required_actions=("confirm approach",) if verdict is ReviewVerdict.ESCALATE else (),
            confidence=0.8,
        )

    def factory(strategy):
        return PlanExecuteReviewGraph(
            planner, executor, reviewer, strategy=strategy, prefer_langgraph=False,
        )

    runner = AdaptiveRuntimeRunner(LegacyGraphShim(lambda _state: "unused"), factory)
    service = AgentRuntimeService(
        runner,
        flags=RuntimeFeatureFlags(langgraph_enabled=True, allow_legacy_fallback=False),
    )
    request = RuntimeStartRequest(
        workspace_id="ws", user_id="user", agent_id="agent", agent_version_id="v1",
        source=RuntimeSource.CHAT, input="Implement and review a migration",
        idempotency_key="escalation", execution_strategy=ExecutionStrategy.PLAN_EXECUTE_REVIEW,
    )

    async def execute():
        started = await service.start(request)
        paused = await service.wait(run_id=started.run_id, workspace_id="ws")
        with pytest.raises(ValueError, match="explicit"):
            await service.resume(RuntimeResumeRequest(
                run_id=paused.run_id, workspace_id="ws", user_id="user",
            ))
        resumed = await service.resume(RuntimeResumeRequest(
            run_id=paused.run_id, workspace_id="ws", user_id="user",
            escalation_decision="approve",
        ))
        assert resumed.status.value == "running"
        return await service.wait(run_id=paused.run_id, workspace_id="ws")

    completed = asyncio.run(execute())
    assert completed.status.value == "succeeded"
    events = service.stream(run_id=completed.run_id, workspace_id="ws")
    assert any(event.type == "escalation.resolved" for event in events)


def test_plan_execute_review_runs_as_a_real_langgraph_checkpointed_template():
    if not LANGGRAPH_AVAILABLE:
        return
    from langgraph.checkpoint.memory import InMemorySaver

    def planner(state, strategy, version):
        return PlanEnvelope(
            run_id=state.identity.run_id, workspace_id="ws", user_id="user", agent_id="agent",
            strategy=strategy, version=version, goal=state.input,
            acceptance_criteria=("correct",),
            workers=(PlannedWorker(worker_id="executor", role="executor", objective="work"),),
            tasks=(PlannedTask(task_id="task", worker_id="executor", objective="work"),),
        )

    def executor(_state, _plan, task_ids, _revision):
        return PlanExecutionBatch(results=tuple(
            worker_result(_state, item, "executor", result={"answer": "ok"})
            for item in task_ids
        ))

    result = asyncio.run(PlanExecuteReviewGraph(
        planner,
        executor,
        make_reviewer([ReviewVerdict.PASS]),
        strategy=ExecutionStrategy.PLAN_EXECUTE_REVIEW,
        checkpointer=InMemorySaver(),
        prefer_langgraph=True,
    ).ainvoke(complex_state()))
    assert result.engine == "langgraph"
    assert result.state.status.value == "succeeded"
