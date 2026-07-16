"""Trace-aware (execution-trajectory) scorer — deterministic, zero-dependency.

A *trace* dimension scores the agent's DAG execution trajectory rather than its
text output. It reads ``ScoreContext.steps`` — a lightweight list of step dicts
({node_id, node_type, status, duration_ms, tokens, error}) the engine threads in
from ``DAGExecutionResult.results`` — and applies one of three deterministic
checks selected by ``dim_cfg["check"]``:

- ``no_error_steps`` (default): full score when no step has status "error",
  else docked by the fraction of error steps.
- ``step_count_range``: full score when the step count falls in
  [min_steps, max_steps], else docked by distance outside the range.
- ``required_nodes``: every node_type/node_id in ``dim_cfg["required_nodes"]``
  must be present and "completed".

With no steps (non-DAG or degraded run) the dimension is *skipped* — execution
failure is already reflected by the case's overall pass/error, so an empty trace
must not be scored as a trace-dimension failure.
"""

from __future__ import annotations

from typing import Any, Dict, List

from app.core.scorers.base import DimensionResult, ScoreContext, register, skipped_result


def _threshold(dim_cfg: Dict[str, Any]) -> float:
    try:
        return max(0.0, min(1.0, float(dim_cfg.get("threshold", 0.6))))
    except (TypeError, ValueError):
        return 0.6


def _result(dim_cfg: Dict[str, Any], score: float, reason: str,
            evidence: Dict[str, Any]) -> DimensionResult:
    threshold = _threshold(dim_cfg)
    score = max(0.0, min(1.0, score))
    return DimensionResult(
        dimension=str(dim_cfg.get("name", dim_cfg.get("check", "trace"))),
        type=str(dim_cfg.get("type", "trace")),
        score=score,
        passed=score >= threshold,
        threshold=threshold,
        weight=float(dim_cfg.get("weight", 0.0) or 0.0),
        required=bool(dim_cfg.get("required", False)),
        reason=reason,
        evidence=evidence,
    )


def _check_no_error_steps(dim_cfg: Dict[str, Any], steps: List[dict]) -> DimensionResult:
    total = len(steps)
    error_steps = [s for s in steps if str(s.get("status", "")).lower() == "error"]
    n_err = len(error_steps)
    score = 1.0 if n_err == 0 else max(0.0, 1.0 - n_err / total)
    reason = "无错误步骤" if n_err == 0 else f"{n_err}/{total} 个步骤出错"
    return _result(dim_cfg, score, reason, {
        "check": "no_error_steps",
        "total_steps": total,
        "error_steps": n_err,
        "error_node_ids": [s.get("node_id") for s in error_steps],
    })


def _check_step_count_range(dim_cfg: Dict[str, Any], steps: List[dict]) -> DimensionResult:
    total = len(steps)
    try:
        min_steps = int(dim_cfg.get("min_steps", 0))
    except (TypeError, ValueError):
        min_steps = 0
    raw_max = dim_cfg.get("max_steps")
    try:
        max_steps = int(raw_max) if raw_max is not None else None
    except (TypeError, ValueError):
        max_steps = None

    if total < min_steps:
        distance = min_steps - total
        denom = max(min_steps, 1)
        score = max(0.0, 1.0 - distance / denom)
        reason = f"步骤数 {total} 少于下限 {min_steps}"
    elif max_steps is not None and total > max_steps:
        distance = total - max_steps
        denom = max(max_steps, 1)
        score = max(0.0, 1.0 - distance / denom)
        reason = f"步骤数 {total} 超过上限 {max_steps}"
    else:
        score = 1.0
        reason = f"步骤数 {total} 落在预期区间"
    return _result(dim_cfg, score, reason, {
        "check": "step_count_range",
        "total_steps": total,
        "min_steps": min_steps,
        "max_steps": max_steps,
    })


def _check_required_nodes(dim_cfg: Dict[str, Any], steps: List[dict]) -> DimensionResult:
    required = dim_cfg.get("required_nodes") or []
    if not isinstance(required, list):
        required = [required]
    required = [str(r) for r in required if r]

    # A node is satisfied when a step whose node_type OR node_id matches is "completed".
    completed_keys = set()
    for s in steps:
        if str(s.get("status", "")).lower() == "completed":
            if s.get("node_type"):
                completed_keys.add(str(s.get("node_type")))
            if s.get("node_id"):
                completed_keys.add(str(s.get("node_id")))

    missing = [r for r in required if r not in completed_keys]
    if not required:
        score, reason = 1.0, "未声明必经节点"
    elif not missing:
        score, reason = 1.0, "所有必经节点均已完成"
    else:
        score = max(0.0, 1.0 - len(missing) / len(required))
        reason = f"缺失/未完成必经节点：{', '.join(missing)}"
    return _result(dim_cfg, score, reason, {
        "check": "required_nodes",
        "required": required,
        "missing": missing,
    })


_CHECKS = {
    "no_error_steps": _check_no_error_steps,
    "step_count_range": _check_step_count_range,
    "required_nodes": _check_required_nodes,
}


async def trace_scorer(dim_cfg: Dict[str, Any], ctx: ScoreContext) -> DimensionResult:
    """Score a trace dimension over ``ctx.steps``; skip when no trace is present."""
    steps = list(ctx.steps or [])
    if not steps:
        return skipped_result(dim_cfg, "无执行轨迹，跳过 trace 维度")

    check = str(dim_cfg.get("check", "no_error_steps"))
    fn = _CHECKS.get(check)
    if fn is None:
        return skipped_result(
            dim_cfg,
            f"未知的 trace 检查类型 '{check}'，跳过该维度",
            evidence={"skipped_reason": "unknown trace check", "check": check},
        )
    return fn(dim_cfg, steps)


register("trace", trace_scorer)
