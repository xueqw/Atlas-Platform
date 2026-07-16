"""Regression comparison — diff two EvaluationRuns of the same agent.

Pure computation over already-persisted per-case results; never re-runs the
agent. Surfaces overall score delta, key-case pass-rate delta, latency/token
deltas, and the regression list (cases that passed in the baseline run but
fail in the candidate run), each tagged with both runs' version identifiers.
"""

from __future__ import annotations

import json
from typing import Dict, List, Optional

from sqlmodel import Session, select

from app.models.db import EvaluationCaseResult, EvaluationRun


def _load_summary(run: EvaluationRun) -> dict:
    try:
        return json.loads(run.summary or "{}")
    except json.JSONDecodeError:
        return {}


def _version_tag(run: EvaluationRun) -> dict:
    return {
        "run_id": run.id,
        "dag_version": run.dag_version,
        "prompt_version": run.prompt_version,
        "model": run.model,
        "created_at": run.created_at.isoformat(),
    }


def _case_map(session: Session, run_id: int) -> Dict[int, EvaluationCaseResult]:
    rows = session.exec(
        select(EvaluationCaseResult).where(EvaluationCaseResult.run_id == run_id)
    ).all()
    return {r.case_id: r for r in rows}


def _delta(candidate: Optional[float], baseline: Optional[float]) -> Optional[float]:
    if candidate is None or baseline is None:
        return None
    return round(candidate - baseline, 4)


def compare_runs(baseline_id: int, candidate_id: int, session: Session) -> dict:
    """Compare a baseline run against a candidate run.

    ``baseline`` is the "before" run, ``candidate`` the "after". Regressions are
    cases that passed in baseline and fail in candidate. Raises ValueError when
    a run is missing or the two runs belong to different agents.
    """
    baseline = session.get(EvaluationRun, baseline_id)
    candidate = session.get(EvaluationRun, candidate_id)
    if not baseline or not candidate:
        raise ValueError("Run not found")
    if baseline.agent_id != candidate.agent_id:
        raise ValueError("Runs belong to different agents")

    base_sum = _load_summary(baseline)
    cand_sum = _load_summary(candidate)

    base_cases = _case_map(session, baseline_id)
    cand_cases = _case_map(session, candidate_id)

    regressions: List[dict] = []
    improvements: List[dict] = []
    for case_id, base_row in base_cases.items():
        cand_row = cand_cases.get(case_id)
        if cand_row is None:
            continue
        if base_row.passed and not cand_row.passed:
            regressions.append({
                "case_id": case_id,
                "case_name": base_row.case_name,
                "is_key": base_row.is_key,
                "baseline_passed": True,
                "candidate_passed": False,
            })
        elif not base_row.passed and cand_row.passed:
            improvements.append({
                "case_id": case_id,
                "case_name": cand_row.case_name,
                "is_key": cand_row.is_key,
            })

    return {
        "baseline": _version_tag(baseline),
        "candidate": _version_tag(candidate),
        "deltas": {
            "avg_score": _delta(cand_sum.get("avg_score"), base_sum.get("avg_score")),
            "pass_rate": _delta(cand_sum.get("pass_rate"), base_sum.get("pass_rate")),
            "key_pass_rate": _delta(cand_sum.get("key_pass_rate"), base_sum.get("key_pass_rate")),
            "latency_avg_ms": _delta(cand_sum.get("latency_avg_ms"), base_sum.get("latency_avg_ms")),
            "token_input": _delta(cand_sum.get("token_input"), base_sum.get("token_input")),
            "token_output": _delta(cand_sum.get("token_output"), base_sum.get("token_output")),
        },
        "baseline_summary": base_sum,
        "candidate_summary": cand_sum,
        "regressions": regressions,
        "improvements": improvements,
    }
