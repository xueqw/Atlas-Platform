"""Release gate — quality check that runs before an agent is published.

Evaluates the agent against the minimal generic criteria built on main line B
(EvaluationRun: pass rate / key cases) and main line A (AgentRunSummary:
error rate / latency / token), PLUS — when the evaluation produced dimension
data (Phase 1) — per-dimension quality gates resolved from a suite_type
template (Phase 3). Missing evaluation or run data is treated as a FAILURE — the
gate never defaults to pass when it cannot judge. A run that simply lacks
dimension data is NOT a failure: the dimension checks are skipped and the gate
falls back to the generic checks (behavior is never weakened).

Effective policy is resolved in four layers (later overrides earlier, per key):
system default ← suite_type template ← suite.release_gate_policy_json ←
agent.release_gate_thresholds.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Optional

from sqlmodel import Session, select, desc

from app.models.db import Agent, AgentRunSummary, EvaluationRun, EvaluationSuite

_log = logging.getLogger(__name__)


# ─── Thresholds & policy ─────────────────────────────────────────────────────

# System defaults for the GENERIC checks. Override per-agent via
# Agent.release_gate_thresholds (JSON object keyed by the same names).
DEFAULT_THRESHOLDS: dict = {
    "min_pass_rate": 0.9,          # overall case pass rate must be >= this
    "max_error_rate": 0.05,        # observability error rate must be < this
    "max_latency_p95_ms": 30000.0, # latency ceiling (eval p95, else run avg)
    "max_avg_token": 100000,       # avg total tokens per run must be <= this
    # How many recent runs to aggregate for observability metrics.
    "run_window": 20,
}

# Per-suite_type dimension gate templates (Phase 3). Each entry is a list of
# {name, min_avg, required_no_fail}. Dimension names align with the Phase 1
# dimension templates (scorers/templates.py) so the gate consumes exactly what
# the evaluation produces. A dimension a run does not contain is skipped (never
# a failure) — so customer_support / rag entries below are safe to declare even
# before their Phase 4 dimensions exist. ``general`` declares no dimension gate
# (equivalent to the pre-Phase-3 generic-only behavior). Thresholds follow the
# Ragas plan §10.2 conservative values.
GATE_TEMPLATES: dict[str, list[dict]] = {
    "general": [],
    "planner": [
        {"name": "goal_completion", "min_avg": 0.8, "required_no_fail": True},
    ],
    "customer_support": [
        {"name": "constraint_following", "min_avg": 0.9, "required_no_fail": True},
    ],
    "rag": [
        {"name": "faithfulness", "min_avg": 0.85, "required_no_fail": True,
         "min_coverage": 1.0},
        {"name": "context_precision", "min_avg": 0.7, "required_no_fail": False,
         "min_coverage": 0.9},
        {"name": "answer_relevancy", "min_avg": 0.7, "required_no_fail": False,
         "min_coverage": 0.9},
    ],
}

# Disabled until a team explicitly promotes a successful run to baseline. This
# avoids treating a first-ever certification run as a regression while making
# every later candidate compare against an intentional, versioned reference.
DEFAULT_REGRESSION_POLICY: dict = {
    "enabled": False,
    "baseline_run_id": None,
    "max_avg_score_drop": 0.03,
    "max_pass_rate_drop": 0.02,
    "max_new_case_regressions": 0,
}


def _coerce_json_obj(raw) -> dict:
    """Parse a JSON string / dict-ish value into a dict, else {}."""
    if not raw:
        return {}
    try:
        return json.loads(raw) if isinstance(raw, str) else dict(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return {}


def resolve_thresholds(agent: Agent) -> dict:
    """Merge per-agent overrides over system defaults (override wins per key).

    Kept for backward compatibility (callers/tests depend on it); the full
    four-layer policy resolution lives in ``resolve_gate_policy``.
    """
    effective = dict(DEFAULT_THRESHOLDS)
    override = _coerce_json_obj(getattr(agent, "release_gate_thresholds", None))
    for key, value in override.items():
        if key in effective and value is not None:
            effective[key] = value
    return effective


def _merge_dimensions(base: list[dict], override: list[dict]) -> list[dict]:
    """Merge dimension lists by name; override entries replace/extend base ones."""
    by_name: dict[str, dict] = {}
    for d in base + override:
        if not isinstance(d, dict):
            continue
        name = str(d.get("name", "")).strip()
        if not name:
            continue
        merged = dict(by_name.get(name, {}))
        merged.update({k: v for k, v in d.items() if v is not None})
        by_name[name] = merged
    return list(by_name.values())


def resolve_gate_policy(agent: Agent, suite: Optional[EvaluationSuite]) -> dict:
    """Resolve the effective gate policy by layering four sources.

    Priority (later overrides earlier, per key): system default ← suite_type
    template ← suite.release_gate_policy_json ← agent.release_gate_thresholds.

    Returns generic, dimension and baseline-regression policy sections.
    """
    # Generic thresholds start from system default; per-suite then per-agent override.
    generic = dict(DEFAULT_THRESHOLDS)
    # Dimensions start from the suite_type template.
    suite_type = (suite.suite_type if suite else "general") or "general"
    dimensions = [dict(d) for d in GATE_TEMPLATES.get(suite_type, [])]
    regression = dict(DEFAULT_REGRESSION_POLICY)

    # Layer 3: per-suite policy override (generic + dimensions).
    if suite is not None:
        suite_policy = _coerce_json_obj(getattr(suite, "release_gate_policy_json", None))
        for key, value in (suite_policy.get("generic") or {}).items():
            if key in generic and value is not None:
                generic[key] = value
        suite_dims = suite_policy.get("dimensions")
        if isinstance(suite_dims, list):
            dimensions = _merge_dimensions(dimensions, suite_dims)
        if isinstance(suite_policy.get("regression"), dict):
            regression.update({k: v for k, v in suite_policy["regression"].items() if v is not None})

    # Layer 4: per-agent override. Back-compat: release_gate_thresholds may be a
    # flat generic-threshold object OR a structured {generic, dimensions} policy.
    agent_policy = _coerce_json_obj(getattr(agent, "release_gate_thresholds", None))
    agent_generic = agent_policy.get("generic") if isinstance(agent_policy.get("generic"), dict) else agent_policy
    for key, value in (agent_generic or {}).items():
        if key in generic and value is not None:
            generic[key] = value
    agent_dims = agent_policy.get("dimensions")
    if isinstance(agent_dims, list):
        dimensions = _merge_dimensions(dimensions, agent_dims)
    if isinstance(agent_policy.get("regression"), dict):
        regression.update({k: v for k, v in agent_policy["regression"].items() if v is not None})

    return {"generic": generic, "dimensions": dimensions,
            "regression": regression, "suite_type": suite_type}


# ─── Result ──────────────────────────────────────────────────────────────────


@dataclass
class GateResult:
    passed: bool
    reasons: list[str] = field(default_factory=list)   # human-readable failing reasons (back-compat)
    summary: dict = field(default_factory=dict)         # evidence for the UI
    failures: list[dict] = field(default_factory=list)  # structured: {code,label,dimension?,actual,threshold}


# ─── Data-source adapter ───────────────────────────────────────────────────────


def _latest_evaluation_run(agent_id: int, session: Session) -> Optional[EvaluationRun]:
    return session.exec(
        select(EvaluationRun)
        .where(EvaluationRun.agent_id == agent_id)
        .order_by(desc(EvaluationRun.created_at))
    ).first()


def _suite_for_run(run: Optional[EvaluationRun], session: Session) -> Optional[EvaluationSuite]:
    """Look up the EvaluationSuite a run belongs to (drives template choice)."""
    if run is None:
        return None
    suite_id = getattr(run, "suite_id", None)
    if suite_id is None:
        return None
    return session.get(EvaluationSuite, suite_id)


def _parse_summary(run: EvaluationRun) -> dict:
    try:
        return json.loads(run.summary or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}


def _observability_metrics(agent_id: int, window: int, session: Session) -> Optional[dict]:
    """Aggregate the most recent AgentRunSummary rows into gate inputs.

    Returns None when there is no observability data at all (so the caller can
    apply the missing-data safety default). Uses the shared aggregation helper
    in ``observability`` so the gate and the monitoring overview compute
    error_rate / latency with one identical caliber; the returned field names
    (run_count / error_rate / latency_p95_ms / avg_token) are kept stable for
    the gate's threshold checks and the UI summary.
    """
    from app.core.observability import recent_run_summaries, aggregate_run_metrics

    rows = recent_run_summaries(agent_id, window, session)
    if not rows:
        return None

    metrics = aggregate_run_metrics(rows)
    return {
        "run_count": metrics["run_count"],
        "error_rate": metrics["error_rate"],
        "latency_p95_ms": metrics["latency_p95_ms"],
        "avg_token": metrics["avg_token"],
    }


# ─── Gate ──────────────────────────────────────────────────────────────────────


def evaluate_release_gate(agent_id: int, session: Session) -> GateResult:
    """Run the release gate for an agent: generic checks + (when available)
    per-dimension quality gates resolved from the suite_type template.

    Returns GateResult(passed, reasons, summary, failures). Missing evaluation
    or observability data is a failure — the gate never defaults to pass. A run
    that merely lacks dimension data is NOT a failure: dimension checks are
    skipped and the generic checks decide.
    """
    agent = session.get(Agent, agent_id)
    if not agent:
        return GateResult(
            passed=False,
            reasons=["智能体不存在"],
            summary={},
            failures=[{"code": "agent_missing", "label": "智能体不存在"}],
        )

    reasons: list[str] = []
    failures: list[dict] = []

    # (1) A recent EvaluationRun must exist — picks the suite (hence template).
    run = _latest_evaluation_run(agent_id, session)
    suite = _suite_for_run(run, session)
    policy = resolve_gate_policy(agent, suite)
    thresholds = policy["generic"]
    summary: dict = {
        "thresholds": thresholds,
        "suite_type": policy["suite_type"],
        "gate_dimensions": policy["dimensions"],
        "regression_policy": policy["regression"],
    }

    if run is None:
        reasons.append("缺少评估数据，无法判定")
        failures.append({"code": "no_evaluation", "label": "缺少评估数据，无法判定"})
    else:
        eval_summary = _parse_summary(run)
        summary["evaluation"] = {
            "run_id": run.id,
            "passed": run.passed,
            "pass_rate": eval_summary.get("pass_rate"),
            "key_total": eval_summary.get("key_total"),
            "key_passed": eval_summary.get("key_passed"),
            "key_pass_rate": eval_summary.get("key_pass_rate"),
            "latency_p95_ms": eval_summary.get("latency_p95_ms"),
            "created_at": run.created_at.isoformat() if run.created_at else "",
        }

        # (2) Every key case must pass.
        key_total = eval_summary.get("key_total") or 0
        key_passed = eval_summary.get("key_passed") or 0
        if key_total > 0 and key_passed < key_total:
            reasons.append(f"关键 case 未全部通过（{key_passed}/{key_total}）")
            failures.append({
                "code": "key_cases_failed",
                "label": f"关键 case 未全部通过（{key_passed}/{key_total}）",
                "actual": key_passed,
                "threshold": key_total,
            })

        # (3) Overall pass rate must meet the threshold.
        pass_rate = eval_summary.get("pass_rate")
        min_pass_rate = thresholds["min_pass_rate"]
        if pass_rate is None:
            reasons.append("缺少评估通过率数据，无法判定")
            failures.append({"code": "no_pass_rate", "label": "缺少评估通过率数据，无法判定"})
        elif pass_rate < min_pass_rate:
            reasons.append(f"通过率 {pass_rate:.0%} 低于阈值 {min_pass_rate:.0%}")
            failures.append({
                "code": "pass_rate_below",
                "label": f"通过率 {pass_rate:.0%} 低于阈值 {min_pass_rate:.0%}",
                "actual": pass_rate,
                "threshold": min_pass_rate,
            })

        # (3b) Dimension-level gates (Phase 3). Only when the run carries
        # dimension data; a run without it falls back to generic checks only and
        # is NOT failed for the absence.
        _check_dimensions(eval_summary, policy["dimensions"], reasons, failures, summary)
        _check_regression(run, policy["regression"], reasons, failures, summary, session)

    # (4)+(5) Observability metrics: error rate / latency / token.
    obs = _observability_metrics(agent_id, int(thresholds["run_window"]), session)
    if obs is None:
        reasons.append("缺少运行数据，无法判定")
        failures.append({"code": "no_run_data", "label": "缺少运行数据，无法判定"})
    else:
        summary["observability"] = obs

        # (4) Error rate must be below the threshold.
        max_error_rate = thresholds["max_error_rate"]
        if obs["error_rate"] >= max_error_rate:
            reasons.append(f"error rate {obs['error_rate']:.0%} 不低于阈值 {max_error_rate:.0%}")
            failures.append({
                "code": "error_rate_high",
                "label": f"error rate {obs['error_rate']:.0%} 不低于阈值 {max_error_rate:.0%}",
                "actual": obs["error_rate"],
                "threshold": max_error_rate,
            })

        # (5) Latency / token / cost must be within range.
        max_latency = thresholds["max_latency_p95_ms"]
        if obs["latency_p95_ms"] > max_latency:
            reasons.append(f"latency p95 {obs['latency_p95_ms']:.0f}ms 超过上限 {max_latency:.0f}ms")
            failures.append({
                "code": "latency_high",
                "label": f"latency p95 {obs['latency_p95_ms']:.0f}ms 超过上限 {max_latency:.0f}ms",
                "actual": obs["latency_p95_ms"],
                "threshold": max_latency,
            })
        max_token = thresholds["max_avg_token"]
        if obs["avg_token"] > max_token:
            reasons.append(f"平均 token {obs['avg_token']:.0f} 超过上限 {max_token:.0f}")
            failures.append({
                "code": "token_high",
                "label": f"平均 token {obs['avg_token']:.0f} 超过上限 {max_token:.0f}",
                "actual": obs["avg_token"],
                "threshold": max_token,
            })

    return GateResult(passed=not reasons, reasons=reasons, summary=summary, failures=failures)


def _check_dimensions(
    eval_summary: dict,
    gate_dimensions: list[dict],
    reasons: list[str],
    failures: list[dict],
    summary: dict,
) -> None:
    """Apply per-dimension gates from the run's dimension data (Phase 3).

    Mutates ``reasons`` / ``failures`` / ``summary`` in place. No-ops (skips,
    never fails) when the run has no dimension data or a gated dimension is
    absent — the gate must not penalize an evaluation for lacking a dimension.
    """
    dimension_averages = eval_summary.get("dimension_averages")
    dimension_coverage = eval_summary.get("dimension_coverage") or {}
    required_dimension_failures = eval_summary.get("required_dimension_failures") or []

    # No dimension data at all → skip entirely, record nothing, fall back to generic.
    if not isinstance(dimension_averages, dict) or not dimension_averages:
        summary["dimensions_checked"] = False
        return

    summary["dimensions_checked"] = True
    summary["dimension_averages"] = dimension_averages
    skipped: list[str] = []

    for gd in gate_dimensions:
        name = str(gd.get("name", "")).strip()
        if not name:
            continue
        min_avg = gd.get("min_avg")
        min_coverage = gd.get("min_coverage")
        coverage = dimension_coverage.get(name) if isinstance(dimension_coverage, dict) else None
        coverage_rate = coverage.get("rate") if isinstance(coverage, dict) else None
        # Dimension declared in policy but absent in this run → skip, don't fail.
        if name not in dimension_averages:
            if min_coverage is not None:
                actual_rate = float(coverage_rate or 0.0)
                reasons.append(f"维度 {name} 评测覆盖率 {actual_rate:.0%} 低于阈值 {float(min_coverage):.0%}")
                failures.append({
                    "code": "dimension_coverage_below",
                    "label": f"维度 {name} 评测覆盖率 {actual_rate:.0%} 低于阈值 {float(min_coverage):.0%}",
                    "dimension": name,
                    "actual": actual_rate,
                    "threshold": min_coverage,
                })
            else:
                skipped.append(name)
            continue
        actual = dimension_averages.get(name)
        if min_avg is not None and isinstance(actual, (int, float)) and actual < min_avg:
            reasons.append(f"维度 {name} 均分 {actual:.2f} 低于阈值 {float(min_avg):.2f}")
            failures.append({
                "code": "dimension_below",
                "label": f"维度 {name} 均分 {actual:.2f} 低于阈值 {float(min_avg):.2f}",
                "dimension": name,
                "actual": actual,
                "threshold": min_avg,
            })
        if min_coverage is not None and (not isinstance(coverage_rate, (int, float)) or coverage_rate < min_coverage):
            actual_rate = float(coverage_rate or 0.0)
            reasons.append(f"维度 {name} 评测覆盖率 {actual_rate:.0%} 低于阈值 {float(min_coverage):.0%}")
            failures.append({
                "code": "dimension_coverage_below",
                "label": f"维度 {name} 评测覆盖率 {actual_rate:.0%} 低于阈值 {float(min_coverage):.0%}",
                "dimension": name,
                "actual": actual_rate,
                "threshold": min_coverage,
            })

    # required_dimension_failures: any required dimension that failed on a case.
    # Honor it when a gated dimension asks for required_no_fail (default True for
    # gated dimensions that set it); list the offending cases.
    gated_required = {
        str(gd.get("name", "")).strip()
        for gd in gate_dimensions
        if gd.get("required_no_fail")
    }
    relevant = [
        f for f in required_dimension_failures
        if isinstance(f, dict) and (not gated_required or str(f.get("dimension", "")) in gated_required)
    ] if gated_required else list(required_dimension_failures)
    if relevant:
        cases = "、".join(
            str(f.get("case_name") or f.get("case_id") or "?") for f in relevant[:5]
        )
        reasons.append(f"必过维度存在 case 失败（{len(relevant)} 个）：{cases}")
        failures.append({
            "code": "required_dimension_failed",
            "label": f"必过维度存在 case 失败（{len(relevant)} 个）",
            "cases": relevant,
        })

    if skipped:
        summary["dimensions_skipped"] = skipped

    # Quality evidence: worst (most-dragging) dimension by average.
    if dimension_averages:
        worst_name = min(dimension_averages, key=lambda k: dimension_averages[k])
        summary["worst_dimension"] = {"dimension": worst_name, "avg": dimension_averages[worst_name]}


def _check_regression(
    run: EvaluationRun,
    policy: dict,
    reasons: list[str],
    failures: list[dict],
    summary: dict,
    session: Session,
) -> None:
    """Compare the candidate run to an explicitly selected baseline."""
    if not policy.get("enabled"):
        summary["regression_checked"] = False
        return

    baseline_id = policy.get("baseline_run_id")
    if not isinstance(baseline_id, int) or baseline_id <= 0:
        summary["regression_checked"] = False
        reasons.append("回归门禁已启用，但尚未设置基线评测运行")
        failures.append({"code": "regression_baseline_missing", "label": "回归门禁已启用，但尚未设置基线评测运行"})
        return
    if baseline_id == run.id:
        summary["regression_checked"] = False
        reasons.append("候选运行不能同时作为回归基线")
        failures.append({"code": "regression_baseline_same_run", "label": "候选运行不能同时作为回归基线"})
        return

    try:
        from app.core.evaluation_compare import compare_runs
        comparison = compare_runs(baseline_id, run.id, session)
    except ValueError as exc:
        summary["regression_checked"] = False
        reasons.append("回归基线不可用")
        failures.append({"code": "regression_baseline_invalid", "label": "回归基线不可用", "detail": str(exc)})
        return

    deltas = comparison["deltas"]
    summary["regression_checked"] = True
    summary["regression"] = {
        "baseline": comparison["baseline"],
        "deltas": deltas,
        "new_case_regressions": len(comparison["regressions"]),
    }
    checks = (
        ("avg_score", "max_avg_score_drop", "平均得分"),
        ("pass_rate", "max_pass_rate_drop", "通过率"),
    )
    for delta_name, limit_name, label in checks:
        delta = deltas.get(delta_name)
        limit = policy.get(limit_name)
        if isinstance(delta, (int, float)) and isinstance(limit, (int, float)) and delta < -float(limit):
            reasons.append(f"相对基线的{label}下降 {abs(delta):.2%}，超过允许值 {float(limit):.2%}")
            failures.append({
                "code": "regression_drop_exceeded",
                "label": f"相对基线的{label}下降 {abs(delta):.2%}，超过允许值 {float(limit):.2%}",
                "metric": delta_name,
                "actual": delta,
                "threshold": -float(limit),
            })

    max_regressions = policy.get("max_new_case_regressions", 0)
    if isinstance(max_regressions, int) and len(comparison["regressions"]) > max_regressions:
        reasons.append(f"新增失败用例 {len(comparison['regressions'])} 个，超过允许值 {max_regressions} 个")
        failures.append({
            "code": "regression_cases_exceeded",
            "label": f"新增失败用例 {len(comparison['regressions'])} 个，超过允许值 {max_regressions} 个",
            "actual": len(comparison["regressions"]),
            "threshold": max_regressions,
            "cases": comparison["regressions"],
        })
