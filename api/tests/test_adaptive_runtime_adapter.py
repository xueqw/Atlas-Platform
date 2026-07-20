import asyncio
import json
import pytest

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.adaptive_runtime import (
    PlanEnvelope,
    PlanExecuteReviewGraph,
    PlannedTask,
    PlannedWorker,
    ReviewRequestEnvelope,
    ReviewCriterionFinding,
    ReviewResultEnvelope,
    ReviewVerdict,
    validate_review_result,
)
from app.adaptive_runtime_adapter import DurableAdaptivePlanExecutor, DurableIndependentReviewer
from app.database import Base
from app.governance_models import ArtifactRecord, GovernanceBase, OrchestrationRunRecord, WorkerRecord
from app.models import Agent, AgentVersion, RuntimeRun, User, Workspace
from app.multi_agent_runtime_adapter import ServerToolRegistry, TrustedTool
from app.runtime_contract import (
    ExecutionStrategy, RuntimeResumeRequest, RuntimeSource, RuntimeStartRequest,
)
from app.runtime_graph import AtlasAgentState
from app.runtime_persistence import (
    SqlAlchemyRuntimeEventRepository,
    SqlAlchemyRuntimeInterruptRepository,
    SqlAlchemyRuntimeRunRepository,
)
from app.runtime_service import AgentRuntimeService, RuntimeFeatureFlags


def _factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'adaptive.db'}")
    Base.metadata.create_all(engine)
    GovernanceBase.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    request = RuntimeStartRequest(
        workspace_id="ws", user_id="user", agent_id="agent", agent_version_id="version",
        source=RuntimeSource.CHAT, input="complex", idempotency_key="parent-key",
        execution_strategy=ExecutionStrategy.MULTI_AGENT_PLAN_EXECUTE_REVIEW,
    )
    state = AtlasAgentState.initial(request.make_identity("parent-run"), request.input)
    with factory() as db:
        db.add_all([
            Workspace(id="ws", name="Workspace"),
            User(id="user", username="user", name="User"),
            Agent(
                id="agent", workspace_id="ws", name="Agent", status="published",
                current_version_id="version", published_version_id="version",
            ),
            AgentVersion(
                id="version", agent_id="agent", version_no=1, label="published",
                snapshot_json='{"system_prompt":"base","model":"test"}',
            ),
        ])
        db.flush()
        db.add(RuntimeRun(
            id="parent-run", workspace_id="ws", user_id="user", agent_id="agent",
            version_id="version", source="chat", graph_template="multi-agent-plan-execute-review",
            execution_mode="langgraph", thread_id="ws:parent-run", idempotency_key="parent-key",
            status="running", input_json=request.model_dump_json(), state_json=state.model_dump_json(),
        ))
        db.commit()
    return factory, state


def _plan():
    schema = {
        "type": "object", "required": ["answer"],
        "properties": {"answer": {"type": "string"}},
    }
    return PlanEnvelope(
        run_id="parent-run", workspace_id="ws", user_id="user", agent_id="agent",
        strategy=ExecutionStrategy.MULTI_AGENT_PLAN_EXECUTE_REVIEW,
        goal="complex", acceptance_criteria=("complete",),
        workers=(
            PlannedWorker(worker_id="one", role="one", objective="one", output_schema=schema),
            PlannedWorker(worker_id="two", role="two", objective="two", output_schema=schema),
        ),
        tasks=(
            PlannedTask(task_id="one-task", worker_id="one", objective="one"),
            PlannedTask(task_id="two-task", worker_id="two", objective="two"),
        ),
    )


def test_durable_adaptive_executor_runs_parallel_children_and_independent_reviewer(tmp_path):
    factory, state = _factory(tmp_path)
    active = 0
    maximum = 0

    async def model(messages, **_kwargs):
        nonlocal active, maximum
        role_prompt = messages[1]["content"]
        payload = json.loads(messages[-1]["content"])
        if "independent-reviewer" in role_prompt:
            review = payload["payload"]["review_request"]
            return json.dumps({
                "verdict": "PASS",
                "criteria": [{
                    "criterion": "complete", "status": "passed",
                    "finding": "all tasks completed", "evidence_refs": [],
                }],
                "reviewed_task_ids": review["reviewed_task_ids"],
                "required_actions": [], "revise_task_ids": [],
                "evidence_refs": [], "confidence": 0.95,
            })
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0.01)
        active -= 1
        return json.dumps({"answer": f"completed:{payload['task_id']}"})

    plan = _plan()
    executor = DurableAdaptivePlanExecutor(factory, model_complete=model)
    batch = asyncio.run(executor(
        state, plan, ("one-task", "two-task"), None,
    ))
    assert maximum == 2
    assert {item.task_id for item in batch.results} == {"one-task", "two-task"}
    assert all(item.status == "succeeded" for item in batch.results)
    assert all(item.runtime_run_id for item in batch.results)

    request = ReviewRequestEnvelope(
        run_id="parent-run", workspace_id="ws", user_id="user", agent_id="agent",
        reviewer_id="reviewer-1", plan=plan, results=batch.results,
        reviewed_task_ids=("one-task", "two-task"), review_round=1,
    )
    review = asyncio.run(DurableIndependentReviewer(factory, model_complete=model)(request))
    assert validate_review_result(request, review).verdict.value == "PASS"

    with factory() as db:
        assert len(db.scalars(select(OrchestrationRunRecord)).all()) == 2
        children = db.scalars(select(RuntimeRun).where(RuntimeRun.source == "subagent")).all()
        assert len(children) == 3
        reviewer = next(
            row for row in db.scalars(select(WorkerRecord)).all()
            if row.spec["role"] == "independent-reviewer"
        )
        assert reviewer.spec["allowed_tools"] == []
        assert reviewer.spec["lifecycle"] == "ephemeral"


def test_adaptive_executor_rejects_cross_tenant_artifact_reference(tmp_path):
    factory, state = _factory(tmp_path)
    with factory() as db:
        db.add(ArtifactRecord(
            artifact_id="foreign-artifact", workspace_id="other", user_id="other",
            agent_id="other", run_id="other", worker_id="other",
            storage_ref="minio://other/private.txt", media_type="text/plain", sha256="a" * 64,
        ))
        db.commit()
    value = _plan()
    first = value.tasks[0].model_copy(update={"artifact_refs": ("foreign-artifact",)})
    value = value.model_copy(update={"tasks": (first, value.tasks[1])})

    async def model(*_args, **_kwargs):
        return '{"answer":"unused"}'

    executor = DurableAdaptivePlanExecutor(factory, model_complete=model)
    try:
        asyncio.run(executor(state, value, ("one-task", "two-task"), None))
    except PermissionError as exc:
        assert "artifact" in str(exc)
    else:
        raise AssertionError("cross-tenant artifact must fail closed")


def test_adaptive_executor_enforces_nested_worker_input_schema(tmp_path):
    factory, state = _factory(tmp_path)
    value = _plan()
    strict_worker = value.workers[0].model_copy(update={
        "input_schema": {
            "type": "object",
            "required": ["must_exist"],
            "properties": {"must_exist": {"type": "string"}},
            "additionalProperties": False,
        },
    })
    value = value.model_copy(update={
        "workers": (strict_worker, value.workers[1]),
    })

    async def model(*_args, **_kwargs):
        raise AssertionError("invalid input must fail before model execution")

    with pytest.raises(ValueError, match="input_schema"):
        asyncio.run(DurableAdaptivePlanExecutor(factory, model_complete=model)(
            state, value, ("one-task", "two-task"), None,
        ))


def test_adaptive_write_pauses_before_side_effect_and_resumes_through_parent_gate(tmp_path):
    factory, _state = _factory(tmp_path)
    with factory() as db:
        db.query(RuntimeRun).delete()
        db.commit()

    writes = []

    async def write_tool(_db, arguments):
        writes.append(dict(arguments))
        return {"sent": True}

    registry = ServerToolRegistry({
        "send": TrustedTool(
            name="send", access="write", invoke=write_tool,
            required_scope=frozenset({"tool:send:write"}),
        ),
    })

    async def model(_messages, **_kwargs):
        return '{"answer":"done"}'

    def planner(state, strategy, version):
        return PlanEnvelope(
            run_id=state.identity.run_id,
            workspace_id=state.identity.workspace_id,
            user_id=state.identity.user_id,
            agent_id=state.identity.agent_id,
            strategy=strategy,
            version=version,
            goal=state.input,
            acceptance_criteria=("complete",),
            workers=(PlannedWorker(
                worker_id="sender", role="sender", objective="send once",
                allowed_tools=("send",),
                output_schema={
                    "type": "object", "required": ["answer"],
                    "properties": {"answer": {"type": "string"}},
                },
            ),),
            tasks=(PlannedTask(
                task_id="send-task", worker_id="sender", objective="send once",
                payload={"tool_call": {"name": "send", "arguments": {"body": "hello"}}},
            ),),
        )

    def reviewer(request):
        return ReviewResultEnvelope(
            request_envelope_id=request.envelope_id,
            run_id=request.run_id,
            workspace_id=request.workspace_id,
            user_id=request.user_id,
            agent_id=request.agent_id,
            reviewer_id=request.reviewer_id,
            verdict=ReviewVerdict.PASS,
            criteria=(ReviewCriterionFinding(criterion="complete", status="passed"),),
            reviewed_task_ids=request.reviewed_task_ids,
            confidence=1.0,
        )

    executor = DurableAdaptivePlanExecutor(
        factory, model_complete=model, tool_registry=registry,
    )
    graph = PlanExecuteReviewGraph(
        planner,
        executor,
        reviewer,
        strategy=ExecutionStrategy.PLAN_EXECUTE_REVIEW,
        prefer_langgraph=False,
    )
    service = AgentRuntimeService(
        graph,
        event_repository=SqlAlchemyRuntimeEventRepository(factory),
        run_repository=SqlAlchemyRuntimeRunRepository(factory),
        interrupt_repository=SqlAlchemyRuntimeInterruptRepository(factory),
        flags=RuntimeFeatureFlags(langgraph_enabled=True, allow_legacy_fallback=True),
    )
    request = RuntimeStartRequest(
        workspace_id="ws", user_id="user", agent_id="agent", agent_version_id="version",
        source=RuntimeSource.CHAT, input="send once", idempotency_key="adaptive-write",
        requested_resources=("tool:send",),
        execution_strategy=ExecutionStrategy.PLAN_EXECUTE_REVIEW,
    )

    async def exercise():
        started = await service.start(request)
        paused = await service.wait(run_id=started.run_id, workspace_id="ws")
        assert paused.status.value == "paused"
        assert writes == []
        state = service.runs.get(run_id=started.run_id, workspace_id="ws").state
        assert state.side_effects_started is True
        events = service.stream(run_id=started.run_id, workspace_id="ws")
        event_types = [event.type for event in events]
        assert event_types.index("side_effect.boundary_started") < event_types.index("run.paused")
        # Simulate a process stopping after the graph pause snapshot but before
        # the parent interrupt ID was attached to the Runtime state.
        stored = service.runs.get(run_id=started.run_id, workspace_id="ws")
        service.runs.save(stored.__class__(
            stored.request,
            service._state_with(stored.state, complex_context={
                **stored.state.complex_context,
                "runtime_interrupt_id": None,
            }),
            stored.execution_mode,
            stored.created_at,
        ))
        with pytest.raises(ValueError, match="was recovered"):
            await service.resume(RuntimeResumeRequest(
                run_id=started.run_id, workspace_id="ws", user_id="user",
            ))
        events = service.stream(run_id=started.run_id, workspace_id="ws")
        interrupt_events = [event for event in events if event.type == "interrupt.requested"]
        assert len({event.payload["interrupt_id"] for event in interrupt_events}) == 1
        interrupt = interrupt_events[-1]
        payload = interrupt.payload
        with pytest.raises(ValueError, match="explicit bound"):
            await service.resume(RuntimeResumeRequest(
                run_id=started.run_id, workspace_id="ws", user_id="user",
            ))
        approval = RuntimeResumeRequest(
            run_id=started.run_id,
            workspace_id="ws",
            user_id="user",
            interrupt_id=payload["interrupt_id"],
            nonce=payload["nonce"],
            decision="approve",
            parameter_digest=payload["parameter_digest"],
            resource_version=payload["resource_version"],
        )
        # Fault injection: the interrupt decision commits, then the process
        # exits before the parent Runtime state receives the authorization.
        assert service.interrupts.resolve(approval).status == "approved"
        resumed = await service.resume(approval)
        assert resumed.status.value == "running"
        completed = await service.wait(run_id=started.run_id, workspace_id="ws")
        assert completed.status.value == "succeeded"
        with pytest.raises(ValueError, match="already consumed"):
            await service.resume(approval)
        denied_started = await service.start(request.model_copy(update={
            "idempotency_key": "adaptive-write-denied",
        }))
        denied_paused = await service.wait(run_id=denied_started.run_id, workspace_id="ws")
        assert denied_paused.status.value == "paused"
        denied_interrupt = next(
            event for event in service.stream(run_id=denied_started.run_id, workspace_id="ws")
            if event.type == "interrupt.requested"
        )
        denied = await service.resume(RuntimeResumeRequest(
            run_id=denied_started.run_id,
            workspace_id="ws",
            user_id="user",
            interrupt_id=denied_interrupt.payload["interrupt_id"],
            nonce=denied_interrupt.payload["nonce"],
            decision="deny",
            parameter_digest=denied_interrupt.payload["parameter_digest"],
            resource_version=denied_interrupt.payload["resource_version"],
        ))
        assert denied.status.value == "cancelled"
        return started.run_id

    run_id = asyncio.run(exercise())
    assert writes == [{"body": "hello"}]
    events = service.stream(run_id=run_id, workspace_id="ws")
    assert any(event.type == "interrupt.resolved" for event in events)
    assert not any(event.type == "runtime.fallback" for event in events)
