"""Integration & verification tests for the DAG workbench (spec §24).

These exercise the real routers (TestClient + WebSocket) and the real DAG
engine / Hermes / release-gate / compiler — only the LLM layer is stubbed.
The fake ``run_conversation`` echoes the user input back as the model output,
so keyword assertions are deterministic without a live model.

Covers:
- 24.1 end-to-end: create → save graph → validate → debug node → test chat →
  test cases → evaluate → publish → monitoring
- 24.2 legacy pipeline → DAG conversion yields a valid DAG
- 24.3 Hermes gating: parameter errors and connection errors block save-time
  validation; sub-threshold evaluation blocks publish
- 24.4 runtime monitoring records latency/tokens/errors and surfaces them
- 24.5 template creation → pre-populated graph → configure → publish
"""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, select, desc

from app.core import agentscope_runner as _runner_mod
from app.core.database import engine
from app.api import agents as agents_api
from app.api import dag as dag_api
from app.api import evaluation as eval_api
from app.api import monitoring as monitoring_api
from app.api import dag_templates as templates_api
from app.api import ws as ws_api
from app.models.db import (
    Agent, AgentStatus, AgentRunSummary, DAGGraph, DAGTemplate,
)

# ws.py captured `engine` at import time; repoint at the conftest test engine.
ws_api.engine = engine


# ── Graph fixtures ────────────────────────────────────────────────────────────

# Minimal runnable DAG: I → agent → O. The agent node runs the (stubbed) LLM
# and writes raw_response; O surfaces it as final_output.
def _io_graph() -> str:
    return json.dumps({
        "nodes": [
            {"id": "i1", "type": "i", "config": {}},
            {"id": "a1", "type": "agent", "config": {
                "system_prompt": "你是一个有帮助的助手。" * 4,
                "model_name": "qwen3.6-27b", "provider": "glm",
            }},
            {"id": "o1", "type": "o", "config": {}},
        ],
        "edges": [
            {"id": "e1", "source": "i1", "target": "a1"},
            {"id": "e2", "source": "a1", "target": "o1"},
        ],
    }, ensure_ascii=False)


# ── LLM stub ──────────────────────────────────────────────────────────────────


@pytest.fixture
def stub_llm(monkeypatch):
    """Patch the agentscope runner so the agent node echoes its input.

    AgentNode.run imports create_agent / run_conversation from
    app.core.agentscope_runner at call time, so patching that module is enough.
    The echoed output makes keyword scoring deterministic.
    """
    import types

    def _create_agent(system_prompt, model_name, provider="glm", **kwargs):
        return types.SimpleNamespace(_system_prompt=system_prompt)

    def _run_conversation(agent, user_message, attachments=None):
        async def _gen():
            yield ("token", f"echo: {user_message}")
            yield ("done", "")
        return _gen()

    monkeypatch.setattr(_runner_mod, "create_agent", _create_agent)
    monkeypatch.setattr(_runner_mod, "run_conversation", _run_conversation)
    return _run_conversation


@pytest.fixture
def app_client() -> TestClient:
    """A FastAPI app mounting every DAG-workbench router under /api."""
    app = FastAPI()
    for mod in (agents_api, dag_api, eval_api, monitoring_api, templates_api):
        app.include_router(mod.router, prefix="/api")
    app.include_router(ws_api.router, prefix="/api")
    return TestClient(app)


def _seed_passing_quality_gate(agent_id: int) -> None:
    """Seed the evidence the release gate requires so publish can succeed:
    a passing EvaluationRun + a window of clean AgentRunSummary rows."""
    from app.models.db import EvaluationRun

    good_summary = {
        "total_cases": 4, "passed_cases": 4, "pass_rate": 1.0,
        "key_total": 1, "key_passed": 1, "key_pass_rate": 1.0,
        "avg_score": 1.0, "latency_p95_ms": 50.0,
        "token_input": 5, "token_output": 5,
    }
    with Session(engine) as session:
        session.add(EvaluationRun(
            agent_id=agent_id, suite_id=1,
            summary=json.dumps(good_summary), passed=True,
        ))
        for _ in range(10):
            session.add(AgentRunSummary(
                agent_id=agent_id, status="completed",
                total_duration_ms=50, token_input=5, token_output=5,
                node_count=2,
            ))
        session.commit()


# ── 24.1 End-to-end happy path ─────────────────────────────────────────────────


def test_24_1_end_to_end_create_to_monitoring(app_client, stub_llm):
    c = app_client

    # create agent
    res = c.post("/api/agents", json={"name": "新建智能体", "description": "e2e"})
    assert res.status_code == 200
    agent_id = res.json()["id"]

    # save DAG graph (the "drag nodes + configure + save" outcome)
    res = c.post(f"/api/agents/{agent_id}/dag-graph",
                 json={"graph_json": _io_graph(), "state_schema": "{}"})
    assert res.status_code == 200
    assert res.json()["version"] == 1

    # graph loads back
    res = c.get(f"/api/agents/{agent_id}/dag-graph")
    assert res.status_code == 200
    assert json.loads(res.json()["graph_json"])["nodes"][1]["type"] == "agent"

    # validate: graph is structurally valid
    res = c.post(f"/api/agents/{agent_id}/dag-graph/validate",
                 json={"graph_json": _io_graph(), "state_schema": "{}"})
    assert res.status_code == 200 and res.json()["valid"] is True

    # debug a single node in isolation
    res = c.post(f"/api/agents/{agent_id}/debug-node", json={
        "node_id": "a1", "node_type": "agent",
        "config": {"system_prompt": "x" * 200, "model_name": "qwen3.6-27b", "provider": "glm"},
        "inputs": {"user_query": "你好"},
    })
    assert res.status_code == 200
    assert res.json()["output"]["raw_response"] == "echo: 你好"

    # test chat over the DAG via WebSocket — streams node lifecycle + token
    with c.websocket_connect(f"/api/agent/{agent_id}") as wsx:
        wsx.send_json({"type": "message", "content": "ping"})
        events = []
        while True:
            evt = wsx.receive_json()
            events.append(evt)
            if evt["type"] == "done":
                break
    types_seen = [e["type"] for e in events]
    assert "node_start" in types_seen and "node_complete" in types_seen
    assert any(e["type"] == "token" and "echo: ping" in e["content"] for e in events)

    # add a custom test case
    res = c.post(f"/api/agents/{agent_id}/test-cases", json={
        "name": "greet", "input_message": "ping",
        "expected_keywords": json.dumps(["echo"]), "is_key": True,
    })
    assert res.status_code == 200

    # evaluate the build — the echoed output contains "echo" → passes
    res = c.post(f"/api/agents/{agent_id}/evaluate", json={
        "test_cases": [{"name": "greet", "input": "ping", "expected_keywords": ["echo"]}],
    })
    assert res.status_code == 200 and res.json()["passed"] is True

    # publish is blocked until the quality gate has evidence
    assert c.post(f"/api/agents/{agent_id}/publish").status_code == 422

    # seed gate evidence, then publish succeeds
    _seed_passing_quality_gate(agent_id)
    res = c.post(f"/api/agents/{agent_id}/publish")
    assert res.status_code == 200 and res.json()["status"] == "published"

    # monitoring endpoints respond for the published agent
    assert c.get(f"/api/agents/{agent_id}/monitoring").status_code == 200
    traces = c.get(f"/api/agents/{agent_id}/monitoring/traces")
    assert traces.status_code == 200
    assert len(traces.json()["traces"]) >= 1  # the seeded run summaries surface


# ── 24.2 Legacy pipeline → DAG conversion ──────────────────────────────────────


def test_24_2_legacy_agent_converts_to_valid_dag():
    from app.core.legacy_converter import auto_convert_agent
    from app.core.dag_executor import DAGParser
    from app.core.hermes import ConnectionConstraintEngine
    from app.models.db import PromptConfig, ModelConfig

    # Seed a legacy (pipeline) agent: prompt + model config, no DAG graph yet.
    with Session(engine) as session:
        pc = PromptConfig(
            role_name="客服", role_description="耐心解答",
            system_prompt="你是一个专业客服。" * 20, output_format="markdown",
        )
        mc = ModelConfig(provider="glm", model_name="qwen3.6-27b")
        session.add(pc)
        session.add(mc)
        session.commit()
        session.refresh(pc)
        session.refresh(mc)
        agent = Agent(name="legacy", prompt_config_id=pc.id, model_config_id=mc.id)
        session.add(agent)
        session.commit()
        session.refresh(agent)
        agent_id = agent.id

        # First open → convert.
        assert auto_convert_agent(agent, session) is True
        # Second open is a no-op (already has a DAG).
        assert auto_convert_agent(agent, session) is False

        dag = session.exec(
            select(DAGGraph).where(DAGGraph.agent_id == agent_id)
        ).first()
        assert dag is not None

    # Converted graph is a valid P→M DAG: parseable, acyclic, M has P upstream.
    parser = DAGParser(dag.graph_json)
    node_types = sorted(n.node_type for n in parser.nodes)
    assert node_types == ["m", "p"]
    assert parser.has_cycles() is False
    conn = ConnectionConstraintEngine.evaluate(parser)
    assert conn.is_valid, [e.message for e in conn.errors]


# ── 24.3 Hermes gating ─────────────────────────────────────────────────────────


def _new_agent_with_graph(c: TestClient, graph_json: str) -> int:
    agent_id = c.post("/api/agents", json={"name": "hermes", "description": ""}).json()["id"]
    c.post(f"/api/agents/{agent_id}/dag-graph",
           json={"graph_json": graph_json, "state_schema": "{}"})
    return agent_id


def test_24_3_parameter_error_fails_validation(app_client):
    # agent node missing required model_name/provider → parameter error.
    bad = json.dumps({
        "nodes": [
            {"id": "i1", "type": "i", "config": {}},
            {"id": "a1", "type": "agent", "config": {"system_prompt": "hi"}},
            {"id": "o1", "type": "o", "config": {}},
        ],
        "edges": [
            {"id": "e1", "source": "i1", "target": "a1"},
            {"id": "e2", "source": "a1", "target": "o1"},
        ],
    })
    agent_id = _new_agent_with_graph(app_client, bad)
    res = app_client.post(f"/api/agents/{agent_id}/dag-graph/validate",
                          json={"graph_json": bad, "state_schema": "{}"})
    body = res.json()
    assert body["valid"] is False
    assert any(e["rule_type"] == "param_validation" for e in body["errors"])


def test_24_3_connection_error_fails_validation_and_blocks_publish(app_client, stub_llm):
    # I node has an incoming edge → connection constraint error. Also a cycle.
    bad = json.dumps({
        "nodes": [
            {"id": "i1", "type": "i", "config": {}},
            {"id": "a1", "type": "agent", "config": {
                "system_prompt": "x" * 200, "model_name": "qwen3.6-27b", "provider": "glm"}},
        ],
        "edges": [
            {"id": "e1", "source": "a1", "target": "i1"},  # illegal: edge into I
        ],
    })
    agent_id = _new_agent_with_graph(app_client, bad)

    res = app_client.post(f"/api/agents/{agent_id}/dag-graph/validate",
                          json={"graph_json": bad, "state_schema": "{}"})
    body = res.json()
    assert body["valid"] is False
    assert any(e["rule_type"] == "connection_constraint" for e in body["errors"])

    # A structurally broken graph (cycle) must block publish at compile time.
    cyclic = json.dumps({
        "nodes": [
            {"id": "a1", "type": "agent", "config": {
                "system_prompt": "x" * 200, "model_name": "qwen3.6-27b", "provider": "glm"}},
            {"id": "a2", "type": "agent", "config": {
                "system_prompt": "x" * 200, "model_name": "qwen3.6-27b", "provider": "glm"}},
        ],
        "edges": [
            {"id": "e1", "source": "a1", "target": "a2"},
            {"id": "e2", "source": "a2", "target": "a1"},
        ],
    })
    cyc_id = _new_agent_with_graph(app_client, cyclic)
    _seed_passing_quality_gate(cyc_id)  # gate would pass; compile must still block
    res = app_client.post(f"/api/agents/{cyc_id}/publish")
    assert res.status_code == 422
    assert any("cycle" in r.lower() for r in res.json()["detail"]["errors"])


def test_24_3_evaluation_below_threshold_blocks_publish(app_client, stub_llm):
    # Valid graph + good compile, but the release gate has a sub-threshold
    # evaluation run → publish blocked on quality, not structure.
    from app.models.db import EvaluationRun

    agent_id = _new_agent_with_graph(app_client, _io_graph())

    failing_summary = {
        "total_cases": 4, "passed_cases": 1, "pass_rate": 0.25,
        "key_total": 1, "key_passed": 0, "key_pass_rate": 0.0,
        "avg_score": 0.25, "latency_p95_ms": 50.0,
    }
    with Session(engine) as session:
        session.add(EvaluationRun(
            agent_id=agent_id, suite_id=1,
            summary=json.dumps(failing_summary), passed=False,
        ))
        for _ in range(10):
            session.add(AgentRunSummary(
                agent_id=agent_id, status="completed",
                total_duration_ms=50, token_input=5, token_output=5, node_count=2,
            ))
        session.commit()

    res = app_client.post(f"/api/agents/{agent_id}/publish")
    assert res.status_code == 422
    reasons = res.json()["detail"]["errors"]
    assert any("通过率" in r or "关键 case" in r for r in reasons)
    with Session(engine) as session:
        assert session.get(Agent, agent_id).status == AgentStatus.DRAFT


# ── 24.4 Runtime monitoring ────────────────────────────────────────────────────


def test_24_4_runtime_records_per_request_metrics(app_client, stub_llm):
    # A chat request over a published DAG must persist an AgentRunSummary row
    # carrying latency / tokens / node count, and the traces endpoint surfaces it.
    agent_id = _new_agent_with_graph(app_client, _io_graph())

    before = _run_summary_count(agent_id)
    with app_client.websocket_connect(f"/api/agent/{agent_id}") as wsx:
        wsx.send_json({"type": "message", "content": "hello"})
        while wsx.receive_json()["type"] != "done":
            pass
    after = _run_summary_count(agent_id)
    assert after == before + 1

    with Session(engine) as session:
        row = session.exec(
            select(AgentRunSummary)
            .where(AgentRunSummary.agent_id == agent_id)
            .order_by(desc(AgentRunSummary.created_at))
        ).first()
    assert row.status == "completed"
    assert row.node_count >= 1
    assert row.total_duration_ms >= 0

    traces = app_client.get(f"/api/agents/{agent_id}/monitoring/traces").json()
    assert len(traces["traces"]) >= 1
    assert traces["traces"][0]["status"] == "completed"


def test_24_4_degradation_status_surfaces_in_dashboard(app_client):
    from app.core.hermes import degradation_detector

    agent_id = app_client.post("/api/agents", json={"name": "deg", "description": ""}).json()["id"]

    # Feed the rolling-window detector enough low scores to flip to "degraded".
    for _ in range(10):
        degradation_detector.record_score(agent_id, 0.2)

    res = app_client.get(f"/api/agents/{agent_id}/monitoring")
    assert res.status_code == 200
    assert res.json()["status"] == "degraded"


def _run_summary_count(agent_id: int) -> int:
    with Session(engine) as session:
        return len(session.exec(
            select(AgentRunSummary).where(AgentRunSummary.agent_id == agent_id)
        ).all())


# ── 24.5 Template creation ──────────────────────────────────────────────────────


def test_24_5_template_creation_to_publish(app_client, stub_llm):
    from app.api.seed import seed_dag_templates

    seed_dag_templates()  # idempotent — seeds built-ins if absent

    templates = app_client.get("/api/dag-templates").json()
    assert len(templates) >= 1
    template_id = templates[0]["id"]

    # apply template → new agent pre-populated with the template's graph
    res = app_client.post(f"/api/dag-templates/{template_id}/apply")
    assert res.status_code == 200
    agent_id = res.json()["agent_id"]

    res = app_client.get(f"/api/agents/{agent_id}/dag-graph")
    assert res.status_code == 200
    graph = json.loads(res.json()["graph_json"])
    assert len(graph["nodes"]) >= 1  # canvas pre-populated from template

    # configure: overwrite with a runnable, publishable I→agent→O graph
    app_client.post(f"/api/agents/{agent_id}/dag-graph",
                    json={"graph_json": _io_graph(), "state_schema": "{}"})

    # publish after seeding quality-gate evidence
    _seed_passing_quality_gate(agent_id)
    res = app_client.post(f"/api/agents/{agent_id}/publish")
    assert res.status_code == 200 and res.json()["status"] == "published"
