"""Langfuse observability — traces, spans, and LLM call instrumentation."""

import logging
import time
import functools
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any, Optional

from app.core.config import LANGFUSE_SECRET_KEY, LANGFUSE_PUBLIC_KEY, LANGFUSE_BASE_URL


_log = logging.getLogger(__name__)

_langfuse_client = None

# Set True once any span/observation export to Langfuse fails (401, connection
# error, ...). langfuse_health() factors this in so the monitoring overview can
# distinguish "keys configured" from "exports actually succeed" — a placeholder
# secret key makes exports 401 and the trace is never persisted, which is why a
# /trace/{id} link 404s. Surfacing this turns a mystery 404 into a credential
# attribution. Never stores key values.
_langfuse_export_failed = False


def _note_export_failure(exc: Exception) -> None:
    """Record + log a Langfuse export failure instead of swallowing it.

    Logs a WARNING with the exception type/status (never a key value) and trips
    the process-level flag read by ``langfuse_health``. Best-effort: must not
    raise from a finally/except path.
    """
    global _langfuse_export_failed
    _langfuse_export_failed = True
    try:
        _log.warning(
            "Langfuse export failed (%s: %s); trace/span not persisted — check "
            "LANGFUSE_SECRET_KEY / LANGFUSE_PUBLIC_KEY (a 401 here is why trace "
            "links 404). No key value is logged.",
            type(exc).__name__,
            str(exc)[:200],
        )
    except Exception:
        pass

# Holds the active node span (a Langfuse observation) for the currently
# executing node. The LLM adapter reads this to attach a child `generation`
# observation under the right node, without nodes needing to import Langfuse.
# contextvars are copied per asyncio task, so parallel nodes don't clobber it.
_active_node_span: ContextVar[Any | None] = ContextVar("_active_node_span", default=None)

# Per-run accumulator of (input_tokens, output_tokens) tuples. The DAGRunner
# installs a fresh list before executing nodes; LLM call sites append their
# usage via record_generation_usage. Because contextvars copy the binding (not
# the list) into child asyncio tasks, appends remain visible to the runner.
_usage_accumulator: ContextVar[Optional[list]] = ContextVar("_usage_accumulator", default=None)




def _get_langfuse():
    global _langfuse_client
    if _langfuse_client is not None:
        return _langfuse_client
    if LANGFUSE_SECRET_KEY and LANGFUSE_PUBLIC_KEY:
        try:
            from langfuse import Langfuse
            _langfuse_client = Langfuse(
                secret_key=LANGFUSE_SECRET_KEY,
                public_key=LANGFUSE_PUBLIC_KEY,
                base_url=LANGFUSE_BASE_URL,
            )
            return _langfuse_client
        except Exception:
            _langfuse_client = False
    else:
        _langfuse_client = False
    return None


def _lf_enabled() -> bool:
    return _get_langfuse() is not None


@contextmanager
def trace_span(name: str, agent_id: int, metadata: Optional[dict] = None):
    """Context manager that creates a Langfuse observation span for a node operation."""
    lf = _get_langfuse()
    t_start = time.time()
    span = None

    try:
        if lf:
            trace_id = lf.create_trace_id()
            span = lf.start_observation(
                trace_context={"trace_id": trace_id},
                name=f"agent-{agent_id}-{name}",
                input=metadata or {},
            )
        yield span
    except Exception:
        raise
    finally:
        elapsed = round((time.time() - t_start) * 1000)
        if span is not None and lf:
            try:
                span.update(output={"elapsed_ms": elapsed})
                span.end()
                lf.flush()
            except Exception as exc:
                _note_export_failure(exc)


def trace_node(name: str):
    """Decorator: wraps an async function in a Langfuse trace+span."""
    def deco(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            agent_id = kwargs.get("agent_id") or (args[0] if args else 0)
            with trace_span(name, agent_id, metadata=kwargs.get("metadata")):
                return await func(*args, **kwargs)
        return wrapper
    return deco


def create_trace_url(trace_id: str) -> str:
    """Return a Langfuse UI link for the given trace_id.

    The self-hosted Langfuse (v3.x) serves traces at ``{base}/trace/{id}``; that
    path is correct. A 404 when opening the link means the *trace itself* isn't
    in Langfuse (it was never flushed, or lives under a different project than
    the one the viewer is logged into), not that the URL shape is wrong.
    """
    base = LANGFUSE_BASE_URL.rstrip("/")
    return f"{base}/trace/{trace_id}"


def set_active_span(span: Any | None):
    """Set the active node span context var. Returns the token to reset with."""
    return _active_node_span.set(span)


def begin_usage_accumulation() -> list:
    """Install a fresh per-run usage accumulator and return the underlying list."""
    acc: list = []
    _usage_accumulator.set(acc)
    return acc


def record_generation_usage(input_tokens: int, output_tokens: int) -> None:
    """Append one LLM call's token usage to the active run accumulator (if any)."""
    acc = _usage_accumulator.get()
    if acc is not None:
        acc.append((int(input_tokens or 0), int(output_tokens or 0)))


def reset_active_span(token) -> None:
    """Reset the active node span context var using a token from set_active_span."""
    try:
        _active_node_span.reset(token)
    except Exception:
        pass


@contextmanager
def with_generation(name: str, model: str, provider: str, **kwargs):
    """Open a Langfuse `generation` observation around an LLM call.

    The observation is created as a child of the active node span (set via
    ``set_active_span``) when one is present, otherwise directly under the
    current trace context. Degrades to a no-op (yields ``None``) when Langfuse
    is unconfigured. The caller updates the yielded observation with output /
    usage and the context manager ends + flushes it.
    """
    lf = _get_langfuse()
    if not lf:
        yield None
        return

    gen = None
    parent = _active_node_span.get()
    metadata = {"provider": provider}
    if "metadata" in kwargs and isinstance(kwargs["metadata"], dict):
        metadata.update(kwargs.pop("metadata"))
    try:
        if parent is not None:
            gen = parent.start_observation(
                name=name, as_type="generation", model=model,
                input=kwargs.get("input"), metadata=metadata,
            )
        else:
            gen = lf.start_observation(
                trace_context={"trace_id": lf.create_trace_id()},
                name=name, as_type="generation", model=model,
                input=kwargs.get("input"), metadata=metadata,
            )
    except Exception:
        gen = None

    try:
        yield gen
    finally:
        if gen is not None:
            try:
                gen.end()
                lf.flush()
            except Exception as exc:
                _note_export_failure(exc)


def health_check() -> tuple[bool, str]:
    """Probe Langfuse connectivity. Returns (ok, detail).

    detail is the configured host on success, or one of ``keys_missing`` /
    ``auth_failed`` / ``unreachable`` on failure.
    """
    if not (LANGFUSE_SECRET_KEY and LANGFUSE_PUBLIC_KEY):
        return False, "keys_missing"
    lf = _get_langfuse()
    if not lf:
        return False, "keys_missing"
    try:
        ok = lf.auth_check()
        if ok:
            return True, LANGFUSE_BASE_URL
        return False, "auth_failed"
    except Exception:
        return False, "unreachable"


def log_startup_status() -> None:
    """Log a single line at startup indicating Langfuse observability state."""
    ok, detail = health_check()
    if ok:
        _log.info("langfuse_observability=on host=%s", detail)
    else:
        _log.info("langfuse_observability=off reason=%s", detail)


def record_run_summary(
    agent_id: int,
    trace_id: str = "",
    status: str = "completed",
    total_duration_ms: int = 0,
    token_input: int = 0,
    token_output: int = 0,
    node_count: int = 0,
    error: str = "",
    dag_version: int = 0,
) -> None:
    """Insert one AgentRunSummary row. Best-effort: logs but never raises."""
    try:
        from sqlmodel import Session
        from app.core.database import engine
        from app.models.db import AgentRunSummary

        row = AgentRunSummary(
            agent_id=agent_id,
            dag_version=dag_version,
            trace_id=trace_id or "",
            status=status,
            total_duration_ms=int(total_duration_ms or 0),
            token_input=int(token_input or 0),
            token_output=int(token_output or 0),
            node_count=int(node_count or 0),
            error=(error or "")[:2000],
        )
        with Session(engine) as session:
            session.add(row)
            session.commit()
    except Exception as exc:
        _log.warning("record_run_summary failed: %s: %s", type(exc).__name__, exc)


# ─── AgentRunSummary aggregation (shared by monitoring overview + release gate) ──
#
# Single source of truth so the monitoring overview and the release gate compute
# request count / latency percentiles / token / error rate with one identical
# caliber. A run counts as an error when its status is not "completed" OR it
# carries a non-empty error message.


def _percentile(sorted_vals: list, pct: float) -> float:
    """Nearest-rank percentile over an ascending list (matches release gate)."""
    if not sorted_vals:
        return 0.0
    idx = int(len(sorted_vals) * pct)
    return float(sorted_vals[min(idx, len(sorted_vals) - 1)])


def aggregate_run_metrics(rows: list) -> dict:
    """Aggregate AgentRunSummary rows into runtime metrics.

    Pure computation over the given rows — the caller decides which rows (a
    recent-N window for the gate, a time window for monitoring). Returns zeros
    for an empty input rather than raising. ``error_rate`` counts any row whose
    status is not ``completed`` or that carries a non-empty error.
    """
    total = len(rows)
    if total == 0:
        return {
            "run_count": 0,
            "error_rate": 0.0,
            "latency_p50_ms": 0.0,
            "latency_p95_ms": 0.0,
            "token_consumption": 0,
            "avg_token": 0.0,
        }
    errors = sum(
        1 for r in rows
        if (getattr(r, "status", "") != "completed") or (getattr(r, "error", "") or "").strip()
    )
    durations = sorted(float(getattr(r, "total_duration_ms", 0) or 0) for r in rows)
    tokens = [int(getattr(r, "token_input", 0) or 0) + int(getattr(r, "token_output", 0) or 0) for r in rows]
    return {
        "run_count": total,
        "error_rate": round(errors / total, 4),
        "latency_p50_ms": _percentile(durations, 0.5),
        "latency_p95_ms": _percentile(durations, 0.95),
        "token_consumption": sum(tokens),
        "avg_token": round(sum(tokens) / total, 2),
    }


def recent_run_summaries(agent_id: int, window: int, session) -> list:
    """The most recent ``window`` AgentRunSummary rows for an agent (newest first)."""
    from sqlmodel import select, desc
    from app.models.db import AgentRunSummary

    return session.exec(
        select(AgentRunSummary)
        .where(AgentRunSummary.agent_id == agent_id)
        .order_by(desc(AgentRunSummary.created_at))
        .limit(window)
    ).all()


def agent_monitoring_overview(agent_id: int, session) -> dict:
    """Real monitoring-overview metrics for an agent from local AgentRunSummary.

    request_count_24h/7d/30d are counted by ``created_at`` time windows; the
    latency percentiles / token / error_rate are aggregated over the 30d window
    (same caliber as the release gate via :func:`aggregate_run_metrics`). No
    Langfuse dependency. Returns zeros when there is no data.
    """
    from datetime import timedelta
    from sqlmodel import select, desc
    from app.models.db import AgentRunSummary

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    cutoff_30d = now - timedelta(days=30)
    cutoff_7d = now - timedelta(days=7)
    cutoff_24h = now - timedelta(hours=24)

    rows = session.exec(
        select(AgentRunSummary)
        .where(AgentRunSummary.agent_id == agent_id)
        .where(AgentRunSummary.created_at >= cutoff_30d)
        .order_by(desc(AgentRunSummary.created_at))
    ).all()

    request_count_24h = sum(1 for r in rows if r.created_at and r.created_at >= cutoff_24h)
    request_count_7d = sum(1 for r in rows if r.created_at and r.created_at >= cutoff_7d)
    request_count_30d = len(rows)

    metrics = aggregate_run_metrics(rows)
    return {
        "request_count_24h": request_count_24h,
        "request_count_7d": request_count_7d,
        "request_count_30d": request_count_30d,
        "p50_latency_ms": metrics["latency_p50_ms"],
        "p95_latency_ms": metrics["latency_p95_ms"],
        "token_consumption": metrics["token_consumption"],
        "error_rate": metrics["error_rate"],
    }


def langfuse_health() -> dict:
    """Return Langfuse health/connectivity for the monitoring overview.

    - ``langfuse_enabled``: both keys configured (the client *can* be built).
    - ``langfuse_auth_ok``: keys valid + reachable AND no export failure has been
      observed this process. A placeholder secret makes ``health_check`` /
      exports fail, so this stays False — distinguishing "link is buildable"
      from "the trace was actually persisted".
    - ``langfuse_base_url``: the configured base_url ONLY. Never contains a key.
    """
    enabled = bool(LANGFUSE_SECRET_KEY and LANGFUSE_PUBLIC_KEY)
    auth_ok = False
    if enabled:
        try:
            auth_ok, _ = health_check()
        except Exception:
            auth_ok = False
        # An observed export failure (e.g. 401 from a placeholder key) overrides
        # an optimistic health_check — exports are the real signal that traces land.
        if _langfuse_export_failed:
            auth_ok = False
    return {
        "langfuse_enabled": enabled,
        "langfuse_auth_ok": auth_ok,
        "langfuse_base_url": LANGFUSE_BASE_URL,
    }


def record_evaluation_scores(case_ctx: dict, dimension_results: list) -> list[str]:
    """Write a case's dimension scores + an overall back to Langfuse as scores.

    Best-effort evidence layer (Phase 2). For each *non-skipped* dimension plus
    one ``overall`` score, writes a numeric Langfuse score attached to the case's
    ``trace_id`` and carrying run-context metadata (agent_id / suite_id /
    case_id / run_id / dag_version / prompt_version / model / dimension /
    source). ``source`` is the dimension's scorer type so Langfuse can tell
    deterministic / judge / ragas evidence apart; the overall uses
    ``source="overall"``.

    ``case_ctx`` carries the run context plus the ``overall`` value (and the
    case's ``trace_id``). ``dimension_results`` is the list of per-dimension
    dicts (``DimensionResult.to_dict()`` shape).

    Returns the list of score ids actually written. When Langfuse is
    unconfigured/unreachable returns ``[]``. NEVER raises — any failure is
    logged at warning level and the ids written so far are returned.
    """
    lf = _get_langfuse()
    if not lf:
        return []

    import uuid

    trace_id = str(case_ctx.get("trace_id") or "")
    base_meta = {
        "agent_id": case_ctx.get("agent_id"),
        "suite_id": case_ctx.get("suite_id"),
        "case_id": case_ctx.get("case_id"),
        "run_id": case_ctx.get("run_id"),
        "dag_version": case_ctx.get("dag_version"),
        "prompt_version": case_ctx.get("prompt_version"),
        "model": case_ctx.get("model") or "",
    }

    written: list[str] = []

    def _write(name: str, value: float, dimension: str, source: str) -> None:
        score_id = uuid.uuid4().hex
        metadata = dict(base_meta)
        metadata["dimension"] = dimension
        metadata["source"] = source
        try:
            kwargs = dict(
                name=name,
                value=float(value),
                data_type="NUMERIC",
                score_id=score_id,
                metadata=metadata,
            )
            if trace_id:
                kwargs["trace_id"] = trace_id
            lf.create_score(**kwargs)
            written.append(score_id)
        except Exception as exc:
            _log.warning(
                "record_evaluation_scores: score '%s' failed: %s: %s",
                name, type(exc).__name__, exc,
            )

    try:
        for dim in dimension_results or []:
            if not isinstance(dim, dict) or dim.get("skipped"):
                continue
            dim_name = str(dim.get("dimension", dim.get("name", ""))) or "dimension"
            try:
                value = float(dim.get("score", 0.0))
            except (TypeError, ValueError):
                value = 0.0
            _write(dim_name, value, dimension=dim_name,
                   source=str(dim.get("type", "") or ""))

        if "overall" in case_ctx and case_ctx["overall"] is not None:
            try:
                overall_val = float(case_ctx["overall"])
            except (TypeError, ValueError):
                overall_val = None
            if overall_val is not None:
                _write("overall", overall_val, dimension="overall", source="overall")

        try:
            lf.flush()
        except Exception as exc:
            _note_export_failure(exc)
    except Exception as exc:  # belt-and-suspenders: never bubble to the engine
        _log.warning(
            "record_evaluation_scores failed: %s: %s", type(exc).__name__, exc,
        )

    return written


class LangfuseCallbackHandler:
    """Callback handler that auto-instruments LLM calls via Langfuse."""

    def __init__(self, trace_id: str, agent_id: int):
        self.trace_id = trace_id
        self.agent_id = agent_id

    def on_llm_start(self, prompts, **kwargs):
        lf = _get_langfuse()
        if not lf or not self.trace_id:
            return
        try:
            span = lf.span(
                trace_id=self.trace_id,
                name="llm-call",
                input={"prompts": prompts},
            )
            self._current_span = span
        except Exception:
            self._current_span = None

    def on_llm_end(self, response, **kwargs):
        if hasattr(self, "_current_span") and self._current_span:
            try:
                self._current_span.end(output={"response": str(response)[:500]})
            except Exception:
                pass
            self._current_span = None