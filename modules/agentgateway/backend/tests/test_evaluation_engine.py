"""Tests for the evaluation engine + regression comparison.

The DAG runner is monkeypatched so cases don't hit a real LLM: each fake run
echoes a canned output keyed by the case's input. Scoring uses keyword/schema
dimensions (deterministic) — the LLM judge path is exercised separately by
leaving judge_prompt empty so it's skipped.
"""

from __future__ import annotations

import json

import pytest
from sqlmodel import Session, select

from app.core import evaluation_engine as ee
from app.core.dag_executor import DAGExecutionResult
from app.core.database import engine
from app.core.evaluation_compare import compare_runs
from app.models.db import (
    Agent, AgentTestCase, DAGGraph, EvaluationCaseResult,
    EvaluationRun, EvaluationSuite,
)


_GRAPH = json.dumps({
    "nodes": [
        {"id": "i1", "type": "i", "config": {}},
        {"id": "agent1", "type": "agent", "config": {
            "system_prompt": "x", "model_name": "test-model", "provider": "glm",
        }},
        {"id": "o1", "type": "o", "config": {}},
    ],
    "edges": [
        {"id": "e1", "source": "i1", "target": "agent1"},
        {"id": "e2", "source": "agent1", "target": "o1"},
    ],
})


def _seed_suite(outputs: dict, *, with_key=True) -> tuple[int, int]:
    """Create agent + DAG + suite with two cases. Returns (agent_id, suite_id).

    ``outputs`` maps case input → canned agent output for the fake runner.
    """
    with Session(engine) as session:
        agent = Agent(name="eval-test")
        session.add(agent)
        session.commit()
        session.refresh(agent)
        session.add(DAGGraph(agent_id=agent.id, graph_json=_GRAPH, version=3))
        suite = EvaluationSuite(agent_id=agent.id, name="suite-1")
        session.add(suite)
        session.commit()
        session.refresh(suite)
        session.add(AgentTestCase(
            agent_id=agent.id, suite_id=suite.id, name="case-key",
            input_message="ping", expected_keywords=json.dumps(["pong"]),
            is_key=with_key, sort_order=1,
        ))
        session.add(AgentTestCase(
            agent_id=agent.id, suite_id=suite.id, name="case-normal",
            input_message="hi", expected_keywords=json.dumps(["hello"]),
            is_key=False, sort_order=2,
        ))
        session.commit()
        return agent.id, suite.id


def _patch_runner(monkeypatch, outputs: dict, *, trace_id="trace-x"):
    """Patch DAGRunner so .run echoes outputs[input] without an LLM."""
    class _FakeRunner:
        def __init__(self, parser, state_manager=None):
            pass

        async def run(self, user_input, agent_id=0, on_event=None, dag_version=0):
            return DAGExecutionResult(
                final_output=outputs.get(user_input, ""),
                total_duration_ms=12,
                trace_id=trace_id,
                token_input=5,
                token_output=7,
            )

    monkeypatch.setattr(ee, "DAGRunner", _FakeRunner)


@pytest.mark.asyncio
async def test_run_suite_produces_n_case_results_plus_summary(monkeypatch):
    agent_id, suite_id = _seed_suite({})
    # key case passes (output has "pong"), normal case fails (no "hello")
    _patch_runner(monkeypatch, {"ping": "pong!", "hi": "yo"})

    with Session(engine) as session:
        run = await ee.run_suite(agent_id, suite_id, session)
        results = session.exec(
            select(EvaluationCaseResult).where(EvaluationCaseResult.run_id == run.id)
        ).all()

    assert len(results) == 2
    summary = json.loads(run.summary)
    assert summary["total_cases"] == 2
    assert summary["passed_cases"] == 1
    assert summary["pass_rate"] == 0.5
    # version identifiers captured on the run
    assert run.dag_version == 3
    assert run.model == "test-model"


@pytest.mark.asyncio
async def test_key_case_pass_rate_tracked_separately(monkeypatch):
    agent_id, suite_id = _seed_suite({})
    # Only the key case passes → key_pass_rate 1.0 even though overall is 0.5
    _patch_runner(monkeypatch, {"ping": "pong", "hi": "nope"})

    with Session(engine) as session:
        run = await ee.run_suite(agent_id, suite_id, session)

    summary = json.loads(run.summary)
    assert summary["key_total"] == 1
    assert summary["key_passed"] == 1
    assert summary["key_pass_rate"] == 1.0
    assert summary["pass_rate"] == 0.5
    # run passes because all key cases pass
    assert run.passed is True


@pytest.mark.asyncio
async def test_langfuse_unavailable_does_not_block(monkeypatch):
    agent_id, suite_id = _seed_suite({})
    # trace_id empty simulates Langfuse off — run must still complete.
    _patch_runner(monkeypatch, {"ping": "pong", "hi": "hello"}, trace_id="")

    with Session(engine) as session:
        run = await ee.run_suite(agent_id, suite_id, session)
        results = session.exec(
            select(EvaluationCaseResult).where(EvaluationCaseResult.run_id == run.id)
        ).all()

    assert json.loads(run.trace_ids) == []  # no trace ids retained
    assert all(r.trace_id == "" for r in results)
    assert json.loads(run.summary)["total_cases"] == 2


@pytest.mark.asyncio
async def test_compare_runs_reports_regression_and_versions(monkeypatch):
    agent_id, suite_id = _seed_suite({})

    # Baseline: both pass.
    _patch_runner(monkeypatch, {"ping": "pong", "hi": "hello"})
    with Session(engine) as session:
        baseline = await ee.run_suite(agent_id, suite_id, session)
        baseline_id = baseline.id

    # Candidate: key case regresses (no "pong" anymore).
    _patch_runner(monkeypatch, {"ping": "broken", "hi": "hello"})
    with Session(engine) as session:
        candidate = await ee.run_suite(agent_id, suite_id, session)
        candidate_id = candidate.id

    with Session(engine) as session:
        diff = compare_runs(baseline_id, candidate_id, session)

    regressions = diff["regressions"]
    assert len(regressions) == 1
    assert regressions[0]["case_name"] == "case-key"
    assert regressions[0]["is_key"] is True
    # version tags present on both sides
    assert diff["baseline"]["dag_version"] == 3
    assert diff["candidate"]["run_id"] == candidate_id
    # pass_rate dropped from 1.0 to 0.5 → delta -0.5
    assert diff["deltas"]["pass_rate"] == -0.5


def _seed_dimension_suite() -> tuple[int, int]:
    """Suite with one dimension-driven case (keyword + required rule)."""
    dims = json.dumps({"dimensions": [
        {"name": "keyword_coverage", "type": "keyword", "weight": 0.5,
         "threshold": 0.5, "keywords": ["pong"]},
        {"name": "format", "type": "rule", "weight": 0.5, "threshold": 0.5,
         "required": True, "rules": [{"type": "contains", "value": "ok"}]},
    ]})
    with Session(engine) as session:
        agent = Agent(name="eval-dim")
        session.add(agent)
        session.commit()
        session.refresh(agent)
        session.add(DAGGraph(agent_id=agent.id, graph_json=_GRAPH, version=1))
        suite = EvaluationSuite(agent_id=agent.id, name="dim-suite",
                                suite_type="general", pass_threshold=0.6)
        session.add(suite)
        session.commit()
        session.refresh(suite)
        session.add(AgentTestCase(
            agent_id=agent.id, suite_id=suite.id, name="dim-case",
            input_message="ping", dimensions_json=dims, is_key=True, sort_order=1,
        ))
        session.commit()
        return agent.id, suite.id


@pytest.mark.asyncio
async def test_dimension_driven_run_persists_breakdown(monkeypatch):
    agent_id, suite_id = _seed_dimension_suite()
    # output hits "pong" (keyword 1.0) and "ok" (rule 1.0) → both pass
    _patch_runner(monkeypatch, {"ping": "pong ok"})

    with Session(engine) as session:
        run = await ee.run_suite(agent_id, suite_id, session)
        rows = session.exec(
            select(EvaluationCaseResult).where(EvaluationCaseResult.run_id == run.id)
        ).all()

    assert len(rows) == 1
    row = rows[0]
    payload = json.loads(row.dimension_results_json)
    names = {d["dimension"] for d in payload["dimensions"]}
    assert names == {"keyword_coverage", "format"}
    assert payload["overall"] == 1.0
    assert row.passed is True
    # run summary carries the per-dimension aggregates
    summary = json.loads(run.summary)
    assert summary["dimension_averages"]["keyword_coverage"] == 1.0
    assert summary["dimension_fail_rates"]["format"] == 0.0


@pytest.mark.asyncio
async def test_dimension_required_failure_fails_run(monkeypatch):
    agent_id, suite_id = _seed_dimension_suite()
    # output hits "pong" but NOT "ok" → required rule fails → case + run fail
    _patch_runner(monkeypatch, {"ping": "pong only"})

    with Session(engine) as session:
        run = await ee.run_suite(agent_id, suite_id, session)
        rows = session.exec(
            select(EvaluationCaseResult).where(EvaluationCaseResult.run_id == run.id)
        ).all()

    assert rows[0].passed is False
    assert run.passed is False
    summary = json.loads(run.summary)
    assert len(summary["required_dimension_failures"]) == 1
    assert summary["required_dimension_failures"][0]["dimension"] == "format"
