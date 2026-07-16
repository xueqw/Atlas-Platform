"""Backend tests for the Phase 3 quality release gate (change:
upgrade-release-gate-quality).

Covers spec ``agent-release-gate`` additions:
- dimension average below threshold blocks; required-dimension case failures block
- a run WITHOUT dimension data falls back to generic checks (not failed for absence)
- suite_type template selection + four-layer policy merge precedence
- structured ``failures`` (with actual/threshold/dimension) coexist with text ``reasons``

Uses the same direct-seeding style as test_release_gate.py. Suites are seeded so
the latest run's suite_id resolves to a real EvaluationSuite with a suite_type,
driving template selection.
"""

from __future__ import annotations

import json

import pytest
from sqlmodel import Session

from app.core.database import engine
from app.core.release_gate import (
    evaluate_release_gate,
    resolve_gate_policy,
    GATE_TEMPLATES,
    DEFAULT_THRESHOLDS,
)
from app.models.db import (
    Agent, AgentRunSummary, EvaluationRun, EvaluationSuite,
    ModelConfig, PromptConfig,
)


def _seed_agent(*, thresholds: str = "{}") -> int:
    with Session(engine) as session:
        pc = PromptConfig(system_prompt="x" * 100)
        mc = ModelConfig(provider="glm", model_name="test-model")
        session.add(pc); session.add(mc); session.commit()
        session.refresh(pc); session.refresh(mc)
        agent = Agent(
            name="gate-dim-test",
            prompt_config_id=pc.id, model_config_id=mc.id,
            release_gate_thresholds=thresholds,
        )
        session.add(agent); session.commit(); session.refresh(agent)
        return agent.id


def _seed_suite(agent_id: int, *, suite_type: str = "planner", policy: str = "{}") -> int:
    with Session(engine) as session:
        suite = EvaluationSuite(
            agent_id=agent_id, name=f"{suite_type}-suite",
            suite_type=suite_type, release_gate_policy_json=policy,
        )
        session.add(suite); session.commit(); session.refresh(suite)
        return suite.id


def _add_eval_run(agent_id: int, suite_id: int, summary: dict, *, passed: bool = True) -> None:
    with Session(engine) as session:
        session.add(EvaluationRun(
            agent_id=agent_id, suite_id=suite_id,
            summary=json.dumps(summary), passed=passed,
        ))
        session.commit()


def _add_run_summaries(agent_id: int, *, count: int = 10, errors: int = 0) -> None:
    with Session(engine) as session:
        for i in range(count):
            session.add(AgentRunSummary(
                agent_id=agent_id,
                status="error" if i < errors else "completed",
                total_duration_ms=100, token_input=10, token_output=10,
            ))
        session.commit()


def _good_summary(**over) -> dict:
    s = {
        "total_cases": 4, "passed_cases": 4, "pass_rate": 1.0,
        "key_total": 2, "key_passed": 2, "key_pass_rate": 1.0,
        "avg_score": 1.0, "latency_p95_ms": 120.0,
        "token_input": 10, "token_output": 10,
    }
    s.update(over)
    return s


# ─── Dimension gates ─────────────────────────────────────────────────────────


def test_dimension_avg_below_threshold_blocks():
    # planner template gates goal_completion >= 0.8; a run at 0.6 must block,
    # and the failure must carry the dimension, actual, threshold.
    agent_id = _seed_agent()
    suite_id = _seed_suite(agent_id, suite_type="planner")
    summary = _good_summary(dimension_averages={"goal_completion": 0.6})
    _add_eval_run(agent_id, suite_id, summary, passed=True)
    _add_run_summaries(agent_id)

    with Session(engine) as session:
        gate = evaluate_release_gate(agent_id, session)

    assert gate.passed is False
    assert any("goal_completion" in r and "0.60" in r for r in gate.reasons)
    dim_fail = [f for f in gate.failures if f.get("code") == "dimension_below"]
    assert dim_fail, "expected a structured dimension_below failure"
    assert dim_fail[0]["dimension"] == "goal_completion"
    assert dim_fail[0]["actual"] == 0.6
    assert dim_fail[0]["threshold"] == 0.8


def test_required_dimension_case_failure_blocks_and_lists_cases():
    agent_id = _seed_agent()
    suite_id = _seed_suite(agent_id, suite_type="planner")
    summary = _good_summary(
        dimension_averages={"goal_completion": 0.95},  # avg fine
        required_dimension_failures=[
            {"case_id": 7, "case_name": "核心目标 case", "dimension": "goal_completion",
             "score": 0.4, "threshold": 0.6},
        ],
    )
    _add_eval_run(agent_id, suite_id, summary, passed=True)
    _add_run_summaries(agent_id)

    with Session(engine) as session:
        gate = evaluate_release_gate(agent_id, session)

    assert gate.passed is False
    req_fail = [f for f in gate.failures if f.get("code") == "required_dimension_failed"]
    assert req_fail, "expected a required_dimension_failed failure"
    assert req_fail[0]["cases"][0]["case_name"] == "核心目标 case"
    assert any("必过维度" in r for r in gate.reasons)


def test_no_dimension_data_falls_back_to_generic_not_failed():
    # planner suite, but the run carries NO dimension_averages (old-style eval).
    # Generic checks all pass → gate passes; dimensions must be skipped, not failed.
    agent_id = _seed_agent()
    suite_id = _seed_suite(agent_id, suite_type="planner")
    _add_eval_run(agent_id, suite_id, _good_summary(), passed=True)  # no dimension_averages
    _add_run_summaries(agent_id, errors=0)

    with Session(engine) as session:
        gate = evaluate_release_gate(agent_id, session)

    assert gate.passed is True, f"should pass on generic checks; reasons={gate.reasons}"
    assert gate.summary.get("dimensions_checked") is False
    assert not any("维度" in r for r in gate.reasons)


def test_dimension_in_policy_but_absent_in_run_is_skipped():
    # Run has dimension data, but not the gated one (goal_completion). The gated
    # dimension must be skipped (recorded), never a failure.
    agent_id = _seed_agent()
    suite_id = _seed_suite(agent_id, suite_type="planner")
    summary = _good_summary(dimension_averages={"format_compliance": 0.9})  # no goal_completion
    _add_eval_run(agent_id, suite_id, summary, passed=True)
    _add_run_summaries(agent_id)

    with Session(engine) as session:
        gate = evaluate_release_gate(agent_id, session)

    assert gate.passed is True
    assert "goal_completion" in (gate.summary.get("dimensions_skipped") or [])
    assert not any(f.get("code") == "dimension_below" for f in gate.failures)


def test_worst_dimension_reported_in_summary():
    agent_id = _seed_agent()
    suite_id = _seed_suite(agent_id, suite_type="general")  # no dim gate, but data present
    summary = _good_summary(dimension_averages={"a": 0.9, "b": 0.5, "c": 0.7})
    _add_eval_run(agent_id, suite_id, summary, passed=True)
    _add_run_summaries(agent_id)

    with Session(engine) as session:
        gate = evaluate_release_gate(agent_id, session)

    assert gate.summary["worst_dimension"]["dimension"] == "b"
    assert gate.summary["worst_dimension"]["avg"] == 0.5
    assert gate.passed is True  # general has no dimension hard gate


# ─── Template selection + four-layer merge ─────────────────────────────────────


def test_suite_type_template_selected_for_planner():
    agent_id = _seed_agent()
    suite_id = _seed_suite(agent_id, suite_type="planner")
    _add_eval_run(agent_id, suite_id, _good_summary(), passed=True)
    _add_run_summaries(agent_id)

    with Session(engine) as session:
        gate = evaluate_release_gate(agent_id, session)

    assert gate.summary["suite_type"] == "planner"
    assert any(d["name"] == "goal_completion" for d in gate.summary["gate_dimensions"])


def test_general_suite_equivalent_to_generic_only():
    agent_id = _seed_agent()
    suite_id = _seed_suite(agent_id, suite_type="general")
    _add_eval_run(agent_id, suite_id, _good_summary(), passed=True)
    _add_run_summaries(agent_id)

    with Session(engine) as session:
        gate = evaluate_release_gate(agent_id, session)

    assert gate.summary["gate_dimensions"] == []
    assert gate.passed is True


def test_four_layer_merge_per_agent_overrides_template():
    # suite policy lowers goal_completion to 0.7; per-agent raises it to 0.95.
    # Effective must be 0.95 (per-agent wins).
    suite_policy = json.dumps({"dimensions": [{"name": "goal_completion", "min_avg": 0.7}]})
    agent_policy = json.dumps({"dimensions": [{"name": "goal_completion", "min_avg": 0.95}]})
    agent_id = _seed_agent(thresholds=agent_policy)
    suite_id = _seed_suite(agent_id, suite_type="planner", policy=suite_policy)

    with Session(engine) as session:
        agent = session.get(Agent, agent_id)
        suite = session.get(EvaluationSuite, suite_id)
        policy = resolve_gate_policy(agent, suite)

    gc = [d for d in policy["dimensions"] if d["name"] == "goal_completion"][0]
    assert gc["min_avg"] == 0.95, f"per-agent override should win, got {gc}"


def test_per_agent_generic_override_still_works_flat_form():
    # Back-compat: a flat {min_pass_rate: ...} override (not wrapped in "generic").
    agent_id = _seed_agent(thresholds=json.dumps({"min_pass_rate": 0.95}))
    with Session(engine) as session:
        agent = session.get(Agent, agent_id)
        policy = resolve_gate_policy(agent, None)
    assert policy["generic"]["min_pass_rate"] == 0.95
    assert policy["suite_type"] == "general"


# ─── Structured failures coexist with reasons ──────────────────────────────────


def test_failures_and_reasons_coexist():
    agent_id = _seed_agent()
    suite_id = _seed_suite(agent_id, suite_type="planner")
    summary = _good_summary(
        pass_rate=0.5, passed_cases=2,
        dimension_averages={"goal_completion": 0.6},
    )
    _add_eval_run(agent_id, suite_id, summary, passed=False)
    _add_run_summaries(agent_id, errors=8)  # high error rate too

    with Session(engine) as session:
        gate = evaluate_release_gate(agent_id, session)

    assert gate.passed is False
    # reasons is still a list[str]
    assert isinstance(gate.reasons, list) and all(isinstance(r, str) for r in gate.reasons)
    # failures is structured with code/label, and a dimension failure carries actual/threshold
    codes = {f["code"] for f in gate.failures}
    assert "pass_rate_below" in codes
    assert "dimension_below" in codes
    assert "error_rate_high" in codes
    for f in gate.failures:
        assert "code" in f and "label" in f
