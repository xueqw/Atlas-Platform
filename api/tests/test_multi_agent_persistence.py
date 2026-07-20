import asyncio
from copy import deepcopy
import json

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.governance_models import (
    CandidateAssetVersionRecord,
    GovernanceAuditRecord,
    GovernanceBase,
    OrchestrationRunRecord,
    ResultEnvelopeRecord,
    SkillRouterDecision,
    TaskEnvelopeRecord,
    ToolAuthorizationRecord,
    WorkerRecord,
)
from app.models import Agent, AgentVersion, RuntimeRun, Skill
from app.memory_ledger import MemoryScope, record_episodic_evidence
from app.multi_agent_repository import MultiAgentRepository, OrchestrationScope
from app.multi_agent_runtime import ResultEnvelope, ToolPolicy, WorkerSpec
from app.multi_agent_service import CandidateAssetPublisher, DurableOrchestrator, TaskPlan
from app.multi_agent_runtime_adapter import ServerToolRegistry, SubagentRuntimeExecutor, TrustedTool


@pytest.fixture()
def session_factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'multi-agent.db'}")
    Base.metadata.create_all(engine)
    GovernanceBase.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def scope(run_id="run-1", workspace_id="ws-1"):
    return OrchestrationScope(workspace_id, "user-1", "agent-1", run_id)


def worker(worker_id="worker-1"):
    return WorkerSpec(
        worker_id, "1.0.0", "specialist", "complete a bounded task",
        {"type": "object", "required": ["value"],
         "properties": {"value": {"type": "string"}}},
        {"type": "object", "required": ["answer"],
         "properties": {"answer": {"type": "string"}}},
        frozenset({"search", "refund"}),
    )


def result_for(envelope, *, status="succeeded", answer="ok", error=""):
    return ResultEnvelope(
        envelope_id=f"result-{envelope.task_id}-{status}-{answer}",
        task_id=envelope.task_id, worker_id=envelope.worker_id,
        status=status, result={"answer": answer} if status == "succeeded" else {},
        error=error, run_id=envelope.run_id, workspace_id=envelope.workspace_id,
        user_id=envelope.user_id, agent_id=envelope.agent_id,
    )


def episodic(db, run_scope, evidence_key):
    value, _ = record_episodic_evidence(
        db,
        scope=MemoryScope(run_scope.workspace_id, run_scope.user_id,
                          run_scope.agent_id, run_scope.run_id),
        task_type="support", outcome="succeeded",
        trajectory={"steps": ["validated", "completed"]}, metrics={"quality": 1.0},
        idempotency_key=evidence_key, redaction_status="redacted",
    )
    db.flush()
    return value.evidence_id


def test_repository_persists_records_and_ticket_consumption_survives_restart(session_factory):
    with session_factory() as db:
        orchestrator = DurableOrchestrator(db)
        orchestrator.create(
            scope(), workers=[worker()],
            tasks=[TaskPlan("task-1", "worker-1", {"value": "x"})],
        )
        repo = MultiAgentRepository(db)
        repo.save_artifact(
            scope(), artifact_id="artifact-1", worker_id="worker-1",
            storage_ref="minio://atlas/run-1/result.json", media_type="application/json",
            sha256="a" * 64,
        )
        ticket = repo.issue_authorization(
            scope(), worker_id="worker-1", tool_name="refund",
            parameters={"order": "A", "amount": 10}, ticket_scope={"refund:write"},
        )
        assert repo.authorize_tool(
            scope(), worker_id="worker-1", tool_name="refund",
            parameters={"order": "A", "amount": 10},
            policy=ToolPolicy("confirm", frozenset({"refund:write"})),
            user_grant={"refund"}, orchestrator_grant={"refund"},
            worker_grant={"refund"}, current_authorization={"refund"},
            ticket_id=ticket.ticket_id,
        ) == (True, "authorized")

    # A new Session simulates a different process: consumption must still deny replay.
    with session_factory() as db:
        repo = MultiAgentRepository(db)
        assert repo.authorize_tool(
            scope(), worker_id="worker-1", tool_name="refund",
            parameters={"order": "A", "amount": 10},
            policy=ToolPolicy("confirm", frozenset({"refund:write"})),
            user_grant={"refund"}, orchestrator_grant={"refund"},
            worker_grant={"refund"}, current_authorization={"refund"},
            ticket_id=ticket.ticket_id,
        ) == (False, "replayed_ticket")
        assert db.scalar(select(ToolAuthorizationRecord).where(
            ToolAuthorizationRecord.ticket_id == ticket.ticket_id
        )).status == "consumed"
        assert len(db.scalars(select(WorkerRecord)).all()) == 1
        assert len(db.scalars(select(TaskEnvelopeRecord)).all()) == 1
        actions = {row.action for row in db.scalars(select(GovernanceAuditRecord)).all()}
        assert {"worker.created", "task.enveloped", "artifact.recorded",
                "tool.authorization.consumed"}.issubset(actions)


def test_ticket_scope_parameter_and_permission_fail_closed(session_factory):
    with session_factory() as db:
        DurableOrchestrator(db).create(
            scope(), workers=[worker()],
            tasks=[TaskPlan("task-1", "worker-1", {"value": "x"})],
        )
        repo = MultiAgentRepository(db)
        ticket = repo.issue_authorization(
            scope(), worker_id="worker-1", tool_name="refund",
            parameters={"amount": 10}, ticket_scope={"refund:write"},
        )
        common = dict(
            worker_id="worker-1", tool_name="refund",
            policy=ToolPolicy("confirm", frozenset({"refund:write"})),
            user_grant={"refund"}, orchestrator_grant={"refund"},
            worker_grant={"refund"}, current_authorization={"refund"},
            ticket_id=ticket.ticket_id,
        )
        assert repo.authorize_tool(scope(), parameters={"amount": 11}, **common) == (
            False, "parameters_changed"
        )
        denied = dict(common)
        denied["worker_grant"] = set()
        assert repo.authorize_tool(scope(), parameters={"amount": 10}, **denied) == (
            False, "tool_not_in_permission_intersection"
        )
        with pytest.raises(LookupError, match="not found"):
            repo.authorize_tool(
                scope(workspace_id="other"), parameters={"amount": 10}, **common
            )


def test_parallel_dependency_retry_aggregation_and_persistent_results(session_factory):
    with session_factory() as db:
        service = DurableOrchestrator(db)
        service.create(
            scope(), workers=[worker("a"), worker("b"), worker("join")],
            tasks=[
                TaskPlan("a-task", "a", {"value": "a"}),
                TaskPlan("b-task", "b", {"value": "b"}),
                TaskPlan("join-task", "join", {"value": "join"},
                         frozenset({"a-task", "b-task"})),
            ], concurrency=2,
        )
        active = 0
        maximum_active = 0
        attempts = {"a-task": 0, "b-task": 0, "join-task": 0}

        async def execute(envelope, _spec):
            nonlocal active, maximum_active
            attempts[envelope.task_id] += 1
            active += 1
            maximum_active = max(maximum_active, active)
            await asyncio.sleep(0.01)
            active -= 1
            if envelope.task_id == "a-task" and attempts[envelope.task_id] == 1:
                return result_for(envelope, status="failed", error="transient")
            return result_for(envelope, answer=envelope.task_id)

        aggregate = asyncio.run(service.execute(scope(), execute))
        assert aggregate["status"] == "succeeded"
        assert maximum_active == 2
        assert attempts == {"a-task": 2, "b-task": 1, "join-task": 1}
        assert db.get(OrchestrationRunRecord, "run-1").status == "succeeded"
        assert len(db.scalars(select(ResultEnvelopeRecord)).all()) == 4


def test_restart_recovers_dispatched_task_and_cancel_propagates(session_factory):
    with session_factory() as db:
        service = DurableOrchestrator(db)
        service.create(
            scope(), workers=[worker()],
            tasks=[TaskPlan("task-1", "worker-1", {"value": "x"})],
        )
        record = db.get(OrchestrationRunRecord, "run-1")
        # JSON columns do not observe in-place mutations below the top-level
        # mapping. Simulate a real process checkpoint by assigning a fully
        # detached payload, as the repository does in production.
        state = deepcopy(record.scheduler_state)
        state["tasks"]["task-1"]["status"] = "running"
        state["tasks"]["task-1"]["attempts"] = 1
        record.scheduler_state = state
        record.status = "running"
        db.commit()

    with session_factory() as db:
        service = DurableOrchestrator(db)

        async def execute(envelope, _spec):
            return result_for(envelope, answer="recovered")

        aggregate = asyncio.run(service.execute(scope(), execute))
        assert aggregate["status"] == "succeeded"
        audit = db.scalars(select(GovernanceAuditRecord).where(
            GovernanceAuditRecord.action == "orchestration.recovered"
        )).all()
        assert len(audit) == 1

        other = scope("run-cancel")
        service.create(
            other, workers=[worker()], tasks=[
                TaskPlan("root", "worker-1", {"value": "x"}),
                TaskPlan("child", "worker-1", {"value": "y"}, frozenset({"root"})),
            ], concurrency=1,
        )
        cancelled = service.cancel(other, actor_id="user-1")
        assert {item["status"] for item in cancelled["tasks"].values()} == {"cancelled"}


def test_persistent_cancellation_probe_stops_worker_without_local_task_handle(session_factory):
    with session_factory() as db:
        service = DurableOrchestrator(db)
        service.create(
            scope(), workers=[worker()],
            tasks=[TaskPlan("task-1", "worker-1", {"value": "x"})],
        )
        probes = 0

        async def execute(envelope, _spec):
            await asyncio.sleep(5)
            return result_for(envelope, answer="too late")

        def cancellation_probe():
            nonlocal probes
            probes += 1
            return True

        aggregate = asyncio.run(service.execute(
            scope(), execute, cancellation_probe=cancellation_probe
        ))
        assert probes == 1
        assert aggregate["status"] == "cancelled"
        assert db.get(OrchestrationRunRecord, "run-1").status == "cancelled"
        result = db.scalar(select(ResultEnvelopeRecord).where(
            ResultEnvelopeRecord.run_id == "run-1"
        ))
        assert result.envelope["status"] == "cancelled"


def test_remote_process_cancel_preempts_model_and_persists_runtime_and_result(session_factory):
    """A control-plane Session can preempt a worker owned by another Session.

    This models two API processes: neither shares an asyncio task registry with
    the other; the only cancellation signal is the durable orchestration row.
    """
    with session_factory() as setup_db:
        setup_db.add(Agent(id="agent-1", name="Parent", current_version_id="version-cancel"))
        setup_db.add(AgentVersion(
            id="version-cancel", agent_id="agent-1", version_no=1, label="draft",
            snapshot_json='{"system_prompt":"parent","model":"test"}',
        ))
        setup_db.commit()
        DurableOrchestrator(setup_db).create(
            scope(), workers=[worker()],
            tasks=[TaskPlan("task-1", "worker-1", {"value": "x"}, timeout_seconds=30)],
            parent_agent_version_id="version-cancel",
        )

    async def scenario():
        model_started = asyncio.Event()
        model_cancelled = asyncio.Event()

        async def slow_model(*_args, **_kwargs):
            model_started.set()
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                model_cancelled.set()
                raise

        def persistent_probe():
            with session_factory() as probe_db:
                status = probe_db.scalar(select(OrchestrationRunRecord.status).where(
                    OrchestrationRunRecord.run_id == "run-1",
                    OrchestrationRunRecord.workspace_id == "ws-1",
                    OrchestrationRunRecord.user_id == "user-1",
                    OrchestrationRunRecord.agent_id == "agent-1",
                ))
            return status == "cancelled"

        with session_factory() as worker_db:
            executor = SubagentRuntimeExecutor(
                worker_db, scope(), model_complete=slow_model,
                session_factory=session_factory,
                cancellation_probe=persistent_probe,
                cancellation_poll_seconds=0.01,
            )
            execution = asyncio.create_task(DurableOrchestrator(worker_db).execute(
                scope(), executor, cancellation_probe=persistent_probe,
            ))
            await asyncio.wait_for(model_started.wait(), timeout=1)

            # This separate Session represents a different API process. It has
            # no reference to ``execution`` or to the Runtime supervisor task.
            with session_factory() as control_db:
                DurableOrchestrator(control_db).cancel(scope(), actor_id="user-1")

            aggregate = await asyncio.wait_for(execution, timeout=1)
            return aggregate, model_cancelled.is_set()

    aggregate, model_was_cancelled = asyncio.run(scenario())
    assert aggregate["status"] == "cancelled", aggregate
    assert model_was_cancelled is True

    with session_factory() as verify_db:
        orchestration = verify_db.get(OrchestrationRunRecord, "run-1")
        runtime = verify_db.scalar(select(RuntimeRun).where(
            RuntimeRun.source == "subagent",
            RuntimeRun.workspace_id == "ws-1",
        ))
        result = verify_db.scalar(select(ResultEnvelopeRecord).where(
            ResultEnvelopeRecord.run_id == "run-1",
            ResultEnvelopeRecord.task_id == "task-1",
        ))
        assert orchestration.status == "cancelled"
        assert runtime is not None and runtime.status == "cancelled"
        assert runtime.ended_at is not None
        assert result is not None and result.envelope["status"] == "cancelled"
        assert result.envelope["runtime_run_id"] == runtime.id


def test_remote_cancel_at_write_tool_boundary_blocks_followup_and_audits_uncertainty(session_factory):
    with session_factory() as setup_db:
        setup_db.add(Agent(id="agent-1", name="Parent", current_version_id="version-tool-cancel"))
        setup_db.add(AgentVersion(
            id="version-tool-cancel", agent_id="agent-1", version_no=1, label="draft",
            snapshot_json='{"system_prompt":"parent","model":"test"}',
        ))
        setup_db.commit()
        DurableOrchestrator(setup_db).create(
            scope(), workers=[worker()], tasks=[TaskPlan(
                "task-1", "worker-1",
                {"value": "x", "tool_call": {"name": "refund", "arguments": {"amount": 10}}},
                timeout_seconds=30,
            )], parent_agent_version_id="version-tool-cancel",
            user_tool_grants={"refund"}, orchestrator_tool_grants={"refund"},
        )
        ticket = MultiAgentRepository(setup_db).issue_authorization(
            scope(), worker_id="worker-1", tool_name="refund",
            parameters={"amount": 10}, ticket_scope={"refund:write"},
            resource_version="order-v1",
        )
        authorization = {
            "ticket_id": ticket.ticket_id,
            "nonce": ticket.nonce,
            "resource_version": "order-v1",
        }

    async def scenario():
        tool_started = asyncio.Event()
        tool_cancelled = asyncio.Event()
        model_called = False

        async def write_tool(_db, _arguments):
            tool_started.set()
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                tool_cancelled.set()
                raise

        async def model(*_args, **_kwargs):
            nonlocal model_called
            model_called = True
            return '{"answer":"must not execute"}'

        def persistent_probe():
            with session_factory() as probe_db:
                return probe_db.scalar(select(OrchestrationRunRecord.status).where(
                    OrchestrationRunRecord.run_id == "run-1"
                )) == "cancelled"

        registry = ServerToolRegistry({
            "refund": TrustedTool(
                "refund", "write", write_tool, frozenset({"refund:write"})
            )
        })
        with session_factory() as worker_db:
            execution = asyncio.create_task(DurableOrchestrator(worker_db).execute(
                scope(), SubagentRuntimeExecutor(
                    worker_db, scope(), model_complete=model,
                    session_factory=session_factory, tool_registry=registry,
                    authorizations={"task-1": authorization},
                    cancellation_probe=persistent_probe,
                    cancellation_poll_seconds=0.01,
                ), resume_task_ids={"task-1"}, cancellation_probe=persistent_probe,
            ))
            await asyncio.wait_for(tool_started.wait(), timeout=1)
            with session_factory() as control_db:
                DurableOrchestrator(control_db).cancel(scope(), actor_id="user-1")
            aggregate = await asyncio.wait_for(execution, timeout=1)
            return aggregate, tool_cancelled.is_set(), model_called

    aggregate, tool_was_cancelled, model_was_called = asyncio.run(scenario())
    assert aggregate["status"] == "cancelled"
    assert tool_was_cancelled is True
    assert model_was_called is False

    with session_factory() as verify_db:
        audit = verify_db.scalar(select(GovernanceAuditRecord).where(
            GovernanceAuditRecord.run_id == "run-1",
            GovernanceAuditRecord.action == "worker.cancelled_after_side_effect_started",
        ))
        assert audit is not None
        assert audit.details == {
            "tool_name": "refund",
            "side_effect_status": "unknown_not_rolled_back",
            "subsequent_steps": "blocked",
        }
        assert verify_db.scalar(select(RuntimeRun).where(
            RuntimeRun.source == "subagent"
        )) is None


def test_dynamic_mcp_tools_join_server_registry_with_write_policy(monkeypatch, session_factory):
    calls = []

    monkeypatch.setattr("app.multi_agent_runtime_adapter.github_mcp.is_configured", lambda: True)

    async def tool_specs():
        return ([
            {"type": "function", "function": {"name": "search_repositories"}},
            {"type": "function", "function": {"name": "create_issue"}},
        ], {"create_issue"})

    async def call_tool(name, arguments):
        calls.append((name, json.loads(arguments)))
        return "ok"

    monkeypatch.setattr("app.multi_agent_runtime_adapter.github_mcp.tool_specs", tool_specs)
    monkeypatch.setattr("app.multi_agent_runtime_adapter.github_mcp.call_tool", call_tool)
    registry = asyncio.run(ServerToolRegistry.discover(["github"]))
    assert registry.get("search_repositories").access == "read"
    write = registry.get("create_issue")
    assert write.access == "write"
    assert write.required_scope == frozenset({"tool:create_issue:write"})
    with session_factory() as db:
        assert asyncio.run(write.invoke(db, {"title": "Bug"})) == "ok"
    assert calls == [("create_issue", {"title": "Bug"})]


def test_dynamic_mcp_discovery_warning_is_bounded_and_does_not_trust_tools(monkeypatch):
    monkeypatch.setattr("app.multi_agent_runtime_adapter.github_mcp.is_configured", lambda: True)

    async def failed_specs():
        raise ConnectionError("https://token@example.invalid/private")

    monkeypatch.setattr("app.multi_agent_runtime_adapter.github_mcp.tool_specs", failed_specs)
    registry = asyncio.run(ServerToolRegistry.discover(["github"]))
    assert registry.discovery_warnings == ({
        "provider": "github",
        "category": "dependency",
        "error_type": "ConnectionError",
        "decision": "fail_closed",
    },)
    assert "search_repositories" not in registry.names()
    assert "token@example" not in json.dumps(registry.discovery_warnings)


def test_empty_user_or_orchestrator_grant_is_deny_all(session_factory):
    calls = []

    async def read_tool(_db, arguments):
        calls.append(arguments)
        return {"found": True}

    registry = ServerToolRegistry({
        "search": TrustedTool("search", "read", read_tool)
    })
    with session_factory() as db:
        db.add(Agent(id="agent-1", name="Parent", current_version_id="version-1"))
        db.add(AgentVersion(
            id="version-1", agent_id="agent-1", version_no=1, label="draft",
            snapshot_json='{"system_prompt":"parent-v1","model":"test"}',
        ))
        db.commit()
        service = DurableOrchestrator(db)
        service.create(
            scope(), workers=[worker()], tasks=[TaskPlan(
                "task-1", "worker-1",
                {"value": "x", "tool_call": {"name": "search", "arguments": {}}},
            )], parent_agent_version_id="version-1",
            user_tool_grants=set(), orchestrator_tool_grants={"search"},
        )

        async def model(*_args, **_kwargs):
            return '{"answer":"must not run"}'

        aggregate = asyncio.run(service.execute(
            scope(), SubagentRuntimeExecutor(
                db, scope(), model_complete=model, session_factory=session_factory,
                tool_registry=registry,
            )
        ))
        assert aggregate["status"] == "partial"
        assert calls == []


@pytest.mark.parametrize("category", ["permission", "validation", "side_effect"])
def test_non_transient_worker_failures_are_never_retried(session_factory, category):
    with session_factory() as db:
        service = DurableOrchestrator(db)
        service.create(
            scope(), workers=[worker()],
            tasks=[TaskPlan("task-1", "worker-1", {"value": "x"}, max_attempts=3)],
        )
        attempts = 0

        async def execute(envelope, _spec):
            nonlocal attempts
            attempts += 1
            return ResultEnvelope(
                envelope_id=f"failure-{category}", task_id=envelope.task_id,
                worker_id=envelope.worker_id, status="failed", result={},
                error=category, error_category=category, run_id=envelope.run_id,
                workspace_id=envelope.workspace_id, user_id=envelope.user_id,
                agent_id=envelope.agent_id,
            )

        aggregate = asyncio.run(service.execute(scope(), execute))
        assert aggregate["status"] == "partial"
        assert attempts == 1


def test_write_tool_pauses_then_executes_once_with_bound_ticket(session_factory):
    calls = []

    async def write_tool(_db, arguments):
        calls.append(arguments)
        return {"written": True}

    registry = ServerToolRegistry({
        "refund": TrustedTool(
            "refund", "write", write_tool, frozenset({"refund:write"})
        )
    })
    with session_factory() as db:
        db.add(Agent(id="agent-1", name="Parent", current_version_id="version-1"))
        db.add(AgentVersion(
            id="version-1", agent_id="agent-1", version_no=1, label="draft",
            snapshot_json='{"system_prompt":"parent-v1","model":"test"}',
        ))
        db.commit()
        service = DurableOrchestrator(db)
        service.create(
            scope(), workers=[worker()], tasks=[TaskPlan(
                "task-1", "worker-1",
                {"value": "x", "tool_call": {"name": "refund", "arguments": {"amount": 10}}},
            )], parent_agent_version_id="version-1",
            user_tool_grants={"refund"}, orchestrator_tool_grants={"refund"},
        )

        async def model(*_args, **_kwargs):
            return '{"answer":"done"}'

        paused = asyncio.run(service.execute(
            scope(), SubagentRuntimeExecutor(
                db, scope(), model_complete=model, session_factory=session_factory,
                tool_registry=registry,
            )
        ))
        assert paused["status"] == "paused"
        assert calls == []

        ticket = MultiAgentRepository(db).issue_authorization(
            scope(), worker_id="worker-1", tool_name="refund",
            parameters={"amount": 10}, ticket_scope={"refund:write"},
            resource_version="order-v1",
        )
        authorizations = {"task-1": {
            "ticket_id": ticket.ticket_id, "nonce": ticket.nonce,
            "resource_version": "order-v1",
        }}
        completed = asyncio.run(service.execute(
            scope(), SubagentRuntimeExecutor(
                db, scope(), model_complete=model, session_factory=session_factory,
                tool_registry=registry, authorizations=authorizations,
            ), resume_task_ids={"task-1"},
        ))
        assert completed["status"] == "succeeded"
        assert calls == [{"amount": 10}]
        assert completed["results"][0]["runtime_run_id"]


def test_candidate_skill_is_hidden_until_publish_then_versions_and_rolls_back(session_factory):
    with session_factory() as db:
        DurableOrchestrator(db).create(
            scope(), workers=[worker()], tasks=[TaskPlan("task", "worker-1", {"value": "x"})]
        )
        publisher = CandidateAssetPublisher(
            db, replay_executor=lambda content, evidence: {
                "passed": len(evidence), "total": len(evidence), "details": {"runner": "test"}
            },
        )
        evidence_id = episodic(db, scope(), "skill-evidence")
        candidate = publisher.propose(
            scope(), candidate_type="skill", name="Refund helper",
            content={"content": "raw trajectory"}, evidence_ids=[evidence_id],
            actor_id="worker-1", idempotency_key="candidate-refund",
        )
        assert db.scalars(select(Skill)).all() == []
        with pytest.raises(ValueError, match="sensitive"):
            publisher.redact(scope(), candidate.candidate_id,
                             redacted_content={"secret": "leak"}, actor_id="redactor")
        safe_v1 = {
            "description": "Use for approved refund requests",
            "summary": "Validated refund workflow",
            "content": "Verify order, then create refund",
            "category_path": "service/refunds",
            "use_when": ["refund approved"],
            "do_not_use_when": ["refund not approved"],
        }
        publisher.redact(scope(), candidate.candidate_id,
                         redacted_content=safe_v1, actor_id="redactor")
        publisher.evaluate(scope(), candidate.candidate_id,
                           replay_report={"replay_pass_rate": 0.0}, actor_id="evaluator")
        publisher.approve(scope(), candidate.candidate_id,
                          actor_id="human-1", human_approval=True)
        v1 = publisher.publish(scope(), candidate.candidate_id,
                               version="1.0.0", actor_id="human-1")
        skill = db.get(Skill, v1.asset_id)
        assert skill.status == "active" and skill.version == "1.0.0"
        assert skill.content == safe_v1["content"]

        publisher.redact(scope(), candidate.candidate_id,
                         redacted_content={**safe_v1, "content": "Improved refund steps"},
                         actor_id="redactor")
        publisher.evaluate(scope(), candidate.candidate_id,
                           replay_report={"replay_pass_rate": 1.0}, actor_id="evaluator")
        publisher.approve(scope(), candidate.candidate_id,
                          actor_id="human-1", human_approval=True)
        v2 = publisher.publish(scope(), candidate.candidate_id,
                               version="2.0.0", actor_id="human-1")
        assert v2.asset_id == v1.asset_id and skill.version == "2.0.0"
        rolled_back = publisher.rollback(scope(), candidate.candidate_id,
                                         version="1.0.0", actor_id="human-1")
        assert rolled_back.active is True
        assert skill.version == "1.0.0" and skill.content == safe_v1["content"]
        versions = db.scalars(select(CandidateAssetVersionRecord).where(
            CandidateAssetVersionRecord.candidate_id == candidate.candidate_id
        )).all()
        assert sum(item.active for item in versions) == 1


def test_candidate_agent_publishes_real_immutable_agent_version(session_factory):
    with session_factory() as db:
        DurableOrchestrator(db).create(
            scope(), workers=[worker()], tasks=[TaskPlan("task", "worker-1", {"value": "x"})]
        )
        publisher = CandidateAssetPublisher(
            db, replay_executor=lambda content, evidence: {"passed": 1, "total": 1}
        )
        evidence_id = episodic(db, scope(), "agent-evidence")
        candidate = publisher.propose(
            scope(), candidate_type="agent", name="Support specialist",
            content={"system_prompt": "raw"}, evidence_ids=[evidence_id],
            actor_id="worker-1", idempotency_key="candidate-agent",
        )
        publisher.redact(
            scope(), candidate.candidate_id,
            redacted_content={"system_prompt": "Handle approved support cases", "model": "test-model"},
            actor_id="redactor",
        )
        publisher.evaluate(scope(), candidate.candidate_id,
                           replay_report={"replay_pass_rate": 1.0}, actor_id="evaluator")
        publisher.approve(scope(), candidate.candidate_id,
                          actor_id="human-1", human_approval=True)
        published = publisher.publish(scope(), candidate.candidate_id,
                                      version="1.0.0", actor_id="human-1")
        agent = db.get(Agent, published.asset_id)
        version = db.get(AgentVersion, agent.published_version_id)
        assert agent.status == "published"
        assert version.label == "published"
        assert json.loads(version.snapshot_json)["candidate_version"] == "1.0.0"
