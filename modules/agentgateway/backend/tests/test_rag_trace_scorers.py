"""Tests for trace-aware + RAG-aware scorers (change: add-rag-trace-aware-scorers).

Covers spec dimension-driven-evaluation additions:
- trace_scorer: no_error_steps / step_count_range / required_nodes / empty-skip
- ragas RAG metrics: missing contexts → skipped; context_recall missing
  ground_truth → skipped (verified via the skip path, no real ragas needed)
- engine threading: steps reach a trace dimension via score_case; absent steps
  leave ScoreContext.steps empty and legacy dims unaffected
- templates: rag has faithfulness, workflow has trace dims, unknown → general
"""

from __future__ import annotations

import json

import pytest

from app.core import evaluation_engine as ee
from app.core.scorers import ScoreContext
from app.core.scorers.ragas_scorer import ragas_scorer
from app.core.scorers.trace_scorer import trace_scorer
from app.core.scorers.templates import get_template
from app.models.db import AgentTestCase


def _ctx(**kw) -> ScoreContext:
    return ScoreContext(**kw)


def _steps(*specs):
    """specs: tuples (node_id, node_type, status[, error])."""
    out = []
    for sp in specs:
        node_id, node_type, status = sp[0], sp[1], sp[2]
        err = sp[3] if len(sp) > 3 else None
        out.append({"node_id": node_id, "node_type": node_type, "status": status,
                    "duration_ms": 10, "tokens": 5, "error": err})
    return out


# ─── trace_scorer: no_error_steps ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_trace_no_error_steps_all_pass():
    dim = {"name": "no_err", "type": "trace", "check": "no_error_steps", "threshold": 0.6}
    steps = _steps(("p1", "prompt", "completed"), ("m1", "model", "completed"))
    res = await trace_scorer(dim, _ctx(steps=steps))
    assert res.score == 1.0 and res.passed is True
    assert res.evidence["error_steps"] == 0


@pytest.mark.asyncio
async def test_trace_no_error_steps_with_error_docks_score():
    dim = {"name": "no_err", "type": "trace", "check": "no_error_steps", "threshold": 0.6}
    steps = _steps(("p1", "prompt", "completed"),
                   ("m1", "model", "error", "boom"),
                   ("k1", "kb", "completed"),
                   ("t1", "tool", "completed"))
    res = await trace_scorer(dim, _ctx(steps=steps))
    assert res.score == pytest.approx(0.75)  # 1 of 4 errored
    assert res.evidence["error_node_ids"] == ["m1"]


# ─── trace_scorer: step_count_range ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_trace_step_count_in_range():
    dim = {"name": "cnt", "type": "trace", "check": "step_count_range",
           "min_steps": 2, "max_steps": 5, "threshold": 0.6}
    steps = _steps(("a", "x", "completed"), ("b", "y", "completed"), ("c", "z", "completed"))
    res = await trace_scorer(dim, _ctx(steps=steps))
    assert res.score == 1.0 and res.passed is True


@pytest.mark.asyncio
async def test_trace_step_count_over_max_docks():
    dim = {"name": "cnt", "type": "trace", "check": "step_count_range",
           "min_steps": 1, "max_steps": 2, "threshold": 0.6}
    steps = _steps(("a", "x", "completed"), ("b", "y", "completed"),
                   ("c", "z", "completed"), ("d", "w", "completed"))  # 4 > max 2
    res = await trace_scorer(dim, _ctx(steps=steps))
    assert res.score < 1.0
    assert res.evidence["total_steps"] == 4 and res.evidence["max_steps"] == 2


@pytest.mark.asyncio
async def test_trace_step_count_under_min_docks():
    dim = {"name": "cnt", "type": "trace", "check": "step_count_range",
           "min_steps": 4, "threshold": 0.6}
    steps = _steps(("a", "x", "completed"))  # 1 < min 4
    res = await trace_scorer(dim, _ctx(steps=steps))
    assert res.score < 1.0


# ─── trace_scorer: required_nodes ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_trace_required_nodes_all_present():
    dim = {"name": "req", "type": "trace", "check": "required_nodes",
           "required_nodes": ["kb", "model"], "threshold": 0.6}
    steps = _steps(("k1", "kb", "completed"), ("m1", "model", "completed"))
    res = await trace_scorer(dim, _ctx(steps=steps))
    assert res.score == 1.0 and res.passed is True
    assert res.evidence["missing"] == []


@pytest.mark.asyncio
async def test_trace_required_nodes_missing_docks():
    dim = {"name": "req", "type": "trace", "check": "required_nodes",
           "required_nodes": ["kb", "tool"], "threshold": 0.6}
    steps = _steps(("k1", "kb", "completed"), ("m1", "model", "completed"))  # no tool
    res = await trace_scorer(dim, _ctx(steps=steps))
    assert "tool" in res.evidence["missing"]
    assert res.score == pytest.approx(0.5)  # 1 of 2 missing


@pytest.mark.asyncio
async def test_trace_required_nodes_only_completed_count():
    dim = {"name": "req", "type": "trace", "check": "required_nodes",
           "required_nodes": ["tool"], "threshold": 0.6}
    steps = _steps(("t1", "tool", "error", "failed"))  # present but not completed
    res = await trace_scorer(dim, _ctx(steps=steps))
    assert "tool" in res.evidence["missing"]


# ─── trace_scorer: empty / unknown ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_trace_empty_steps_skipped():
    dim = {"name": "no_err", "type": "trace", "check": "no_error_steps"}
    res = await trace_scorer(dim, _ctx(steps=[]))
    assert res.skipped is True
    assert "无执行轨迹" in res.reason


@pytest.mark.asyncio
async def test_trace_unknown_check_skipped():
    dim = {"name": "x", "type": "trace", "check": "nonexistent_check"}
    steps = _steps(("a", "x", "completed"))
    res = await trace_scorer(dim, _ctx(steps=steps))
    assert res.skipped is True


# ─── RAG ragas metrics: input-validation skip paths ──────────────────────────


@pytest.mark.asyncio
async def test_ragas_faithfulness_missing_contexts_skipped():
    dim = {"name": "faith", "type": "faithfulness", "metric": "faithfulness"}
    res = await ragas_scorer(dim, _ctx(output="answer", contexts=[]))
    assert res.skipped is True
    assert "非空检索上下文" in res.reason or "contexts" in res.reason


@pytest.mark.asyncio
async def test_ragas_context_precision_missing_contexts_skipped():
    dim = {"name": "cp", "type": "context_precision", "metric": "context_precision"}
    res = await ragas_scorer(dim, _ctx(user_input="q", output="a", contexts=[]))
    assert res.skipped is True


@pytest.mark.asyncio
async def test_ragas_context_recall_missing_ground_truth_skipped():
    # contexts present, but no reference_output → recall needs ground truth.
    dim = {"name": "cr", "type": "context_recall", "metric": "context_recall"}
    res = await ragas_scorer(dim, _ctx(user_input="q", output="a",
                                       contexts=["doc1"], reference_output=""))
    assert res.skipped is True
    assert "ground truth" in res.reason or "reference_output" in res.reason


@pytest.mark.asyncio
async def test_ragas_rag_metric_with_contexts_degrades_when_ragas_absent():
    # With contexts + ground truth present, validation passes; ragas itself is
    # not installed in test env, so it degrades to skipped (not a crash).
    dim = {"name": "faith", "type": "faithfulness", "metric": "faithfulness"}
    res = await ragas_scorer(dim, _ctx(output="a", contexts=["doc1"]))
    assert res.skipped is True  # ragas not installed → skip, never gate
    assert res.evidence.get("metric") == "faithfulness"


# ─── engine threading ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_score_case_threads_steps_to_trace_dimension():
    case = AgentTestCase(
        suite_id=1, name="t", input_message="hi",
        dimensions_json=json.dumps({"dimensions": [
            {"name": "no_err", "type": "trace", "check": "no_error_steps",
             "weight": 1.0, "threshold": 0.6, "required": True},
        ]}),
    )
    steps = _steps(("p1", "prompt", "completed"), ("m1", "model", "completed"))
    cs = await ee.score_case(case, "out", steps=steps)
    trace_dim = [d for d in cs.dimension_results if d.type == "trace"][0]
    assert trace_dim.skipped is False
    assert trace_dim.score == 1.0
    assert cs.passed is True


@pytest.mark.asyncio
async def test_score_case_without_steps_skips_trace_dimension():
    case = AgentTestCase(
        suite_id=1, name="t", input_message="hi",
        dimensions_json=json.dumps({"dimensions": [
            {"name": "no_err", "type": "trace", "check": "no_error_steps",
             "weight": 1.0, "threshold": 0.6, "required": False},
        ]}),
    )
    cs = await ee.score_case(case, "out")  # no steps
    trace_dim = [d for d in cs.dimension_results if d.type == "trace"][0]
    assert trace_dim.skipped is True


def test_build_context_steps_default_empty():
    case = AgentTestCase(suite_id=1, name="t", input_message="hi")
    ctx = ee._build_context(case, "out")
    assert ctx.steps == [] and ctx.trace_id == ""


# ─── templates ───────────────────────────────────────────────────────────────


def test_template_rag_has_faithfulness():
    rag = get_template("rag")
    names = [d["name"] for d in rag["dimensions"]]
    types = [d["type"] for d in rag["dimensions"]]
    assert "faithfulness" in names
    assert "faithfulness" in types
    # faithfulness is the required RAG dimension
    faith = [d for d in rag["dimensions"] if d["name"] == "faithfulness"][0]
    assert faith["required"] is True


def test_template_workflow_has_trace_dims():
    wf = get_template("workflow")
    types = [d["type"] for d in wf["dimensions"]]
    assert "trace" in types
    checks = [d.get("check") for d in wf["dimensions"] if d["type"] == "trace"]
    assert "no_error_steps" in checks


def test_template_unknown_falls_back_to_general():
    assert get_template("nope_xyz") == get_template("general")
