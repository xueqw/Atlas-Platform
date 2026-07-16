"""Ragas-backed semantic scorers.

Covers both answer-only metrics (answer_relevancy, answer_correctness) and
RAG-aware metrics that need retrieved contexts (faithfulness, context_precision,
context_recall). Ragas pulls in a heavy LLM/embedding stack and may be absent in
many environments, so it is an *optional* dependency imported lazily inside the
scorer. On any failure — import error, missing inputs, evaluation error — the
dimension is returned as *skipped* with the reason recorded in evidence: it is
excluded from the weighted overall and never fails the case. Ragas augments,
it never gates, the local evaluation.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from app.core.scorers.base import DimensionResult, ScoreContext, register, skipped_result

_log = logging.getLogger(__name__)

# Ragas metric name per dimension type.
_METRIC_BY_TYPE = {
    "ragas": "answer_relevancy",          # default when type is bare "ragas"
    "answer_relevancy": "answer_relevancy",
    "answer_correctness": "answer_correctness",
    "faithfulness": "faithfulness",
    "context_precision": "context_precision",
    "context_recall": "context_recall",
}

# Metrics that require non-empty retrieved contexts to be meaningful.
_RAG_METRICS = {"faithfulness", "context_precision", "context_recall"}
# Metrics that additionally require a ground truth (reference_output).
_NEEDS_GROUND_TRUTH = {"answer_correctness", "context_recall"}

_VALID_METRICS = {
    "answer_relevancy", "answer_correctness",
    "faithfulness", "context_precision", "context_recall",
}


def _resolve_metric_name(dim_cfg: Dict[str, Any]) -> str:
    """Pick which ragas metric to run from the dimension config.

    Honors an explicit ``metric`` field, else maps the dimension ``type``, else
    falls back to answer_relevancy.
    """
    explicit = dim_cfg.get("metric")
    if explicit in _VALID_METRICS:
        return explicit
    return _METRIC_BY_TYPE.get(str(dim_cfg.get("type", "")), "answer_relevancy")


def _compute_ragas_score(metric_name: str, ctx: ScoreContext) -> float:
    """Run a single ragas metric and return its [0,1] score.

    Imports ragas lazily; raises on any failure so the caller degrades. Tries
    the modern single-sample API and tolerates minor version differences.
    """
    from ragas import evaluate  # noqa: F401  (import-time availability check)
    from ragas.metrics import (
        answer_relevancy, answer_correctness,
        faithfulness, context_precision, context_recall,
    )
    from datasets import Dataset

    metric = {
        "answer_relevancy": answer_relevancy,
        "answer_correctness": answer_correctness,
        "faithfulness": faithfulness,
        "context_precision": context_precision,
        "context_recall": context_recall,
    }[metric_name]

    contexts = [str(c) for c in (ctx.contexts or [])]
    # Fall back only for answer-only metrics; RAG metrics are gated upstream on
    # non-empty contexts, so by here ``contexts`` is already populated for them.
    if not contexts:
        contexts = [ctx.reference_output or ctx.output]

    sample = {
        "question": [ctx.user_input],
        "answer": [ctx.output],
        "contexts": [contexts],
        "ground_truth": [ctx.reference_output],
    }
    dataset = Dataset.from_dict(sample)
    result = evaluate(dataset, metrics=[metric])
    df = result.to_pandas()
    value = float(df[metric_name].iloc[0])
    if value != value:  # NaN guard
        raise ValueError("ragas returned NaN")
    return max(0.0, min(1.0, value))


async def ragas_scorer(dim_cfg: Dict[str, Any], ctx: ScoreContext) -> DimensionResult:
    """Score a ragas dimension, degrading to *skipped* on any unavailability.

    Input requirements per metric (missing → skipped with a clear reason, never
    scored against nothing):
    - answer_correctness / context_recall need a reference_output (ground truth).
    - faithfulness / context_precision / context_recall need non-empty contexts.
    """
    try:
        threshold = max(0.0, min(1.0, float(dim_cfg.get("threshold", 0.6))))
    except (TypeError, ValueError):
        threshold = 0.6

    metric_name = _resolve_metric_name(dim_cfg)

    # RAG metrics require retrieved contexts to mean anything.
    if metric_name in _RAG_METRICS and not (ctx.contexts and any(str(c).strip() for c in ctx.contexts)):
        return skipped_result(
            dim_cfg,
            f"{metric_name} 需要非空检索上下文（contexts），但未提供，跳过该维度",
        )

    # Ground-truth-dependent metrics need a reference_output.
    if metric_name in _NEEDS_GROUND_TRUTH and not (ctx.reference_output or "").strip():
        return skipped_result(
            dim_cfg,
            f"{metric_name} 需要 reference_output（ground truth），但未提供，跳过该维度",
        )

    try:
        score = _compute_ragas_score(metric_name, ctx)
    except ImportError as exc:
        return skipped_result(
            dim_cfg,
            f"Ragas 未安装({type(exc).__name__}),跳过该维度",
            evidence={"skipped_reason": "ragas not installed", "metric": metric_name,
                      "error": str(exc)[:300]},
        )
    except Exception as exc:
        _log.warning("ragas scorer failed: %s: %s", type(exc).__name__, exc)
        return skipped_result(
            dim_cfg,
            f"Ragas 调用失败({type(exc).__name__}),跳过该维度",
            evidence={"skipped_reason": "ragas evaluation error", "metric": metric_name,
                      "error": str(exc)[:300]},
        )

    name = str(dim_cfg.get("name", metric_name))
    return DimensionResult(
        dimension=name,
        type=str(dim_cfg.get("type", "ragas")),
        score=score,
        passed=score >= threshold,
        threshold=threshold,
        weight=float(dim_cfg.get("weight", 0.0) or 0.0),
        required=bool(dim_cfg.get("required", False)),
        reason=f"Ragas {metric_name} = {round(score, 4)}",
        evidence={"metric": metric_name, "score": score},
    )


# Register under every supported key so cases can declare the bare "ragas" type
# or a specific metric name as the dimension type.
register("ragas", ragas_scorer)
register("answer_relevancy", ragas_scorer)
register("answer_correctness", ragas_scorer)
register("faithfulness", ragas_scorer)
register("context_precision", ragas_scorer)
register("context_recall", ragas_scorer)
