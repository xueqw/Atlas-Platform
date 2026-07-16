"""Backend tests for Phase 2 (Langfuse score evidence + monitoring真值化).

Covers two specs:
- ``runtime-monitoring-metrics``: monitoring overview aggregates real metrics
  from local AgentRunSummary rows (request_count time windows, p50/p95 latency,
  token, error_rate) and stays同口径 with the release gate.
- ``langfuse-observability``: ``record_evaluation_scores`` is best-effort —
  returns [] without raising when Langfuse is unconfigured, and never writes a
  score for a skipped dimension.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.api import agents as agents_api
from app.api import monitoring as monitoring_api
from app.core.database import engine
from app.core.observability import (
    aggregate_run_metrics,
    agent_monitoring_overview,
    record_evaluation_scores,
    langfuse_health,
)
from app.core.release_gate import _observability_metrics
from app.models.db import Agent, AgentRunSummary
from app.models.db import _utcnow


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(agents_api.router, prefix="/api")
    app.include_router(monitoring_api.router, prefix="/api")
    return TestClient(app)


def _new_agent() -> int:
    with Session(engine) as session:
        agent = Agent(name="mon-test", description="")
        session.add(agent)
        session.commit()
        session.refresh(agent)
        return agent.id


def _add_runs(agent_id: int, specs: list[dict]) -> None:
    """Insert AgentRunSummary rows. Each spec may set status/error/duration/
    tokens and an ``age`` timedelta subtracted from now for created_at."""
    with Session(engine) as session:
        for s in specs:
            row = AgentRunSummary(
                agent_id=agent_id,
                status=s.get("status", "completed"),
                error=s.get("error", ""),
                total_duration_ms=s.get("duration_ms", 100),
                token_input=s.get("token_input", 5),
                token_output=s.get("token_output", 5),
                node_count=s.get("node_count", 1),
            )
            age = s.get("age")
            if age is not None:
                row.created_at = _utcnow() - age
            session.add(row)
        session.commit()


# ─── aggregate_run_metrics: pure aggregation ────────────────────────────────────


def test_aggregate_run_metrics_empty_is_zeros():
    m = aggregate_run_metrics([])
    assert m["run_count"] == 0
    assert m["error_rate"] == 0.0
    assert m["latency_p50_ms"] == 0.0
    assert m["latency_p95_ms"] == 0.0
    assert m["token_consumption"] == 0
    assert m["avg_token"] == 0.0


def test_aggregate_run_metrics_real_values():
    agent_id = _new_agent()
    # 10 rows, 2 errors (one via status, one via non-empty error message).
    specs = [{"duration_ms": 100, "token_input": 10, "token_output": 10} for _ in range(8)]
    specs.append({"status": "error", "duration_ms": 500, "token_input": 0, "token_output": 0})
    specs.append({"status": "completed", "error": "boom", "duration_ms": 900, "token_input": 0, "token_output": 0})
    _add_runs(agent_id, specs)

    with Session(engine) as session:
        rows = session.exec(
            select(AgentRunSummary).where(AgentRunSummary.agent_id == agent_id)
        ).all()
    m = aggregate_run_metrics(rows)
    assert m["run_count"] == 10
    assert m["error_rate"] == 0.2  # 2/10 — status!=completed OR error set
    assert m["token_consumption"] == 8 * 20  # the 8 healthy rows × 20; error rows carried 0
    assert m["latency_p95_ms"] >= m["latency_p50_ms"]


# ─── time-window request counts ─────────────────────────────────────────────────


def test_monitoring_overview_time_windows():
    agent_id = _new_agent()
    _add_runs(agent_id, [
        {"age": timedelta(hours=1)},     # in 24h, 7d, 30d
        {"age": timedelta(days=3)},      # in 7d, 30d
        {"age": timedelta(days=10)},     # in 30d only
        {"age": timedelta(days=40)},     # outside 30d → excluded everywhere
    ])
    with Session(engine) as session:
        ov = agent_monitoring_overview(agent_id, session)
    assert ov["request_count_24h"] == 1
    assert ov["request_count_7d"] == 2
    assert ov["request_count_30d"] == 3  # 40d-old row excluded


def test_monitoring_overview_no_data_is_zeros():
    agent_id = _new_agent()
    with Session(engine) as session:
        ov = agent_monitoring_overview(agent_id, session)
    assert ov["request_count_24h"] == 0
    assert ov["request_count_30d"] == 0
    assert ov["p95_latency_ms"] == 0.0
    assert ov["error_rate"] == 0.0
    assert ov["token_consumption"] == 0


# ─── endpoint: real metrics + langfuse health, 200 with no data ─────────────────


def test_get_monitoring_endpoint_real_and_health(client):
    agent_id = _new_agent()
    _add_runs(agent_id, [
        {"duration_ms": 100, "token_input": 10, "token_output": 10},
        {"status": "error", "duration_ms": 200, "token_input": 0, "token_output": 0},
    ])
    res = client.get(f"/api/agents/{agent_id}/monitoring")
    assert res.status_code == 200
    body = res.json()
    assert body["request_count_30d"] == 2
    assert body["error_rate"] == 0.5
    assert body["token_consumption"] == 20
    # Langfuse health fields present; base_url must never carry a key.
    assert "langfuse_enabled" in body
    assert "langfuse_auth_ok" in body
    assert "secret" not in body["langfuse_base_url"].lower()


def test_monitoring_exposes_credential_status_without_keys(client, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-1234567890")  # placeholder
    monkeypatch.setenv("DEEPSEEK_API_KEY", "")             # missing
    agent_id = _new_agent()
    res = client.get(f"/api/agents/{agent_id}/monitoring")
    assert res.status_code == 200
    body = res.json()
    assert "credential_status" in body
    by_name = {c["name"]: c["status"] for c in body["credential_status"]}
    # All known providers + langfuse keys are reported.
    for p in ("openai", "glm", "anthropic", "deepseek", "custom"):
        assert p in by_name
    assert by_name["openai"] == "placeholder"
    assert by_name["deepseek"] == "missing"
    # No key value leaks anywhere in the response.
    assert "sk-1234567890" not in res.text
    for c in body["credential_status"]:
        assert set(c.keys()) == {"name", "status"}


def test_get_monitoring_endpoint_no_data_200(client):
    agent_id = _new_agent()
    res = client.get(f"/api/agents/{agent_id}/monitoring")
    assert res.status_code == 200
    body = res.json()
    assert body["request_count_24h"] == 0
    assert body["request_count_30d"] == 0
    assert body["error_rate"] == 0.0


# ─── same caliber: monitoring vs release gate ───────────────────────────────────


def test_monitoring_and_release_gate_same_caliber():
    agent_id = _new_agent()
    # Mixed errors + latencies; both paths must agree on error_rate / p95.
    specs = [{"duration_ms": d} for d in (50, 60, 70, 80, 90, 100, 110, 120)]
    specs.append({"status": "error", "duration_ms": 130})
    specs.append({"status": "error", "duration_ms": 140})
    _add_runs(agent_id, specs)

    with Session(engine) as session:
        gate = _observability_metrics(agent_id, 20, session)
        ov = agent_monitoring_overview(agent_id, session)
    assert gate is not None
    assert gate["error_rate"] == ov["error_rate"]
    assert gate["latency_p95_ms"] == ov["p95_latency_ms"]


# ─── langfuse_health shape / no secret leak ─────────────────────────────────────


def test_langfuse_health_shape_no_secret():
    health = langfuse_health()
    assert set(health) == {"langfuse_enabled", "langfuse_auth_ok", "langfuse_base_url"}
    assert isinstance(health["langfuse_enabled"], bool)
    # base_url is a host string only; the secret/public keys never appear.
    from app.core import config as cfg
    if cfg.LANGFUSE_SECRET_KEY:
        assert cfg.LANGFUSE_SECRET_KEY not in health["langfuse_base_url"]
    if cfg.LANGFUSE_PUBLIC_KEY:
        assert cfg.LANGFUSE_PUBLIC_KEY not in health["langfuse_base_url"]


# ─── record_evaluation_scores: best-effort degradation ──────────────────────────


def test_record_scores_returns_empty_when_langfuse_unconfigured(monkeypatch):
    # Force the langfuse client to be unavailable regardless of env config.
    import app.core.observability as obs
    monkeypatch.setattr(obs, "_get_langfuse", lambda: None)

    ids = obs.record_evaluation_scores(
        {"agent_id": 1, "suite_id": 2, "case_id": 3, "run_id": 4,
         "dag_version": 1, "prompt_version": 1, "model": "m",
         "trace_id": "t", "overall": 0.8},
        [{"dimension": "goal", "type": "judge", "score": 0.9, "skipped": False}],
    )
    assert ids == []


def test_record_scores_skips_skipped_dimensions(monkeypatch):
    # Capture create_score calls via a fake client; the skipped dim is excluded.
    import app.core.observability as obs

    calls = []

    class _FakeLF:
        def create_score(self, **kwargs):
            calls.append(kwargs)

        def flush(self):
            pass

    monkeypatch.setattr(obs, "_get_langfuse", lambda: _FakeLF())

    ids = obs.record_evaluation_scores(
        {"agent_id": 1, "suite_id": 2, "case_id": 3, "run_id": 4,
         "dag_version": 1, "prompt_version": 1, "model": "m",
         "trace_id": "trace-xyz", "overall": 0.75},
        [
            {"dimension": "goal", "type": "judge", "score": 0.9, "skipped": False},
            {"dimension": "rag", "type": "ragas", "score": 0.0, "skipped": True},
        ],
    )
    # one dimension + overall written; the skipped ragas dim never written
    assert len(ids) == 2
    written_names = [c["name"] for c in calls]
    assert "goal" in written_names
    assert "overall" in written_names
    assert "rag" not in written_names
    # metadata carries run context + source; trace_id is attached
    goal_call = next(c for c in calls if c["name"] == "goal")
    assert goal_call["metadata"]["source"] == "judge"
    assert goal_call["metadata"]["agent_id"] == 1
    assert goal_call["trace_id"] == "trace-xyz"
    assert goal_call["data_type"] == "NUMERIC"
    overall_call = next(c for c in calls if c["name"] == "overall")
    assert overall_call["metadata"]["source"] == "overall"


def test_record_scores_never_raises_on_client_error(monkeypatch):
    # A create_score that always raises must be swallowed; returns [].
    import app.core.observability as obs

    class _BadLF:
        def create_score(self, **kwargs):
            raise RuntimeError("langfuse down")

        def flush(self):
            pass

    monkeypatch.setattr(obs, "_get_langfuse", lambda: _BadLF())

    ids = obs.record_evaluation_scores(
        {"agent_id": 1, "overall": 0.5, "trace_id": "t"},
        [{"dimension": "goal", "type": "judge", "score": 0.9, "skipped": False}],
    )
    assert ids == []


# ─── run-level degradation: run completes, langfuse_score_ids empty ─────────────


@pytest.mark.asyncio
async def test_run_suite_completes_with_empty_score_ids_when_langfuse_off(monkeypatch):
    """When Langfuse is unconfigured the suite run still completes and every
    case result persists with langfuse_score_ids == "[]"."""
    import json as _json

    from app.core import evaluation_engine as ee
    import app.core.observability as obs
    from app.core.dag_executor import DAGExecutionResult
    from app.models.db import (
        Agent, AgentTestCase, DAGGraph, EvaluationCaseResult, EvaluationSuite,
    )

    # Force Langfuse unavailable for the writeback path.
    monkeypatch.setattr(obs, "_get_langfuse", lambda: None)

    graph = _json.dumps({
        "nodes": [
            {"id": "i1", "type": "i", "config": {}},
            {"id": "agent1", "type": "agent", "config": {
                "system_prompt": "x", "model_name": "test-model", "provider": "glm"}},
            {"id": "o1", "type": "o", "config": {}},
        ],
        "edges": [
            {"id": "e1", "source": "i1", "target": "agent1"},
            {"id": "e2", "source": "agent1", "target": "o1"},
        ],
    })

    with Session(engine) as session:
        agent = Agent(name="score-degrade")
        session.add(agent)
        session.commit()
        session.refresh(agent)
        session.add(DAGGraph(agent_id=agent.id, graph_json=graph, version=1))
        suite = EvaluationSuite(agent_id=agent.id, name="s")
        session.add(suite)
        session.commit()
        session.refresh(suite)
        session.add(AgentTestCase(
            agent_id=agent.id, suite_id=suite.id, name="c",
            input_message="ping", expected_keywords=_json.dumps(["pong"]),
            is_key=True, sort_order=1,
        ))
        session.commit()
        agent_id, suite_id = agent.id, suite.id

    class _FakeRunner:
        def __init__(self, parser, state_manager=None):
            pass

        async def run(self, user_input, agent_id=0, on_event=None, dag_version=0):
            return DAGExecutionResult(
                final_output="pong", total_duration_ms=10,
                trace_id="", token_input=3, token_output=4,
            )

    monkeypatch.setattr(ee, "DAGRunner", _FakeRunner)

    with Session(engine) as session:
        run = await ee.run_suite(agent_id, suite_id, session)
        rows = session.exec(
            select(EvaluationCaseResult).where(EvaluationCaseResult.run_id == run.id)
        ).all()

    assert len(rows) == 1
    # The run completed and the case persisted with an empty score-id list.
    assert _json.loads(rows[0].langfuse_score_ids) == []
    assert _json.loads(run.summary)["total_cases"] == 1

