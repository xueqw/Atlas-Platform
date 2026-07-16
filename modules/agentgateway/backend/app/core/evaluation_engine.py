"""Evaluation engine — batch-run a suite's cases through the agent's DAG,
score each case across its declared dimensions, and persist one EvaluationRun
plus per-case EvaluationCaseResult rows.

Scoring is dimension-driven (Phase 1): a case declares dimensions in
``dimensions_json`` ({name, type, weight, threshold, required, ...}); each is
dispatched through the scorer registry (keyword/schema/rule/judge/ragas) and
aggregated into a weighted overall + pass/fail. Cases without dimensions fall
back to ``_legacy_dimensions`` — an equal-weight mapping of the old
keyword/schema/judge fields whose pass/fail is identical to the pre-refactor
fixed-three-dimension logic.

Reuses the same DAGRunner path as chat so evaluation runs exactly what
production runs (and inherits Langfuse traces + token accounting). Degrades
gracefully when Langfuse is unconfigured: trace_id falls back to empty string
and the run still completes.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from sqlmodel import Session, select, desc

from app.core.dag_executor import DAGParser, DAGRunner, StateManager
import app.core.scorers as scorers
from app.core.scorers import DimensionResult, ScoreContext, get_scorer, skipped_result
from app.models.db import (
    Agent,
    AgentTestCase,
    DAGGraph,
    EvaluationCaseResult,
    EvaluationRun,
    EvaluationSuite,
    PromptVersion,
)

_log = logging.getLogger(__name__)

PASS_THRESHOLD = 0.6
# Re-exported for backward compatibility with anything importing them here.
JUDGE_PROVIDER = scorers.judge.JUDGE_PROVIDER
JUDGE_MODEL = scorers.judge.JUDGE_MODEL


@dataclass
class CaseScore:
    scores: Dict[str, float] = field(default_factory=dict)
    overall: float = 1.0
    passed: bool = True
    dimension_results: List[DimensionResult] = field(default_factory=list)
    evidence: Dict[str, Any] = field(default_factory=dict)


def _extract_model_name(graph_json: str) -> str:
    """Pull a representative model name from the DAG's agent / m node config."""
    try:
        raw = json.loads(graph_json) if isinstance(graph_json, str) else graph_json
    except (json.JSONDecodeError, TypeError):
        return ""
    for node in raw.get("nodes", []):
        ntype = node.get("type", node.get("node_type", ""))
        if ntype in ("agent", "m"):
            cfg = node.get("config", {}) or {}
            model = cfg.get("model_name") or cfg.get("model")
            if model:
                return str(model)
    return ""


def _safe_json(raw: str, default: Any) -> Any:
    try:
        value = json.loads(raw) if isinstance(raw, str) and raw.strip() else default
    except (json.JSONDecodeError, TypeError):
        return default
    return value if value is not None else default


def _parse_dimensions(case: AgentTestCase) -> List[Dict[str, Any]]:
    """Extract the declared dimension list from a case's ``dimensions_json``.

    Accepts both ``{"dimensions": [...]}`` and a bare ``[...]`` list. Returns an
    empty list when unset/malformed so the caller can fall back to legacy.
    """
    raw = (case.dimensions_json or "").strip()
    if not raw:
        return []
    parsed = _safe_json(raw, None)
    if isinstance(parsed, dict):
        dims = parsed.get("dimensions", [])
    elif isinstance(parsed, list):
        dims = parsed
    else:
        dims = []
    return [d for d in dims if isinstance(d, dict)]


def _case_threshold(case: AgentTestCase, default: float) -> float:
    """Per-case overall threshold: dimensions_json top-level override, else default."""
    parsed = _safe_json(case.dimensions_json or "", {})
    if isinstance(parsed, dict):
        for key in ("pass_threshold", "threshold"):
            if key in parsed:
                try:
                    return max(0.0, min(1.0, float(parsed[key])))
                except (TypeError, ValueError):
                    pass
    return max(0.0, min(1.0, default))


def _legacy_dimensions(case: AgentTestCase) -> List[Dict[str, Any]]:
    """Map a pre-refactor case's fixed fields to equal-weight dimensions.

    Mirrors the old ``score_case`` exactly: a dimension is emitted only for a
    field that is actually set (non-empty keywords / schema / judge_prompt), all
    equal weight and non-required, so the weighted overall reduces to the old
    mean and ``case_pass`` reduces to ``overall >= 0.6``.
    """
    dims: List[Dict[str, Any]] = []

    keywords = _safe_json(case.expected_keywords or "[]", [])
    if isinstance(keywords, list) and keywords:
        dims.append({"name": "keyword", "type": "keyword", "weight": 1.0,
                     "threshold": PASS_THRESHOLD, "required": False,
                     "keywords": keywords})

    schema = _safe_json(case.expected_schema or "{}", {})
    if isinstance(schema, dict) and schema:
        dims.append({"name": "schema", "type": "schema", "weight": 1.0,
                     "threshold": PASS_THRESHOLD, "required": False,
                     "schema": schema})

    if (case.judge_prompt or "").strip():
        dims.append({"name": "judge", "type": "judge", "weight": 1.0,
                     "threshold": PASS_THRESHOLD, "required": False,
                     "judge_prompt": case.judge_prompt})

    return dims


def _build_context(case: AgentTestCase, output: str,
                   latency_ms: float = 0.0,
                   token_input: int = 0, token_output: int = 0,
                   steps: Optional[list] = None, trace_id: str = "") -> ScoreContext:
    """Assemble the ScoreContext from a case + its agent output.

    ``steps`` (execution trajectory) and ``trace_id`` feed trace-aware scorers;
    they default to empty so non-DAG / legacy callers are unaffected.
    """
    ctx_blob = _safe_json(getattr(case, "context_json", "") or "{}", {})
    if isinstance(ctx_blob, dict):
        contexts = ctx_blob.get("retrieved_contexts") or ctx_blob.get("contexts") or []
    elif isinstance(ctx_blob, list):
        contexts = ctx_blob
    else:
        contexts = []
    if not isinstance(contexts, list):
        contexts = [contexts]

    constraints = _safe_json(getattr(case, "constraints_json", "") or "{}", {})
    if not isinstance(constraints, dict):
        constraints = {}

    metadata = _safe_json(getattr(case, "metadata_json", "") or "{}", {})
    if not isinstance(metadata, dict):
        metadata = {}
    # Surface legacy fields so scorers can read them through metadata too.
    metadata.setdefault("expected_keywords", _safe_json(case.expected_keywords or "[]", []))
    metadata.setdefault("expected_schema", _safe_json(case.expected_schema or "{}", {}))
    metadata.setdefault("judge_prompt", case.judge_prompt or "")

    return ScoreContext(
        user_input=case.input_message or "",
        output=output or "",
        reference_output=getattr(case, "reference_output", "") or "",
        contexts=contexts,
        constraints=constraints,
        metadata=metadata,
        latency_ms=latency_ms,
        token_input=token_input,
        token_output=token_output,
        steps=list(steps) if steps else [],
        trace_id=trace_id or "",
    )


def _normalize_weights(dims: List[DimensionResult]) -> List[float]:
    """Normalized weights over scored (non-skipped) dimensions.

    Falls back to equal weights when all weights are zero/absent.
    """
    weights = [max(0.0, d.weight) for d in dims]
    total = sum(weights)
    if total <= 0:
        n = len(dims)
        return [1.0 / n] * n if n else []
    return [w / total for w in weights]


def _aggregate(results: List[DimensionResult], case_threshold: float) -> tuple[float, bool]:
    """Weighted overall + pass/fail over a case's dimension results.

    overall = Σ(score × normalized weight) across non-skipped dimensions.
    case_pass = no required (non-skipped) dimension failed AND overall ≥ threshold.
    Skipped dimensions are excluded from the overall and never fail the case.
    With no scorable dimensions overall defaults to 1.0 (old "no assertions").
    """
    scored = [d for d in results if not d.skipped]
    if not scored:
        overall = 1.0
    else:
        weights = _normalize_weights(scored)
        overall = sum(d.score * w for d, w in zip(scored, weights))

    required_failed = any(d.required and not d.passed for d in scored)
    case_pass = (not required_failed) and overall >= case_threshold
    return overall, case_pass


async def score_case(case: AgentTestCase, output: str,
                     case_threshold: float = PASS_THRESHOLD,
                     latency_ms: float = 0.0,
                     token_input: int = 0, token_output: int = 0,
                     steps: Optional[list] = None, trace_id: str = "") -> CaseScore:
    """Score one case output across whichever dimensions it declares.

    Resolves the dimension list (declared ``dimensions_json`` or the legacy
    mapping), dispatches each through the scorer registry, then aggregates a
    weighted overall and pass/fail. Unknown dimension types and malformed
    configs are isolated as *skipped* dimensions (recorded with a reason) rather
    than crashing the run. ``steps`` / ``trace_id`` feed trace-aware dimensions;
    omitting them leaves trace dimensions to skip on an empty trajectory.
    """
    declared = _parse_dimensions(case)
    dim_cfgs = declared if declared else _legacy_dimensions(case)
    threshold = _case_threshold(case, case_threshold)

    ctx = _build_context(case, output, latency_ms, token_input, token_output,
                         steps=steps, trace_id=trace_id)

    results: List[DimensionResult] = []
    for dim_cfg in dim_cfgs:
        dim_type = str(dim_cfg.get("type", "")).strip()
        scorer = get_scorer(dim_type)
        if scorer is None:
            results.append(skipped_result(
                dim_cfg,
                f"未知维度类型 '{dim_type}'，已跳过",
                evidence={"skipped_reason": "unknown dimension type",
                          "type": dim_type,
                          "registered": scorers.registered_types()},
            ))
            continue
        try:
            results.append(await scorer(dim_cfg, ctx))
        except Exception as exc:  # an individual scorer must never crash the run
            _log.warning("scorer for %s failed: %s: %s",
                         dim_type, type(exc).__name__, exc)
            results.append(skipped_result(
                dim_cfg,
                f"评分器异常({type(exc).__name__})，已跳过",
                evidence={"skipped_reason": "scorer error", "error": str(exc)[:300]},
            ))

    overall, passed = _aggregate(results, threshold)

    # Flat scores keep the legacy shape: {dim_name: score, ..., "overall": x}.
    flat = {d.dimension: round(float(d.score), 4) for d in results if not d.skipped}
    evidence = {d.dimension: d.evidence for d in results}

    return CaseScore(
        scores=flat,
        overall=round(overall, 4),
        passed=passed,
        dimension_results=results,
        evidence=evidence,
    )


def _percentile(sorted_vals: List[float], pct: float) -> float:
    if not sorted_vals:
        return 0.0
    idx = int(len(sorted_vals) * pct)
    return float(sorted_vals[min(idx, len(sorted_vals) - 1)])


def build_summary(case_results: List[EvaluationCaseResult],
                  token_input: int, token_output: int) -> dict:
    """Aggregate per-case results into a run-level summary.

    Beyond pass_rate / key_pass_rate / avg_score / latency / token, includes
    per-dimension aggregates for downstream release gates:
      - dimension_averages: mean score per dimension (over scored occurrences)
      - dimension_fail_rates: fraction of scored occurrences that failed
      - required_dimension_failures: cases where a required dimension failed
    """
    total = len(case_results)
    passed = sum(1 for r in case_results if r.passed)
    key_results = [r for r in case_results if r.is_key]
    key_total = len(key_results)
    key_passed = sum(1 for r in key_results if r.passed)

    overalls = []
    for r in case_results:
        try:
            overalls.append(float(json.loads(r.scores or "{}").get("overall", 0.0)))
        except (json.JSONDecodeError, TypeError, ValueError):
            overalls.append(0.0)
    avg_score = round(sum(overalls) / total, 4) if total else 0.0

    latencies = sorted(float(r.duration_ms) for r in case_results)
    avg_latency = round(sum(latencies) / total, 2) if total else 0.0

    # Per-dimension aggregation from each row's dimension_results_json.
    dim_scores: Dict[str, List[float]] = {}
    dim_fails: Dict[str, List[int]] = {}
    required_failures: List[dict] = []
    for r in case_results:
        payload = _safe_json(getattr(r, "dimension_results_json", "") or "{}", {})
        dims = payload.get("dimensions", []) if isinstance(payload, dict) else []
        for d in dims:
            if not isinstance(d, dict) or d.get("skipped"):
                continue
            name = str(d.get("dimension", d.get("name", "")))
            if not name:
                continue
            try:
                score = float(d.get("score", 0.0))
            except (TypeError, ValueError):
                score = 0.0
            is_passed = bool(d.get("passed", False))
            dim_scores.setdefault(name, []).append(score)
            dim_fails.setdefault(name, []).append(0 if is_passed else 1)
            if d.get("required") and not is_passed:
                required_failures.append({
                    "case_id": r.case_id,
                    "case_name": r.case_name,
                    "dimension": name,
                    "score": round(score, 4),
                    "threshold": round(float(d.get("threshold", 0.0) or 0.0), 4),
                })

    dimension_averages = {
        name: round(sum(vals) / len(vals), 4) for name, vals in dim_scores.items() if vals
    }
    dimension_fail_rates = {
        name: round(sum(vals) / len(vals), 4) for name, vals in dim_fails.items() if vals
    }

    return {
        "total_cases": total,
        "passed_cases": passed,
        "pass_rate": round(passed / total, 4) if total else 0.0,
        "key_total": key_total,
        "key_passed": key_passed,
        "key_pass_rate": round(key_passed / key_total, 4) if key_total else None,
        "avg_score": avg_score,
        "latency_avg_ms": avg_latency,
        "latency_p50_ms": _percentile(latencies, 0.5),
        "latency_p95_ms": _percentile(latencies, 0.95),
        "token_input": token_input,
        "token_output": token_output,
        "dimension_averages": dimension_averages,
        "dimension_fail_rates": dimension_fail_rates,
        "required_dimension_failures": required_failures,
    }


def _parse_turns(case: "AgentTestCase") -> List[Dict[str, str]]:
    """Extract multi-turn conversation turns from a case.

    Returns a list of dicts with at least ``user_query``; returns an empty list
    when the case is single-turn (caller falls back to ``input_message``).
    """
    raw = (getattr(case, "turns_json", None) or "").strip()
    if not raw:
        return []
    parsed = _safe_json(raw, None)
    if isinstance(parsed, list) and parsed:
        return [t for t in parsed if isinstance(t, dict) and t.get("user_query")]
    return []


async def _execute_case(case, parser, agent_id: int, dag_version: int):
    """Execute a single case (single or multi-turn) through the DAG.

    Returns (output, trace_id, token_in, token_out, total_duration_ms, steps).
    Multi-turn cases execute turns sequentially with the same DAGRunner state so
    conversation context is preserved across turns.
    """
    turns = _parse_turns(case)
    runner = DAGRunner(parser, state_manager=StateManager())

    if not turns:
        # Single-turn: original behavior
        exec_result = await runner.run(
            case.input_message, agent_id=agent_id, dag_version=dag_version
        )
        steps = [
            {
                "node_id": r.node_id, "node_type": r.node_type,
                "status": r.status, "duration_ms": r.duration_ms,
                "tokens": r.tokens, "error": r.error,
            }
            for r in (exec_result.results or [])
        ]
        return (
            str(exec_result.final_output or ""),
            exec_result.trace_id or "",
            exec_result.token_input,
            exec_result.token_output,
            exec_result.total_duration_ms,
            steps,
        )

    # Multi-turn: sequential execution preserving runner state
    all_outputs: List[str] = []
    all_steps: List[dict] = []
    total_token_in = 0
    total_token_out = 0
    total_duration = 0.0
    last_trace_id = ""

    for turn in turns:
        exec_result = await runner.run(
            turn["user_query"], agent_id=agent_id, dag_version=dag_version
        )
        output = str(exec_result.final_output or "")
        all_outputs.append(output)
        last_trace_id = exec_result.trace_id or last_trace_id
        total_token_in += exec_result.token_input
        total_token_out += exec_result.token_output
        total_duration += exec_result.total_duration_ms
        all_steps.extend([
            {
                "node_id": r.node_id, "node_type": r.node_type,
                "status": r.status, "duration_ms": r.duration_ms,
                "tokens": r.tokens, "error": r.error,
            }
            for r in (exec_result.results or [])
        ])

    # Final output is the last turn's response (most meaningful for scoring)
    final_output = all_outputs[-1] if all_outputs else ""
    return (final_output, last_trace_id, total_token_in, total_token_out, total_duration, all_steps)


async def run_suite(agent_id: int, suite_id: int, session: Session) -> EvaluationRun:
    """Execute every case in a suite against the agent's latest DAG version.

    Persists one EvaluationRun and N EvaluationCaseResult rows, then returns
    the run. Raises ValueError on missing agent / suite / DAG.
    """
    agent = session.get(Agent, agent_id)
    if not agent:
        raise ValueError("Agent not found")
    suite = session.get(EvaluationSuite, suite_id)
    if not suite or suite.agent_id != agent_id:
        raise ValueError("Suite not found")

    dag = session.exec(
        select(DAGGraph).where(DAGGraph.agent_id == agent_id).order_by(desc(DAGGraph.version))
    ).first()
    if not dag:
        raise ValueError("Agent has no DAG graph")

    parser = DAGParser(dag.graph_json)
    if parser.has_cycles():
        raise ValueError("DAG contains cycles")

    cases = session.exec(
        select(AgentTestCase)
        .where(AgentTestCase.suite_id == suite_id)
        .order_by(AgentTestCase.sort_order)
    ).all()

    prompt_version_row = session.exec(
        select(PromptVersion)
        .where(PromptVersion.agent_id == agent_id)
        .order_by(desc(PromptVersion.version_number))
    ).first()
    prompt_version = prompt_version_row.version_number if prompt_version_row else agent.version

    suite_threshold = float(getattr(suite, "pass_threshold", PASS_THRESHOLD) or PASS_THRESHOLD)

    run = EvaluationRun(
        agent_id=agent_id,
        suite_id=suite_id,
        dag_version=dag.version,
        prompt_version=prompt_version,
        model=_extract_model_name(dag.graph_json),
        trace_ids="[]",
        summary="{}",
        passed=False,
    )
    session.add(run)
    session.commit()
    session.refresh(run)

    case_rows: List[EvaluationCaseResult] = []
    trace_ids: List[str] = []
    total_token_in = 0
    total_token_out = 0

    for case in cases:
        output, trace_id, case_token_in, case_token_out, case_duration_ms, steps = \
            await _execute_case(case, parser, agent_id, dag.version)
        trace_ids.append(trace_id)
        total_token_in += case_token_in
        total_token_out += case_token_out

        case_score = await score_case(
            case, output,
            case_threshold=suite_threshold,
            latency_ms=float(case_duration_ms),
            token_input=case_token_in,
            token_output=case_token_out,
            steps=steps,
            trace_id=trace_id,
        )
        dim_payload = {
            "dimensions": [d.to_dict() for d in case_score.dimension_results],
            "overall": case_score.overall,
        }

        # Best-effort: write this case's dimension + overall scores back to
        # Langfuse as evidence. Returns [] (and never raises) when Langfuse is
        # unconfigured/unreachable, so the run completes regardless.
        score_ids: List[str] = []
        try:
            from app.core.observability import record_evaluation_scores

            score_ids = record_evaluation_scores(
                {
                    "agent_id": agent_id,
                    "suite_id": suite_id,
                    "case_id": case.id,
                    "run_id": run.id,
                    "dag_version": dag.version,
                    "prompt_version": prompt_version,
                    "model": run.model,
                    "trace_id": trace_id,
                    "overall": case_score.overall,
                },
                dim_payload["dimensions"],
            )
        except Exception as exc:  # defensive: writeback must never fail the run
            _log.warning("record_evaluation_scores raised: %s: %s",
                         type(exc).__name__, exc)
            score_ids = []

        row = EvaluationCaseResult(
            run_id=run.id,
            case_id=case.id,
            case_name=case.name,
            is_key=case.is_key,
            output=output[:50000],
            scores=json.dumps({**case_score.scores, "overall": case_score.overall}, ensure_ascii=False),
            dimension_results_json=json.dumps(dim_payload, ensure_ascii=False),
            evidence_json=json.dumps(case_score.evidence, ensure_ascii=False),
            langfuse_score_ids=json.dumps(score_ids, ensure_ascii=False),
            passed=case_score.passed,
            trace_id=trace_id,
            duration_ms=int(case_duration_ms),
        )
        session.add(row)
        case_rows.append(row)

    summary = build_summary(case_rows, total_token_in, total_token_out)
    run.trace_ids = json.dumps([t for t in trace_ids if t], ensure_ascii=False)
    run.summary = json.dumps(summary, ensure_ascii=False)
    # A run passes when every key case passes (or, absent key cases, all cases).
    if summary["key_total"] > 0:
        run.passed = summary["key_passed"] == summary["key_total"]
    else:
        run.passed = summary["passed_cases"] == summary["total_cases"] and summary["total_cases"] > 0

    session.add(run)
    session.commit()
    session.refresh(run)
    return run
