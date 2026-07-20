from sqlalchemy import select

from app.database import SessionLocal
from app.governance_models import OrchestrationRunRecord, ProceduralCandidate
from app.memory_ledger import MemoryScope, record_episodic_evidence
from app.multi_agent_service import deterministic_candidate_replay


OUTPUT_SCHEMA = {
    "type": "object",
    "required": ["case_id", "decision"],
    "properties": {
        "case_id": {"type": "string", "minLength": 1},
        "decision": {"type": "string", "enum": ["approve", "deny"]},
    },
    "additionalProperties": False,
}


def replay_evidence(*, expected=None, output_schema=None):
    return ({
        "evidence_type": "episodic",
        "evidence_id": "episode-1",
        "task_type": "refund",
        "trajectory": {
            "replay_fixture": {
                "input": {"case_id": "case-17", "eligible": True},
                "input_schema": {
                    "type": "object",
                    "required": ["case_id", "eligible"],
                    "properties": {
                        "case_id": {"type": "string", "minLength": 1},
                        "eligible": {"type": "boolean"},
                    },
                    "additionalProperties": False,
                },
                "expected_output": expected or {"case_id": "case-17", "decision": "approve"},
                "output_schema": output_schema or OUTPUT_SCHEMA,
                "assertions": [{"path": "$.decision", "operator": "equals", "expected": "approve"}],
            },
        },
        "metrics": {},
    },)


def candidate(output):
    return {
        "content": "Approve only eligible refund cases.",
        "replay_program": {"operation": "template", "output": output},
    }


def test_real_persisted_fixture_passes_default_offline_runner():
    report = deterministic_candidate_replay(candidate({
        "case_id": "{{input.case_id}}", "decision": "approve",
    }), replay_evidence())
    assert report["passed"] == report["total"] == 1
    assert report["details"][0]["checks"] == {
        "candidate_instructions": True,
        "persisted_replay_fixture": True,
        "input_schema": True,
        "runner_execution": True,
        "output_schema": True,
        "expected_assertions": True,
    }


def test_persisted_result_envelope_fixture_uses_same_replay_contract():
    episodic = replay_evidence()[0]
    evidence = ({
        "evidence_type": "result_envelope",
        "evidence_id": "result-1",
        "task_type": "refund-task",
        "trajectory": {
            "worker_id": "refund-worker",
            "task_id": "refund-task",
            "result": {"_replay_fixture": episodic["trajectory"]["replay_fixture"]},
        },
        "metrics": {},
    },)
    report = deterministic_candidate_replay(candidate({
        "case_id": "{{input.case_id}}", "decision": "approve",
    }), evidence)
    assert report["passed"] == report["total"] == 1


def test_incorrect_candidate_output_fails_expected_result_and_assertion():
    report = deterministic_candidate_replay(candidate({
        "case_id": "{{input.case_id}}", "decision": "deny",
    }), replay_evidence())
    assert report["passed"] == 0
    assert report["details"][0]["checks"]["expected_assertions"] is False


def test_output_schema_mismatch_fails_even_when_expected_output_matches():
    expected = {"case_id": "case-17", "decision": 1}
    report = deterministic_candidate_replay(candidate({
        "case_id": "{{input.case_id}}", "decision": 1,
    }), replay_evidence(expected=expected))
    assert report["passed"] == 0
    assert report["details"][0]["checks"]["output_schema"] is False


def test_nonempty_legacy_trajectory_does_not_implicitly_pass():
    evidence = ({
        "evidence_id": "legacy", "trajectory": {"steps": ["done"]}, "metrics": {},
    },)
    report = deterministic_candidate_replay(
        {"content": "Any instructions", "replay_program": {
            "operation": "constant", "output": {"answer": "anything"},
        }},
        evidence,
    )
    assert report["passed"] == 0
    assert report["details"][0]["checks"]["persisted_replay_fixture"] is False
    assert report["details"][0]["checks"]["input_schema"] is False


def test_http_evaluate_executes_server_replay_and_ignores_client_score(auth_client, monkeypatch):
    agent = auth_client.post("/api/agents", json={"name": "Replay Organizer"}).json()

    async def planner(*_args, **_kwargs):
        return ""  # Use the deterministic one-worker planning fallback.

    monkeypatch.setattr("app.multi_agent_api.complete", planner)
    planned = auth_client.post("/api/orchestrations/plan", json={
        "agent_id": agent["id"], "goal": "Evaluate a refund procedure",
    })
    assert planned.status_code == 201, planned.text
    run_id = planned.json()["run_id"]

    with SessionLocal() as db:
        run = db.scalar(select(OrchestrationRunRecord).where(
            OrchestrationRunRecord.run_id == run_id
        ))
        evidence, _ = record_episodic_evidence(
            db,
            scope=MemoryScope(run.workspace_id, run.user_id, run.agent_id, run.run_id),
            task_type="refund", outcome="succeeded",
            trajectory=replay_evidence()[0]["trajectory"], metrics={},
            idempotency_key="http-replay-evidence", redaction_status="redacted",
        )
        evidence_id = evidence.evidence_id
        db.commit()

    proposed = auth_client.post(f"/api/orchestrations/{run_id}/candidates", json={
        "candidate_type": "skill", "name": "Wrong refund candidate",
        "content": {"content": "raw"}, "evidence_ids": [evidence_id],
        "idempotency_key": "http-wrong-candidate",
    })
    assert proposed.status_code == 201, proposed.text
    candidate_id = proposed.json()["candidate_id"]
    redacted = auth_client.post(
        f"/api/orchestrations/{run_id}/candidates/{candidate_id}/redact",
        json={"content": candidate({
            "case_id": "{{input.case_id}}", "decision": "deny",
        })},
    )
    assert redacted.status_code == 200, redacted.text

    rejected = auth_client.post(
        f"/api/orchestrations/{run_id}/candidates/{candidate_id}/evaluate",
        json={"replay_report": {"replay_pass_rate": 1.0}},
    )
    assert rejected.status_code == 422
    with SessionLocal() as db:
        persisted = db.get(ProceduralCandidate, candidate_id)
        assert persisted.status == "redacted" and persisted.evaluation == {}
