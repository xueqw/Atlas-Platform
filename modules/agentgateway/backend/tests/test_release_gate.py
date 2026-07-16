"""Backend tests for the release gate (main line D).

Covers spec ``agent-release-gate``:
- Key case not all-pass → publish blocked with the right reason.
- All criteria met → publish allowed, status → published.
- No evaluation data → blocked (no default pass).
- Per-agent threshold override takes effect.

Legacy (non-DAG) agents are used so the publish path's structural checks pass
trivially and the quality gate is the deciding factor. The gate's data sources
(EvaluationRun / AgentRunSummary) are seeded directly.
"""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session

from app.api import agents as agents_api
from app.core.database import engine
from app.core.release_gate import evaluate_release_gate, DEFAULT_THRESHOLDS
from app.models.db import (
    Agent, AgentStatus, AgentRunSummary, EvaluationRun,
    ModelConfig, PromptConfig,
)


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(agents_api.router, prefix="/api")
    return TestClient(app)


def _seed_agent(*, thresholds: str = "{}") -> int:
    """Create a legacy (non-DAG) agent with valid prompt/model. Returns id."""
    with Session(engine) as session:
        pc = PromptConfig(system_prompt="x" * 100)
        mc = ModelConfig(provider="glm", model_name="test-model")
        session.add(pc)
        session.add(mc)
        session.commit()
        session.refresh(pc)
        session.refresh(mc)
        agent = Agent(
            name="gate-test",
            prompt_config_id=pc.id,
            model_config_id=mc.id,
            release_gate_thresholds=thresholds,
        )
        session.add(agent)
        session.commit()
        session.refresh(agent)
        return agent.id


def _add_eval_run(agent_id: int, summary: dict, *, passed: bool = True) -> None:
    with Session(engine) as session:
        session.add(EvaluationRun(
            agent_id=agent_id, suite_id=1,
            summary=json.dumps(summary), passed=passed,
        ))
        session.commit()


def _add_run_summaries(agent_id: int, *, count: int, errors: int = 0,
                       duration_ms: int = 100, tokens: int = 10) -> None:
    with Session(engine) as session:
        for i in range(count):
            session.add(AgentRunSummary(
                agent_id=agent_id,
                status="error" if i < errors else "completed",
                total_duration_ms=duration_ms,
                token_input=tokens, token_output=tokens,
            ))
        session.commit()


def _good_eval_summary() -> dict:
    return {
        "total_cases": 4, "passed_cases": 4, "pass_rate": 1.0,
        "key_total": 2, "key_passed": 2, "key_pass_rate": 1.0,
        "avg_score": 1.0, "latency_p95_ms": 120.0,
        "token_input": 10, "token_output": 10,
    }


# ─── Publish path ──────────────────────────────────────────────────────────────


def test_publish_blocked_when_key_case_not_all_pass(client):
    agent_id = _seed_agent()
    summary = _good_eval_summary()
    summary.update(key_passed=1, pass_rate=0.75, passed_cases=3)
    _add_eval_run(agent_id, summary, passed=False)
    _add_run_summaries(agent_id, count=10)

    res = client.post(f"/api/agents/{agent_id}/publish")
    assert res.status_code == 422
    reasons = res.json()["detail"]["errors"]
    assert any("关键 case" in r for r in reasons)

    # status must stay non-published
    with Session(engine) as session:
        assert session.get(Agent, agent_id).status == AgentStatus.DRAFT


def test_publish_allowed_when_all_criteria_met(client):
    agent_id = _seed_agent()
    _add_eval_run(agent_id, _good_eval_summary(), passed=True)
    _add_run_summaries(agent_id, count=10, errors=0)

    res = client.post(f"/api/agents/{agent_id}/publish")
    assert res.status_code == 200
    assert res.json()["status"] == "published"

    with Session(engine) as session:
        assert session.get(Agent, agent_id).status == AgentStatus.PUBLISHED


def test_publish_blocked_when_no_evaluation_data(client):
    agent_id = _seed_agent()
    # Observability exists, but no EvaluationRun at all.
    _add_run_summaries(agent_id, count=10)

    res = client.post(f"/api/agents/{agent_id}/publish")
    assert res.status_code == 422
    reasons = res.json()["detail"]["errors"]
    assert any("缺少评估" in r for r in reasons)

    with Session(engine) as session:
        assert session.get(Agent, agent_id).status == AgentStatus.DRAFT


def test_release_gate_endpoint_returns_summary_and_reasons(client):
    agent_id = _seed_agent()
    summary = _good_eval_summary()
    summary.update(pass_rate=0.5, passed_cases=2, key_passed=1)
    _add_eval_run(agent_id, summary, passed=False)
    _add_run_summaries(agent_id, count=10, errors=8)  # high error rate

    res = client.get(f"/api/agents/{agent_id}/release-gate")
    assert res.status_code == 200
    body = res.json()
    assert body["passed"] is False
    assert body["summary"]["evaluation"]["pass_rate"] == 0.5
    assert body["summary"]["observability"]["error_rate"] >= 0.05
    # both an eval reason and an observability reason surface
    assert any("通过率" in r for r in body["reasons"])
    assert any("error rate" in r for r in body["reasons"])


# ─── Gate unit behavior ────────────────────────────────────────────────────────


def test_missing_run_data_blocks_without_default_pass():
    agent_id = _seed_agent()
    _add_eval_run(agent_id, _good_eval_summary(), passed=True)
    # No AgentRunSummary rows → observability missing.

    with Session(engine) as session:
        gate = evaluate_release_gate(agent_id, session)
    assert gate.passed is False
    assert any("缺少运行数据" in r for r in gate.reasons)


def test_per_agent_threshold_override_takes_effect():
    # System default min_pass_rate is 0.9. A pass_rate of 0.8 passes by default
    # but an override raising the bar to 0.95 must block.
    override = json.dumps({"min_pass_rate": 0.95})
    agent_id = _seed_agent(thresholds=override)
    summary = _good_eval_summary()
    summary.update(pass_rate=0.8, passed_cases=3)  # key cases still all pass
    _add_eval_run(agent_id, summary, passed=True)
    _add_run_summaries(agent_id, count=10, errors=0)

    with Session(engine) as session:
        gate = evaluate_release_gate(agent_id, session)
    assert gate.passed is False
    assert gate.summary["thresholds"]["min_pass_rate"] == 0.95
    assert any("通过率" in r for r in gate.reasons)


def test_default_thresholds_used_without_override():
    agent_id = _seed_agent()  # release_gate_thresholds == "{}"
    summary = _good_eval_summary()
    summary.update(pass_rate=0.8, passed_cases=3)
    _add_eval_run(agent_id, summary, passed=True)
    _add_run_summaries(agent_id, count=10, errors=0)

    with Session(engine) as session:
        gate = evaluate_release_gate(agent_id, session)
    # 0.8 < default 0.9 → blocked, proving the default applies.
    assert gate.summary["thresholds"]["min_pass_rate"] == DEFAULT_THRESHOLDS["min_pass_rate"]
    assert gate.passed is False
