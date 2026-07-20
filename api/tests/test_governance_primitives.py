import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.governance_models import (
    GovernanceBase,
    GovernanceAuditRecord,
    SkillRouterDecision,
)
from app.memory_ledger import (
    MemoryScope,
    WorkingStateConflict,
    append_event,
    checkpoint_state,
    explain_memory_retrieval,
    load_checkpoint,
    load_profile_card,
    propose_procedural_candidate,
    rebuild_checkpoint,
    record_episodic_evidence,
    transition_procedural_candidate,
    tombstone_profile_card,
    update_profile_card,
)
from app.multi_agent_runtime import (
    CandidateAsset,
    DagScheduler,
    DagTask,
    OrchestratorTeam,
    ResultEnvelope,
    ToolPolicy,
    WorkerSpec,
    aggregate_results,
    authorize_tool_call,
    contract_schemas,
    execute_ready_parallel,
    issue_ticket,
    utcnow,
)
from app.skill_router import (
    load_selected_skill_content,
    persist_router_decision,
    route_skills,
    search_skill_metadata,
    validate_skill_metadata,
)


@pytest.fixture()
def db():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    GovernanceBase.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def scope(**changes):
    values = {
        "workspace_id": "ws-a", "user_id": "user-a", "agent_id": "agent-a",
        "run_id": "run-a", "worker_id": "worker-a",
    }
    values.update(changes)
    return MemoryScope(**values)


def governed_skills():
    return [
        {
            "id": "meeting", "name": "Meeting notes", "category_path": ["work", "writing"],
            "summary": "Turn meeting notes into actions", "use_when": ["meeting notes"],
            "do_not_use_when": ["medical diagnosis"], "input_schema": {"type": "object"},
            "output_schema": {"type": "object"}, "permissions": ["documents:read"],
            "version": "1.0.0", "status": "published", "content": "private meeting procedure",
        },
        {
            "id": "support", "name": "Support reply", "category_path": ["service", "support"],
            "summary": "Draft a customer support reply", "use_when": ["support response"],
            "do_not_use_when": ["legal filing"], "input_schema": {"type": "object"},
            "output_schema": {"type": "object"}, "permissions": ["tickets:read"],
            "version": "2.0.0", "status": "published", "content": "private support procedure",
        },
        {
            "id": "draft", "name": "Draft only", "category_path": ["work"],
            "summary": "Unpublished", "use_when": [], "do_not_use_when": [],
            "input_schema": {}, "output_schema": {}, "permissions": [],
            "version": "0.1.0", "status": "draft", "content": "must not be disclosed",
        },
    ]


def test_all_governance_tables_initialize_additively(db):
    table_names = set(GovernanceBase.metadata.tables)
    assert {
        "memory_ledger_events", "memory_semantic_facts", "memory_checkpoints", "memory_user_profile_cards",
        "memory_episodic_evidence", "memory_procedural_candidates",
        "skill_router_decisions", "orchestration_workers",
        "orchestration_task_envelopes", "orchestration_result_envelopes",
        "orchestration_tool_authorizations", "orchestration_artifacts",
        "governance_audit_records",
    }.issubset(table_names)
    db.add(GovernanceAuditRecord(
        workspace_id="ws", user_id="user", agent_id="agent",
        action="schema.test", decision="allowed", details={},
    ))
    db.flush()


def test_ledger_idempotency_is_scoped_and_never_leaks_tenant_payload(db):
    first, created = append_event(
        db, scope=scope(), event_type="memory.note", payload={"secret": "tenant-a"},
        idempotency_key="same-delivery-key",
    )
    second, second_created = append_event(
        db, scope=scope(workspace_id="ws-b"), event_type="memory.note",
        payload={"secret": "tenant-b"}, idempotency_key="same-delivery-key",
    )
    assert created and second_created
    assert first.event_id != second.event_id
    assert second.payload == {"secret": "tenant-b"}


def test_working_checkpoint_compare_and_swap_rebuild_and_worker_isolation(db):
    working = scope(worker_id="worker-a")
    checkpoint, created = checkpoint_state(
        db, scope=working, state_kind="working", state_key="plan",
        snapshot={"step": 1}, expected_version=0, idempotency_key="checkpoint-1",
    )
    assert created and checkpoint.version == 1
    retry, retry_created = checkpoint_state(
        db, scope=working, state_kind="working", state_key="plan",
        snapshot={"step": 999}, expected_version=0, idempotency_key="checkpoint-1",
    )
    assert retry_created is False and retry.version == 1
    with pytest.raises(WorkingStateConflict):
        checkpoint_state(
            db, scope=working, state_kind="working", state_key="plan",
            snapshot={"step": 2}, expected_version=0, idempotency_key="stale-write",
        )
    updated, _ = checkpoint_state(
        db, scope=working, state_kind="working", state_key="plan",
        snapshot={"step": 2}, expected_version=1, idempotency_key="checkpoint-2",
    )
    assert updated.version == 2
    assert rebuild_checkpoint(db, scope=working, state_kind="working", state_key="plan") == {
        "version": 2, "snapshot": {"step": 2},
    }
    assert load_checkpoint(
        db, scope=scope(worker_id="worker-b"), state_kind="working", state_key="plan"
    ) is None


def test_structured_profile_card_is_ledger_backed_scoped_and_tombstoned(db):
    card, created = update_profile_card(
        db,
        scope=scope(),
        fields={"locale": "zh-CN", "home_city": "Hangzhou"},
        expected_version=0,
        idempotency_key="profile-v1",
    )
    assert created and card.version == 1
    assert explain_memory_retrieval(db, scope=scope())["profile_card"]["fields"]["home_city"] == "Hangzhou"
    assert load_profile_card(db, scope=scope(workspace_id="ws-b")) is None
    with pytest.raises(WorkingStateConflict):
        update_profile_card(
            db,
            scope=scope(),
            fields={"home_city": "Shanghai"},
            expected_version=0,
            idempotency_key="profile-stale",
        )
    removed, removed_now = tombstone_profile_card(
        db,
        scope=scope(),
        expected_version=1,
        idempotency_key="profile-delete",
    )
    assert removed_now and removed.version == 2
    replayed, replayed_now = tombstone_profile_card(
        db,
        scope=scope(),
        expected_version=1,
        idempotency_key="profile-delete",
    )
    assert replayed_now is False and replayed.version == 2
    assert explain_memory_retrieval(db, scope=scope())["profile_card"] is None


def test_procedural_candidate_cannot_publish_without_redaction_evaluation_approval(db):
    pending, _ = record_episodic_evidence(
        db, scope=scope(), task_type="refund", outcome="succeeded",
        trajectory={"messages": ["pii"]}, metrics={"quality": 0.9},
        idempotency_key="evidence-pending",
    )
    with pytest.raises(ValueError, match="successful and redacted"):
        propose_procedural_candidate(
            db, scope=scope(), candidate_type="skill", name="Refund helper",
            content={"steps": []}, evidence_ids=[pending.evidence_id], idempotency_key="candidate-bad",
        )
    evidence, _ = record_episodic_evidence(
        db, scope=scope(), task_type="refund", outcome="succeeded",
        trajectory={"messages": ["[redacted]"]}, metrics={"quality": 0.95},
        redaction_status="redacted", idempotency_key="evidence-redacted",
    )
    candidate, _ = propose_procedural_candidate(
        db, scope=scope(), candidate_type="skill", name="Refund helper",
        content={"steps": ["verify"]}, evidence_ids=[evidence.evidence_id],
        idempotency_key="candidate-good",
    )
    with pytest.raises(ValueError, match="cannot publish"):
        transition_procedural_candidate(
            db, scope=scope(), candidate_id=candidate.candidate_id, action="publish",
            actor_id="human", asset_version="1.0.0", idempotency_key="publish-too-soon",
        )
    transition_procedural_candidate(
        db, scope=scope(), candidate_id=candidate.candidate_id, action="evaluate",
        actor_id="evaluator", evaluation={"replay_pass_rate": 1.0}, idempotency_key="evaluate",
    )
    transition_procedural_candidate(
        db, scope=scope(), candidate_id=candidate.candidate_id, action="approve",
        actor_id="human", idempotency_key="approve",
    )
    published, _ = transition_procedural_candidate(
        db, scope=scope(), candidate_id=candidate.candidate_id, action="publish",
        actor_id="human", asset_version="1.0.0", idempotency_key="publish",
    )
    assert published.status == "published" and published.asset_version == "1.0.0"


def test_skill_validation_search_tree_routing_pagination_and_audit(db):
    skills = governed_skills()
    assert validate_skill_metadata(skills[0])["id"] == "meeting"
    with pytest.raises(ValueError, match="missing required fields"):
        validate_skill_metadata({"id": "incomplete"})
    page = search_skill_metadata(
        skills, query="meeting", category_prefix=["work"], statuses={"published"},
        permitted_ids={"meeting", "draft"}, page=1, page_size=1,
    )
    assert page["total"] == 1
    assert page["items"][0]["id"] == "meeting"
    assert "content" not in page["items"][0]
    decision = route_skills(
        skills, query="meeting notes", category_prefix=["work"], permitted_ids={"meeting"},
        keyword_scores={"meeting": 0.9}, vector_scores={"meeting": 0.9},
    )
    assert decision["selected_ids"] == ["meeting"]
    assert decision["tree_stage"] == {"candidate_count_before": 3, "candidate_count_after": 1}
    record = persist_router_decision(db, scope=scope(), decision=decision)
    assert db.get(SkillRouterDecision, record.decision_id).selected_ids == ["meeting"]
    assert load_selected_skill_content(
        skills, ["meeting", "support"], permitted_ids={"meeting"}
    ) == [{"id": "meeting", "name": "Meeting notes", "content": "private meeting procedure"}]


def test_unpublished_skill_is_never_routed_or_progressively_loaded():
    skills = governed_skills()
    decision = route_skills(
        skills,
        query="unpublished",
        permitted_ids={"draft"},
        manual_ids=["draft"],
        keyword_scores={"draft": 1.0},
        vector_scores={"draft": 1.0},
    )
    assert decision["decision"] == "no_selection"
    assert decision["selected_ids"] == []
    assert "manual_selection_filtered_by_status_or_tree" in decision["reasons"]
    assert load_selected_skill_content(
        skills, ["draft"], permitted_ids={"draft"}
    ) == []


def test_versioned_contracts_orchestrator_scope_and_result_validation():
    schemas = contract_schemas()
    assert all(schema["$id"].endswith("/v1") for schema in schemas.values())
    team = OrchestratorTeam("run", "ws", "user", "agent")
    spec = team.create_worker(WorkerSpec(
        "researcher", "1.0.0", "research", "find answer",
        {"type": "object", "required": ["query"], "properties": {"query": {"type": "string"}}},
        {"type": "object", "required": ["answer"], "properties": {"answer": {"type": "string"}}},
        frozenset({"search"}),
    ))
    assert spec.worker_id == "researcher"
    task = team.make_task(task_id="t1", worker_id="researcher", payload={"query": "Atlas"})
    accepted = team.accept_result(ResultEnvelope(
        "r1", "t1", "researcher", "succeeded", {"answer": "ok"},
        run_id="run", workspace_id="ws", user_id="user", agent_id="agent",
    ))
    assert accepted.result == {"answer": "ok"}
    for changed_scope in (
        {"run_id": "other", "workspace_id": "ws", "user_id": "user", "agent_id": "agent"},
        {"run_id": "run", "workspace_id": "other", "user_id": "user", "agent_id": "agent"},
        {"run_id": "run", "workspace_id": "ws", "user_id": "other", "agent_id": "agent"},
        {"run_id": "run", "workspace_id": "ws", "user_id": "user", "agent_id": "other"},
    ):
        with pytest.raises(PermissionError, match="scope"):
            team.accept_result(ResultEnvelope(
                "r2", task.task_id, "researcher", "succeeded", {"answer": "leak"},
                **changed_scope,
            ))


def test_worker_specs_tasks_and_results_detach_nested_mutable_context():
    input_schema = {
        "type": "object",
        "required": ["request"],
        "properties": {"request": {"type": "object"}},
    }
    output_schema = {
        "type": "object",
        "required": ["answer"],
        "properties": {"answer": {"type": "object"}},
    }
    team = OrchestratorTeam("run", "ws", "user", "agent")
    created = team.create_worker(WorkerSpec(
        "isolated", "1.0.0", "worker", "isolated work", input_schema, output_schema,
    ))
    input_schema["required"].append("injected")
    assert created.input_schema["required"] == ["request"]

    payload = {"request": {"secret": "worker-a"}}
    task = team.make_task(task_id="isolated-task", worker_id="isolated", payload=payload)
    payload["request"]["secret"] = "mutated outside envelope"
    assert task.payload == {"request": {"secret": "worker-a"}}

    worker_result = {"answer": {"value": "accepted"}}
    accepted = team.accept_result(ResultEnvelope(
        "isolated-result", task.task_id, "isolated", "succeeded", worker_result,
        run_id="run", workspace_id="ws", user_id="user", agent_id="agent",
    ))
    worker_result["answer"]["value"] = "mutated after acceptance"
    assert accepted.result == {"answer": {"value": "accepted"}}


def test_tool_policy_requires_permission_intersection_ticket_scope_and_anti_replay():
    now = utcnow()
    parameters = {"ticket_id": "123", "decision": "refund"}
    ticket = issue_ticket(
        "run", "worker", "refund", parameters,
        scope={"refund:write"}, ttl_seconds=30, now=now,
    )
    used = set()
    args = dict(
        tool_name="refund", parameters=parameters,
        policy=ToolPolicy("confirm", frozenset({"refund:write"})),
        user_grant={"refund"}, orchestrator_grant={"refund"},
        worker_grant={"refund"}, current_authorization={"refund"},
        run_id="run", worker_id="worker", used_ticket_ids=used, now=now,
    )
    assert authorize_tool_call(**args, ticket=ticket) == (True, "authorized")
    assert authorize_tool_call(**args, ticket=ticket) == (False, "replayed_ticket")
    changed = dict(args)
    changed["parameters"] = {"ticket_id": "123", "decision": "double refund"}
    fresh = issue_ticket("run", "worker", "refund", parameters, scope={"refund:write"}, now=now)
    assert authorize_tool_call(**changed, ticket=fresh) == (False, "parameters_changed")
    denied = dict(args)
    denied["worker_grant"] = set()
    assert authorize_tool_call(**denied, ticket=None) == (
        False, "tool_not_in_permission_intersection",
    )


def test_dag_cycle_cancel_aggregation_conflicts_and_candidate_gate():
    with pytest.raises(ValueError, match="acyclic"):
        DagScheduler([DagTask("a", "w1", {"b"}), DagTask("b", "w2", {"a"})])
    scheduler = DagScheduler([DagTask("a", "w1"), DagTask("b", "w2")], concurrency=2)
    scheduler.dispatch()
    scheduler.cancel()
    assert scheduler.terminal()
    common = dict(run_id="run", workspace_id="ws", user_id="user", agent_id="agent")
    aggregation = aggregate_results([
        ResultEnvelope("r1", "a", "w1", "succeeded", {"answer": "x"}, **common),
        ResultEnvelope("r2", "b", "w2", "succeeded", {"answer": "y"}, **common),
    ], conflict_keys={"answer"})
    assert aggregation["status"] == "conflict"

    candidate = CandidateAsset("c1", "skill", "Reusable helper", {"pii": "raw"}, ("run-1",))
    with pytest.raises(ValueError, match="approved"):
        candidate.publish(actor_id="system", version="1.0.0")
    candidate.redact(actor_id="redactor", content={"steps": ["safe"]})
    candidate.evaluate(actor_id="evaluator", report={"replay_pass_rate": 1.0})
    candidate.approve(actor_id="human")
    assert candidate.publish(actor_id="human", version="1.0.0")["active"] is True


def test_dag_executor_runs_independent_workers_in_parallel_with_cap():
    scheduler = DagScheduler(
        [DagTask("a", "worker-a"), DagTask("b", "worker-b"), DagTask("c", "worker-c")],
        concurrency=2,
    )
    active = 0
    maximum_active = 0

    async def execute(task):
        nonlocal active, maximum_active
        active += 1
        maximum_active = max(maximum_active, active)
        await asyncio.sleep(0.01)
        active -= 1
        return ResultEnvelope(
            f"result-{task.task_id}", task.task_id, task.worker_id,
            "succeeded", {"answer": task.task_id},
            run_id="run", workspace_id="ws", user_id="user", agent_id="agent",
        )

    first_batch = asyncio.run(execute_ready_parallel(scheduler, execute))
    assert {item.task_id for item in first_batch} == {"a", "b"}
    assert maximum_active == 2
    second_batch = asyncio.run(execute_ready_parallel(scheduler, execute))
    assert [item.task_id for item in second_batch] == ["c"]
    assert scheduler.terminal()
