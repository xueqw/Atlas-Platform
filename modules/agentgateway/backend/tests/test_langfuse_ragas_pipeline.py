"""Tests for the strict Langfuse + Ragas release pipeline."""

from __future__ import annotations

import json

from sqlmodel import Session

from app.core.database import engine
from app.core.evaluation_engine import _aggregate, build_summary
from app.core.release_gate import evaluate_release_gate
from app.core.scorers.base import skipped_result
from app.models.db import (
    Agent,
    AgentRunSummary,
    EvaluationCaseResult,
    EvaluationRun,
    EvaluationSuite,
    ModelConfig,
    PromptConfig,
)


def _agent_and_suite(suite_type: str = "rag") -> tuple[int, int]:
    with Session(engine) as session:
        prompt = PromptConfig(system_prompt="pipeline test")
        model = ModelConfig(provider="glm", model_name="test-model")
        session.add(prompt)
        session.add(model)
        session.commit()
        session.refresh(prompt)
        session.refresh(model)
        agent = Agent(name="pipeline-test", prompt_config_id=prompt.id, model_config_id=model.id)
        session.add(agent)
        session.commit()
        session.refresh(agent)
        suite = EvaluationSuite(agent_id=agent.id, name="rag-suite", suite_type=suite_type)
        session.add(suite)
        session.commit()
        session.refresh(suite)
        return agent.id, suite.id


def _healthy_runtime(agent_id: int) -> None:
    with Session(engine) as session:
        for _ in range(3):
            session.add(AgentRunSummary(agent_id=agent_id, status="completed", total_duration_ms=100))
        session.commit()


def _summary(**overrides: object) -> dict:
    value = {
        "total_cases": 2,
        "passed_cases": 2,
        "pass_rate": 1.0,
        "key_total": 1,
        "key_passed": 1,
        "key_pass_rate": 1.0,
        "avg_score": 0.9,
        "latency_p95_ms": 100.0,
        "token_input": 10,
        "token_output": 10,
    }
    value.update(overrides)
    return value


def test_required_ragas_metric_can_be_strict_when_skipped():
    skipped = skipped_result(
        {"name": "faithfulness", "type": "faithfulness", "required": True, "gate_on_skip": True},
        "ragas unavailable",
    )
    overall, passed = _aggregate([skipped], 0.6)
    assert overall == 1.0
    assert passed is False
    assert skipped.evidence["gate_on_skip"] is True


def test_summary_records_metric_coverage_and_dataset_pipeline_fields():
    row = EvaluationCaseResult(
        run_id=1,
        case_id=1,
        case_name="rag case",
        passed=False,
        duration_ms=10,
        scores=json.dumps({"overall": 0.4}),
        dimension_results_json=json.dumps({"dimensions": [
            {"dimension": "faithfulness", "score": 0.0, "passed": False, "skipped": True},
            {"dimension": "answer_relevancy", "score": 0.8, "passed": True, "skipped": False},
        ]}),
    )
    summary = build_summary([row], 3, 4)
    assert summary["dimension_coverage"]["faithfulness"]["rate"] == 0.0
    assert summary["dimension_coverage"]["answer_relevancy"]["rate"] == 1.0


def test_rag_gate_blocks_when_required_metric_was_not_evaluated():
    agent_id, suite_id = _agent_and_suite()
    _healthy_runtime(agent_id)
    with Session(engine) as session:
        session.add(EvaluationRun(
            agent_id=agent_id,
            suite_id=suite_id,
            passed=False,
            summary=json.dumps(_summary(
                dimension_averages={"answer_relevancy": 0.9},
                dimension_coverage={
                    "faithfulness": {"declared": 2, "scored": 0, "skipped": 2, "rate": 0.0},
                    "answer_relevancy": {"declared": 2, "scored": 2, "skipped": 0, "rate": 1.0},
                },
            )),
        ))
        session.commit()
        gate = evaluate_release_gate(agent_id, session)
    assert gate.passed is False
    assert any(f["code"] == "dimension_coverage_below" and f["dimension"] == "faithfulness" for f in gate.failures)


def test_regression_gate_blocks_candidate_that_drops_below_baseline():
    agent_id, suite_id = _agent_and_suite("general")
    _healthy_runtime(agent_id)
    with Session(engine) as session:
        baseline = EvaluationRun(agent_id=agent_id, suite_id=suite_id, passed=True, summary=json.dumps(_summary()))
        session.add(baseline)
        session.commit()
        session.refresh(baseline)

        suite = session.get(EvaluationSuite, suite_id)
        suite.release_gate_policy_json = json.dumps({"regression": {
            "enabled": True,
            "baseline_run_id": baseline.id,
            "max_avg_score_drop": 0.03,
            "max_pass_rate_drop": 0.02,
            "max_new_case_regressions": 0,
        }})
        session.add(suite)
        session.commit()

        candidate = EvaluationRun(
            agent_id=agent_id,
            suite_id=suite_id,
            passed=False,
            summary=json.dumps(_summary(avg_score=0.7, pass_rate=0.5, passed_cases=1)),
        )
        session.add(candidate)
        session.commit()

        gate = evaluate_release_gate(agent_id, session)
    assert gate.passed is False
    assert gate.summary["regression_checked"] is True
    assert any(f["code"] == "regression_drop_exceeded" for f in gate.failures)
