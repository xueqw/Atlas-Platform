"""Scorer registry — the pluggable core of dimension-driven evaluation.

A *dimension* is one scored aspect of a case (e.g. goal_completion, format
compliance, answer relevancy). Each dimension declares a ``type`` that maps,
via this registry, to a :class:`Scorer` which turns the agent output + case
context into a :class:`DimensionResult`.

Adding a new dimension type = registering one more scorer; the aggregation in
``evaluation_engine`` never changes. All scorers share one return shape so the
engine and frontend stay decoupled from any individual scorer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, Optional, Protocol


@dataclass
class ScoreContext:
    """Everything a scorer might need about one case run.

    Carries the agent's output plus the case's declared inputs. Scorers read
    only what they need (keyword scorer uses ``output``; ragas uses
    ``reference_output`` + ``contexts``), so new scorers can lean on fields the
    older ones ignore.
    """
    user_input: str = ""
    output: str = ""
    reference_output: str = ""
    contexts: list = field(default_factory=list)
    constraints: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)
    latency_ms: float = 0.0
    token_input: int = 0
    token_output: int = 0
    # Execution trace (Phase 4, trace-aware scoring): a lightweight list of step
    # dicts ({node_id, node_type, status, duration_ms, tokens, error}) from the
    # run's DAGExecutionResult.results, plus the run trace_id. Empty for non-DAG
    # or degraded runs; trace scorers skip when steps is empty.
    steps: list = field(default_factory=list)
    trace_id: str = ""


@dataclass
class DimensionResult:
    """Unified result for one scored dimension.

    ``score`` is normalized to [0, 1]. ``skipped`` marks a dimension that could
    not be evaluated (e.g. ragas unavailable): it is excluded from the weighted
    overall and never fails the case, but is still persisted with its reason.
    """
    dimension: str
    score: float = 0.0
    passed: bool = False
    threshold: float = 0.0
    weight: float = 0.0
    reason: str = ""
    evidence: Dict[str, Any] = field(default_factory=dict)
    required: bool = False
    type: str = ""
    skipped: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "dimension": self.dimension,
            "type": self.type,
            "score": round(float(self.score), 4),
            "passed": bool(self.passed),
            "threshold": round(float(self.threshold), 4),
            "weight": round(float(self.weight), 4),
            "required": bool(self.required),
            "reason": self.reason,
            "evidence": self.evidence,
            "skipped": bool(self.skipped),
        }


class Scorer(Protocol):
    """A scorer turns a dimension config + case context into a result.

    Scorers are awaited uniformly by the engine; deterministic scorers simply
    return without awaiting anything.
    """

    async def __call__(self, dim_cfg: Dict[str, Any], ctx: ScoreContext) -> DimensionResult:
        ...


_REGISTRY: Dict[str, Scorer] = {}


def register(dim_type: str, scorer: Scorer) -> None:
    """Register ``scorer`` under a dimension ``type`` (last registration wins)."""
    _REGISTRY[dim_type] = scorer


def get_scorer(dim_type: str) -> Optional[Scorer]:
    """Return the scorer for ``dim_type`` or ``None`` when unregistered."""
    return _REGISTRY.get(dim_type)


def registered_types() -> list:
    """All currently registered dimension types (for validation/diagnostics)."""
    return sorted(_REGISTRY.keys())


def skipped_result(dim_cfg: Dict[str, Any], reason: str,
                   evidence: Optional[Dict[str, Any]] = None) -> DimensionResult:
    """Build a 'skipped' DimensionResult from a raw dimension config.

    Used for degradation paths (ragas unavailable) and config errors so the
    dimension is recorded with its reason but excluded from scoring/pass logic.
    """
    payload = dict(evidence or {"skipped_reason": reason})
    if dim_cfg.get("gate_on_skip"):
        payload["gate_on_skip"] = True
    return DimensionResult(
        dimension=str(dim_cfg.get("name", dim_cfg.get("type", "unknown"))),
        type=str(dim_cfg.get("type", "")),
        score=0.0,
        passed=False,
        threshold=float(dim_cfg.get("threshold", 0.0) or 0.0),
        weight=float(dim_cfg.get("weight", 0.0) or 0.0),
        required=bool(dim_cfg.get("required", False)),
        reason=reason,
        evidence=payload,
        skipped=True,
    )
