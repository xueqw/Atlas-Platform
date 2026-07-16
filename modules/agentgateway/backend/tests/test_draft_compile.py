"""Backend tests for change: draft-agent-compile-and-test (T6).

Covers spec draft-agent-compile + the draft-agent-staging draft→tested delta:
- compile from the proposal's nodes/edges, structured success/errors/warnings
- node missing-config → error (no exception); complete → success + graph_json
- capability ref unresolved → warning (not error)
- single-turn dry-run (mocked): pass → ok; raise → warning, result still returned
- clean compile advances draft→tested; failure stays draft; no Agent/DAGGraph rows
"""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.api import planner as planner_api
from app.core import draft_compile, proposal_store
from app.core.draft_agent import convert_proposal_to_draft
from app.models.db import Agent, DAGGraph, DraftAgent, CapabilityItem


def _good_nodes():
    return [
        {"id": "agent1", "type": "agent", "config": {
            "system_prompt": "你是助手", "model_name": "qwen3.6-27b", "provider": "glm",
        }},
    ]


def _seed_confirmed_draft(cid="conv-t6", nodes=None, caps=None):
    payload = {
        "title": "测试 Agent", "goal": "g", "runtime_mode": "direct",
        "recommended_capabilities": caps if caps is not None else [],
        "nodes": nodes if nodes is not None else _good_nodes(),
        "edges": [],
    }
    p = proposal_store.upsert_proposal(
        conversation_id=cid, user_goal="g", inferred_goal="g", task_type="t",
        proposal_json=json.dumps(payload, ensure_ascii=False), selected_runtime_mode="direct",
    )
    proposal_store.transition_status(p.id, "confirmed")
    draft = convert_proposal_to_draft(p.id)
    return p, draft


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(planner_api.router, prefix="/api")
    return TestClient(app)


@pytest.fixture(autouse=True)
def _no_real_model(monkeypatch):
    # Dry-run must never hit the network in tests. Patch the runtime entry so a
    # dry-run "passes" unless a test overrides it.
    import app.core.agentscope_runner as runner

    async def _ok_stream(agent, user_message, attachments=None):
        yield ("token", "ok")

    monkeypatch.setattr(runner, "create_agent", lambda **kw: object())
    monkeypatch.setattr(runner, "run_conversation", _ok_stream)
    return monkeypatch


# ─── 6.1 / 6.2 compile success + missing config ─────────────────────────────

def test_compile_success_produces_graph():
    _p, draft = _seed_confirmed_draft(cid="conv-ok")
    res = draft_compile.compile_draft_agent(draft.id, run_dryrun=False)
    assert res["success"] is True
    assert not res["errors"]
    assert res["graph_json"] and json.loads(res["graph_json"])["nodes"]


def test_compile_missing_config_is_error_not_exception():
    bad = [{"id": "agent1", "type": "agent", "config": {"system_prompt": ""}}]  # missing model_name/provider
    _p, draft = _seed_confirmed_draft(cid="conv-bad", nodes=bad)
    res = draft_compile.compile_draft_agent(draft.id, run_dryrun=False)
    assert res["success"] is False
    assert any("agent1" in e for e in res["errors"])


def test_compile_no_topology_errors():
    _p, draft = _seed_confirmed_draft(cid="conv-empty", nodes=[])
    res = draft_compile.compile_draft_agent(draft.id, run_dryrun=False)
    assert res["success"] is False
    assert any("拓扑" in e for e in res["errors"])


# ─── 6.3 capability ref resolution warning ──────────────────────────────────

def test_unresolved_capability_ref_warns():
    _p, draft = _seed_confirmed_draft(cid="conv-cap", caps=[{"id": 999999, "type": "tool", "name": "ghost"}])
    res = draft_compile.compile_draft_agent(draft.id, run_dryrun=False)
    assert any("ghost" in w or "未解析" in w for w in res["warnings"])
    assert res["success"] is True  # missing cap is a warning, not an error


def test_resolved_capability_no_warning():
    with Session(planner_api.engine) as s:
        if not s.exec(select(CapabilityItem).where(CapabilityItem.name == "real-tool")).first():
            s.add(CapabilityItem(type="tool", name="real-tool", description="d", tags="[]", config="{}"))
            s.commit()
        cid = s.exec(select(CapabilityItem).where(CapabilityItem.name == "real-tool")).first().id
    _p, draft = _seed_confirmed_draft(cid="conv-cap2", caps=[{"id": cid, "type": "tool", "name": "real-tool"}])
    res = draft_compile.compile_draft_agent(draft.id, run_dryrun=False)
    assert not any("real-tool" in w for w in res["warnings"])


# ─── 6.4 dry-run ────────────────────────────────────────────────────────────

def test_dry_run_pass():
    _p, draft = _seed_confirmed_draft(cid="conv-dry-ok")
    res = draft_compile.compile_draft_agent(draft.id, run_dryrun=True)
    assert res["dryrun"]["ran"] is True and res["dryrun"]["ok"] is True


def test_dry_run_failure_becomes_warning(monkeypatch):
    import app.core.agentscope_runner as runner

    def _boom(**kw):
        raise RuntimeError("no credentials")

    monkeypatch.setattr(runner, "create_agent", _boom)
    _p, draft = _seed_confirmed_draft(cid="conv-dry-fail")
    res = draft_compile.compile_draft_agent(draft.id, run_dryrun=True)
    assert res["dryrun"]["ok"] is False
    assert any("dry-run" in w for w in res["warnings"])
    # compile itself still succeeded (dry-run failure is non-blocking)
    assert res["success"] is True


# ─── 6.5 status advance + no Agent rows ─────────────────────────────────────

def test_clean_compile_advances_to_tested():
    _p, draft = _seed_confirmed_draft(cid="conv-tested")
    assert draft.status == "draft"
    draft_compile.compile_draft_agent(draft.id, run_dryrun=True)
    with Session(planner_api.engine) as s:
        assert s.get(DraftAgent, draft.id).status == "tested"
        # T6 must NOT create production Agent / DAGGraph rows
        assert s.exec(select(Agent).where(Agent.name == "测试 Agent")).first() is None


def test_failed_compile_stays_draft():
    bad = [{"id": "agent1", "type": "agent", "config": {"system_prompt": ""}}]
    _p, draft = _seed_confirmed_draft(cid="conv-stay", nodes=bad)
    draft_compile.compile_draft_agent(draft.id, run_dryrun=False)
    with Session(planner_api.engine) as s:
        assert s.get(DraftAgent, draft.id).status == "draft"


# ─── 6.6 trigger API ────────────────────────────────────────────────────────

def test_compile_endpoint(client: TestClient):
    _p, draft = _seed_confirmed_draft(cid="conv-api6")
    resp = client.post(f"/api/planner/draft-agents/{draft.id}/compile", json={"dry_run": False})
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True and "graph_json" in body
    with Session(planner_api.engine) as s:
        assert s.exec(select(Agent).where(Agent.name == "测试 Agent")).first() is None
