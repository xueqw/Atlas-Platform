"""Tests for planning-context-aggregation (T1).

Covers the change's acceptance scenarios:
- 6.1 seven segments always present, shape stable, default-row / hardcoded
      fallback, capabilities carry only id/type/name (no raw content)
- 6.2 per-source failure degrades that segment but the whole aggregation
      still succeeds
- 6.3 GET /planner/planning-context returns the full shape with no write
      side effects
"""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.api import planner as planner_api
from app.core import planning_context as pc
from app.models.db import CapabilityItem, UserProfile, WorkspaceContext


_SEGMENTS = {
    "user_input", "user_profile", "memory_context", "workspace_context",
    "external_context", "internal_context", "policy_context",
}


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(planner_api.router, prefix="/api")
    return TestClient(app)


def _seed_capability(name: str, ctype: str = "tool"):
    with Session(planner_api.engine) as session:
        existing = session.exec(
            select(CapabilityItem).where(CapabilityItem.name == name)
        ).first()
        if existing:
            return
        session.add(CapabilityItem(
            type=ctype, name=name, description="desc",
            tags=json.dumps(["t"]),
            config=json.dumps({"content": "RAW MARKDOWN SHOULD NOT LEAK"}),
        ))
        session.commit()


# ─── 6.1 shape / fallback / no-raw-content ──────────────────────────────────

def test_seven_segments_always_present():
    ctx = pc.build_planning_context(user_input={"goal_text": "做一个客户分析 agent"})
    assert set(ctx.keys()) == _SEGMENTS
    # every segment is a present dict, not a dropped key
    for seg in _SEGMENTS:
        assert ctx[seg] is not None


def test_shape_stable_across_calls():
    a = pc.build_planning_context(user_input={"goal_text": "x"})
    b = pc.build_planning_context(user_input={"goal_text": "y"})
    assert set(a.keys()) == set(b.keys())
    assert set(a["memory_context"].keys()) == set(b["memory_context"].keys())


def test_user_input_carries_attachments_and_constraints():
    ctx = pc.build_planning_context(user_input={
        "goal_text": "g",
        "attachments": [{"type": "doc", "id": "f1"}],
        "explicit_constraints": ["只读 CRM"],
    })
    ui = ctx["user_input"]
    assert ui["goal_text"] == "g"
    assert ui["attachments"] == [{"type": "doc", "id": "f1"}]
    assert ui["explicit_constraints"] == ["只读 CRM"]


def test_user_profile_hardcoded_fallback_when_unseeded():
    # No default row seeded in the test DB → hardcoded default dict.
    ctx = pc.build_planning_context(user_input={"goal_text": "g"})
    prof = ctx["user_profile"]
    assert prof["user_id"] == "default"
    assert prof["preferences"].get("interaction_style") == "proposal_first"


def test_user_profile_reads_seeded_row():
    with Session(planner_api.engine) as s:
        if s.get(UserProfile, "default") is None:
            s.add(UserProfile(id="default", name="张三", role="销售总监",
                              preferences_json=json.dumps({"language": "zh-CN"})))
            s.commit()
    ctx = pc.build_planning_context(user_input={"goal_text": "g"})
    assert ctx["user_profile"]["role"] == "销售总监"


def test_workspace_context_fallback_and_seeded():
    ctx = pc.build_planning_context(user_input={"goal_text": "g"})
    assert ctx["workspace_context"]["workspace_id"] == "default"
    with Session(planner_api.engine) as s:
        if s.get(WorkspaceContext, "default") is None:
            s.add(WorkspaceContext(id="default", name="华东销售工作台",
                                   connected_systems_json=json.dumps(["crm"])))
            s.commit()
    ctx2 = pc.build_planning_context(user_input={"goal_text": "g"})
    assert "crm" in ctx2["workspace_context"]["connected_systems"]


def test_internal_capabilities_only_id_type_name_no_raw():
    _seed_capability("sql_query", "tool")
    ctx = pc.build_planning_context(user_input={"goal_text": "g"})
    caps = ctx["internal_context"]["capabilities"]
    assert any(c["name"] == "sql_query" for c in caps)
    for c in caps:
        # structured ref only — no config/content/description leakage
        assert set(c.keys()) == {"id", "type", "name"}
    # belt-and-suspenders: raw markdown never appears anywhere in the segment
    assert "RAW MARKDOWN SHOULD NOT LEAK" not in json.dumps(ctx["internal_context"])
    assert ctx["internal_context"]["runtime_modes"] == pc.RUNTIME_MODES


def test_external_context_empty_systems_at_t1():
    ctx = pc.build_planning_context(user_input={"goal_text": "g"})
    assert ctx["external_context"]["systems"] == []


def test_policy_context_defaults():
    ctx = pc.build_planning_context(user_input={"goal_text": "g"})
    pol = ctx["policy_context"]
    assert pol["clarification_policy"] == "minimal"
    assert pol["publish_policy"] == "draft_before_publish"


# ─── 6.2 degrade-on-source-failure ──────────────────────────────────────────

def test_single_source_failure_degrades_segment_only(monkeypatch):
    def boom(*_args, **_kwargs):
        raise RuntimeError("source down")

    # Break the internal_context builder; the rest must still aggregate.
    monkeypatch.setattr(pc, "_build_internal_context", boom)
    ctx = pc.build_planning_context(user_input={"goal_text": "g"})
    assert set(ctx.keys()) == _SEGMENTS  # still total
    # degraded segment falls back to empty/default form
    assert ctx["internal_context"]["capabilities"] == []
    assert ctx["internal_context"]["runtime_modes"] == pc.RUNTIME_MODES
    # an unrelated segment is unaffected
    assert ctx["policy_context"]["clarification_policy"] == "minimal"


# ─── 6.3 read-only API shape ────────────────────────────────────────────────

def test_api_returns_full_shape(client: TestClient):
    resp = client.get("/api/planner/planning-context", params={"goal_text": "做个日报 agent"})
    assert resp.status_code == 200
    body = resp.json()
    assert "planning_context" in body
    ctx = body["planning_context"]
    assert set(ctx.keys()) == _SEGMENTS
    assert ctx["user_input"]["goal_text"] == "做个日报 agent"


def test_api_has_no_write_side_effect(client: TestClient):
    # Calling the read-only endpoint must not create source rows.
    with Session(planner_api.engine) as s:
        # ensure a clean slate for this assertion's intent: capture count
        before = len(s.exec(select(UserProfile)).all())
    client.get("/api/planner/planning-context", params={"goal_text": "x"})
    with Session(planner_api.engine) as s:
        after = len(s.exec(select(UserProfile)).all())
    assert after == before
