"""Tests for dimension-driven evaluation: scorers, weighted aggregation,
legacy backward-compat, and graceful degradation (ragas unavailable / illegal
dimension configs).

These exercise the scorer registry + engine without hitting a real LLM: the
judge path is monkeypatched, and ragas is verified through its lazy-import
degradation rather than installing the heavy stack.
"""

from __future__ import annotations

import json

import pytest

from app.core import evaluation_engine as ee
from app.core import scorers
from app.core.scorers import ScoreContext
from app.core.scorers.deterministic import keyword_scorer, rule_scorer, schema_scorer
from app.core.scorers.judge import judge_scorer
from app.core.scorers.ragas_scorer import ragas_scorer
from app.models.db import AgentTestCase


def _ctx(output="", **kw) -> ScoreContext:
    return ScoreContext(output=output, **kw)


# ─── keyword scorer ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_keyword_scorer_hit_rate_and_evidence():
    dim = {"name": "kw", "type": "keyword", "weight": 1.0, "threshold": 0.6,
           "keywords": ["alpha", "beta", "gamma"]}
    res = await keyword_scorer(dim, _ctx("alpha and BETA here"))
    assert res.score == pytest.approx(2 / 3)
    assert res.passed is True  # 0.666 >= 0.6
    assert set(res.evidence["hit"]) == {"alpha", "beta"}
    assert res.evidence["missed"] == ["gamma"]


@pytest.mark.asyncio
async def test_keyword_scorer_no_keywords_passes():
    dim = {"name": "kw", "type": "keyword", "keywords": []}
    res = await keyword_scorer(dim, _ctx("anything"))
    assert res.score == 1.0 and res.passed is True


@pytest.mark.asyncio
async def test_keyword_scorer_below_threshold_fails():
    dim = {"name": "kw", "type": "keyword", "threshold": 0.6,
           "keywords": ["a", "b", "c", "d"]}
    res = await keyword_scorer(dim, _ctx("only a"))
    assert res.score == 0.25 and res.passed is False


# ─── schema scorer ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_schema_scorer_valid_and_invalid():
    schema = {"type": "object", "required": ["x"], "properties": {"x": {"type": "number"}}}
    dim = {"name": "sc", "type": "schema", "schema": schema}
    ok = await schema_scorer(dim, _ctx(json.dumps({"x": 1})))
    assert ok.score == 1.0 and ok.passed is True
    bad = await schema_scorer(dim, _ctx(json.dumps({"x": "no"})))
    assert bad.score == 0.0 and bad.passed is False
    assert bad.evidence["valid"] is False


@pytest.mark.asyncio
async def test_schema_scorer_no_schema_passes():
    res = await schema_scorer({"name": "sc", "type": "schema", "schema": {}}, _ctx("{}"))
    assert res.score == 1.0 and res.passed is True


# ─── rule scorer ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rule_scorer_mixed_rules():
    dim = {"name": "r", "type": "rule", "threshold": 0.6, "rules": [
        {"type": "contains", "value": "hello"},
        {"type": "not_contains", "value": "forbidden"},
        {"type": "min_length", "value": 3},
        {"type": "regex", "value": r"\d+"},
    ]}
    # "hello 42" → contains hello (ok), not_contains forbidden (ok),
    # min_length 3 (ok), regex digits (ok) → 4/4
    res = await rule_scorer(dim, _ctx("hello 42"))
    assert res.score == 1.0 and res.passed is True
    assert len(res.evidence["rules"]) == 4


@pytest.mark.asyncio
async def test_rule_scorer_partial_and_unknown_rule():
    dim = {"name": "r", "type": "rule", "threshold": 0.9, "rules": [
        {"type": "contains", "value": "yes"},
        {"type": "contains", "value": "absent"},
        {"type": "bogus", "value": "x"},
    ]}
    res = await rule_scorer(dim, _ctx("yes indeed"))
    assert res.score == pytest.approx(1 / 3)
    assert res.passed is False


@pytest.mark.asyncio
async def test_rule_scorer_no_rules_passes():
    res = await rule_scorer({"name": "r", "type": "rule", "rules": []}, _ctx("x"))
    assert res.score == 1.0 and res.passed is True


# ─── judge scorer ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_judge_scorer_uses_llm_score(monkeypatch):
    async def fake_judge(prompt, user_input, output):
        return 0.8, "looks good"

    monkeypatch.setattr("app.core.scorers.judge._llm_judge", fake_judge)
    dim = {"name": "goal", "type": "judge", "threshold": 0.6,
           "judge_prompt": "is it good?"}
    res = await judge_scorer(dim, _ctx("some output"))
    assert res.score == 0.8 and res.passed is True
    assert res.reason == "looks good"
    assert res.skipped is False


@pytest.mark.asyncio
async def test_judge_scorer_skips_when_unavailable(monkeypatch):
    async def fake_judge(prompt, user_input, output):
        return None

    monkeypatch.setattr("app.core.scorers.judge._llm_judge", fake_judge)
    dim = {"name": "goal", "type": "judge", "judge_prompt": "criteria"}
    res = await judge_scorer(dim, _ctx("out"))
    assert res.skipped is True and res.passed is False
    assert "跳过" in res.reason


@pytest.mark.asyncio
async def test_judge_scorer_skips_when_no_prompt():
    res = await judge_scorer({"name": "goal", "type": "judge"}, _ctx("out"))
    assert res.skipped is True


# ─── ragas scorer (degradation) ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ragas_scorer_skips_when_unavailable():
    # ragas is not installed in the test env → must degrade, not crash.
    dim = {"name": "rel", "type": "answer_relevancy", "weight": 0.3}
    res = await ragas_scorer(dim, _ctx("answer", user_input="q"))
    assert res.skipped is True and res.passed is False
    assert "ragas" in res.reason.lower() or "Ragas" in res.reason
    assert res.evidence.get("metric") == "answer_relevancy"


@pytest.mark.asyncio
async def test_ragas_correctness_skips_without_reference():
    dim = {"name": "corr", "type": "answer_correctness"}
    res = await ragas_scorer(dim, _ctx("answer", user_input="q", reference_output=""))
    assert res.skipped is True
    assert "reference_output" in res.reason


@pytest.mark.asyncio
async def test_ragas_scorer_scores_when_available(monkeypatch):
    # Simulate ragas being available by patching the compute helper.
    monkeypatch.setattr(
        "app.core.scorers.ragas_scorer._compute_ragas_score",
        lambda metric_name, ctx: 0.75,
    )
    dim = {"name": "rel", "type": "answer_relevancy", "threshold": 0.6, "weight": 0.5}
    res = await ragas_scorer(dim, _ctx("answer", user_input="q"))
    assert res.skipped is False
    assert res.score == 0.75 and res.passed is True


# ─── weighted aggregation via score_case ─────────────────────────────────────


def _case(**kw) -> AgentTestCase:
    """Build an in-memory (unsaved) case; defaults match db.py field defaults."""
    base = dict(
        agent_id=1, name="c", input_message="hi",
        expected_keywords="[]", expected_schema="{}", judge_prompt="",
        dimensions_json="", reference_output="",
        context_json="{}", constraints_json="{}", metadata_json="{}",
    )
    base.update(kw)
    return AgentTestCase(**base)


@pytest.mark.asyncio
async def test_score_case_weighted_overall():
    # two deterministic dims: keyword 1.0 (weight 0.25), rule 0.0 (weight 0.75)
    dims = {"dimensions": [
        {"name": "kw", "type": "keyword", "weight": 0.25, "threshold": 0.5,
         "keywords": ["ok"]},
        {"name": "r", "type": "rule", "weight": 0.75, "threshold": 0.5,
         "rules": [{"type": "contains", "value": "absent"}]},
    ]}
    case = _case(dimensions_json=json.dumps(dims))
    res = await ee.score_case(case, "ok here", case_threshold=0.6)
    # overall = 1.0*0.25 + 0.0*0.75 = 0.25
    assert res.overall == pytest.approx(0.25)
    assert res.passed is False  # below 0.6


@pytest.mark.asyncio
async def test_score_case_weights_normalized():
    # weights 2 and 6 → normalized 0.25 / 0.75; same as above unnormalized
    dims = {"dimensions": [
        {"name": "kw", "type": "keyword", "weight": 2, "threshold": 0.5,
         "keywords": ["ok"]},
        {"name": "r", "type": "rule", "weight": 6, "threshold": 0.5,
         "rules": [{"type": "contains", "value": "absent"}]},
    ]}
    case = _case(dimensions_json=json.dumps(dims))
    res = await ee.score_case(case, "ok here", case_threshold=0.6)
    assert res.overall == pytest.approx(0.25)


@pytest.mark.asyncio
async def test_score_case_required_failure_fails_case():
    # overall is high but a required dim fails its threshold → case fails
    dims = {"dimensions": [
        {"name": "kw", "type": "keyword", "weight": 0.9, "threshold": 0.5,
         "keywords": ["ok"]},
        {"name": "r", "type": "rule", "weight": 0.1, "threshold": 0.5,
         "required": True, "rules": [{"type": "contains", "value": "absent"}]},
    ]}
    case = _case(dimensions_json=json.dumps(dims))
    res = await ee.score_case(case, "ok here", case_threshold=0.6)
    # overall = 1.0*0.9 + 0.0*0.1 = 0.9 >= 0.6, but required rule failed
    assert res.overall == pytest.approx(0.9)
    assert res.passed is False


@pytest.mark.asyncio
async def test_score_case_unknown_type_isolated(monkeypatch):
    dims = {"dimensions": [
        {"name": "kw", "type": "keyword", "weight": 1.0, "threshold": 0.5,
         "keywords": ["ok"]},
        {"name": "weird", "type": "does_not_exist", "weight": 1.0},
    ]}
    case = _case(dimensions_json=json.dumps(dims))
    res = await ee.score_case(case, "ok", case_threshold=0.6)
    # unknown dim is skipped (excluded from overall), keyword scores 1.0
    assert res.overall == pytest.approx(1.0)
    assert res.passed is True
    skipped = [d for d in res.dimension_results if d.skipped]
    assert len(skipped) == 1 and skipped[0].dimension == "weird"


@pytest.mark.asyncio
async def test_score_case_scorer_exception_isolated(monkeypatch):
    # a scorer that raises must be isolated as skipped, not crash the case
    async def boom(dim_cfg, ctx):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(scorers, "get_scorer", lambda t: boom if t == "judge" else scorers.base.get_scorer(t))
    dims = {"dimensions": [
        {"name": "kw", "type": "keyword", "weight": 1.0, "threshold": 0.5, "keywords": ["ok"]},
        {"name": "j", "type": "judge", "weight": 1.0, "judge_prompt": "x"},
    ]}
    case = _case(dimensions_json=json.dumps(dims))
    # Patch the engine's get_scorer reference too.
    monkeypatch.setattr(ee, "get_scorer", lambda t: boom if t == "judge" else scorers.base.get_scorer(t))
    res = await ee.score_case(case, "ok", case_threshold=0.6)
    assert res.passed is True  # keyword still scored
    assert any(d.skipped and "异常" in d.reason for d in res.dimension_results)


@pytest.mark.asyncio
async def test_ragas_dim_skipped_does_not_fail_case():
    # answer_relevancy ragas dim unavailable → skipped, keyword carries the case
    dims = {"dimensions": [
        {"name": "kw", "type": "keyword", "weight": 0.5, "threshold": 0.5, "keywords": ["ok"]},
        {"name": "rel", "type": "answer_relevancy", "weight": 0.5, "required": True},
    ]}
    case = _case(dimensions_json=json.dumps(dims))
    res = await ee.score_case(case, "ok", case_threshold=0.6)
    # ragas skipped (even though required) → excluded; keyword 1.0 → overall 1.0
    assert res.overall == pytest.approx(1.0)
    assert res.passed is True
    assert any(d.skipped for d in res.dimension_results)


# ─── legacy backward-compat ──────────────────────────────────────────────────


def _legacy_reference(case: AgentTestCase, output: str,
                      judge_value):
    """Re-implementation of the PRE-refactor score_case for parity checking.

    Mirrors the old fixed three-dimension logic exactly: keyword hit-rate,
    schema 1/0, judge value (or skipped when None), overall = mean, pass at 0.6.
    """
    import jsonschema as _js
    scores = {}
    lower = (output or "").lower()
    kws = json.loads(case.expected_keywords or "[]")
    if kws:
        hits = sum(1 for kw in kws if str(kw).lower() in lower)
        scores["keyword"] = hits / len(kws)
    schema = json.loads(case.expected_schema or "{}")
    if schema:
        try:
            data = json.loads(output) if isinstance(output, str) else output
            _js.validate(instance=data, schema=schema)
            scores["schema"] = 1.0
        except Exception:
            scores["schema"] = 0.0
    if (case.judge_prompt or "").strip() and judge_value is not None:
        scores["judge"] = judge_value
    if scores:
        overall = sum(scores.values()) / len(scores)
        passed = overall >= 0.6
    else:
        overall = 1.0
        passed = True
    return round(overall, 4), passed


@pytest.mark.asyncio
@pytest.mark.parametrize("kw,schema,output,judge", [
    (["pong"], "{}", "pong!", None),                 # keyword only, pass
    (["hello"], "{}", "nope", None),                 # keyword only, fail
    ([], "{}", "anything", None),                    # no assertions, pass
    (["a", "b"], "{}", "a only", None),              # keyword 0.5 → fail at 0.6
    (["a", "b", "c"], "{}", "a b only", None),       # keyword 0.66 → pass
    ([], {"type": "object", "required": ["x"]}, '{"x":1}', None),  # schema pass
    ([], {"type": "object", "required": ["x"]}, "notjson", None),  # schema fail
])
async def test_legacy_case_parity_no_judge(monkeypatch, kw, schema, output, judge):
    case = _case(
        expected_keywords=json.dumps(kw),
        expected_schema=json.dumps(schema) if schema != "{}" else "{}",
        judge_prompt="",
        dimensions_json="",  # legacy path
    )
    ref_overall, ref_passed = _legacy_reference(case, output, judge)
    res = await ee.score_case(case, output, case_threshold=ee.PASS_THRESHOLD)
    assert res.passed == ref_passed
    assert res.overall == pytest.approx(ref_overall)


@pytest.mark.asyncio
async def test_legacy_case_with_judge_parity(monkeypatch):
    async def fake_judge(prompt, user_input, output):
        return 0.4, "meh"

    monkeypatch.setattr("app.core.scorers.judge._llm_judge", fake_judge)
    case = _case(
        expected_keywords=json.dumps(["pong"]),
        judge_prompt="be good",
        dimensions_json="",
    )
    # legacy: keyword 1.0 + judge 0.4 → mean 0.7 → pass
    ref_overall, ref_passed = _legacy_reference(case, "pong", 0.4)
    res = await ee.score_case(case, "pong", case_threshold=ee.PASS_THRESHOLD)
    assert res.overall == pytest.approx(ref_overall) == pytest.approx(0.7)
    assert res.passed == ref_passed is True


@pytest.mark.asyncio
async def test_legacy_judge_unavailable_skipped(monkeypatch):
    async def fake_judge(prompt, user_input, output):
        return None

    monkeypatch.setattr("app.core.scorers.judge._llm_judge", fake_judge)
    case = _case(expected_keywords=json.dumps(["pong"]), judge_prompt="x", dimensions_json="")
    res = await ee.score_case(case, "pong", case_threshold=ee.PASS_THRESHOLD)
    # judge skipped → only keyword 1.0 counts → overall 1.0, pass (matches legacy)
    assert res.overall == pytest.approx(1.0)
    assert res.passed is True


# ─── build_summary dimension aggregation ─────────────────────────────────────


class _Row:
    """Minimal stand-in for EvaluationCaseResult for build_summary."""
    def __init__(self, case_id, is_key, passed, overall, dims, duration_ms=10):
        self.case_id = case_id
        self.case_name = f"case-{case_id}"
        self.is_key = is_key
        self.passed = passed
        self.scores = json.dumps({"overall": overall})
        self.dimension_results_json = json.dumps({"dimensions": dims, "overall": overall})
        self.duration_ms = duration_ms


def test_build_summary_dimension_aggregates():
    rows = [
        _Row(1, True, True, 0.9, [
            {"dimension": "goal", "type": "judge", "score": 0.9, "passed": True,
             "threshold": 0.6, "weight": 1.0, "required": True, "skipped": False},
        ]),
        _Row(2, False, False, 0.3, [
            {"dimension": "goal", "type": "judge", "score": 0.3, "passed": False,
             "threshold": 0.6, "weight": 1.0, "required": True, "skipped": False},
        ]),
    ]
    summary = ee.build_summary(rows, token_input=10, token_output=20)
    assert summary["dimension_averages"]["goal"] == pytest.approx(0.6)
    assert summary["dimension_fail_rates"]["goal"] == pytest.approx(0.5)
    failures = summary["required_dimension_failures"]
    assert len(failures) == 1 and failures[0]["case_id"] == 2
    assert failures[0]["dimension"] == "goal"


def test_build_summary_excludes_skipped_dims():
    rows = [
        _Row(1, False, True, 1.0, [
            {"dimension": "rel", "type": "ragas", "score": 0.0, "passed": False,
             "threshold": 0.6, "weight": 0.5, "required": False, "skipped": True},
            {"dimension": "kw", "type": "keyword", "score": 1.0, "passed": True,
             "threshold": 0.6, "weight": 0.5, "required": False, "skipped": False},
        ]),
    ]
    summary = ee.build_summary(rows, 0, 0)
    # skipped ragas dim excluded entirely from aggregates
    assert "rel" not in summary["dimension_averages"]
    assert summary["dimension_averages"]["kw"] == pytest.approx(1.0)
    assert summary["required_dimension_failures"] == []


def test_template_defaults():
    from app.core.scorers.templates import get_template
    general = get_template("general")
    planner = get_template("planner")
    assert {d["name"] for d in planner["dimensions"]} == {
        "goal_completion", "format_compliance", "answer_relevancy"}
    # returned copy is independent of the module constant
    general["dimensions"].append({"x": 1})
    assert len(get_template("general")["dimensions"]) == 3
