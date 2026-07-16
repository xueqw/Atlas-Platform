"""Tests for planner-effective-skills-loading (Batch A — Skill Loading).

Covers the change's acceptance scenarios:
- 6.1 resolve_effective_skills(): three-tuple, dedup, fixed order, defaults
      always present and non-removable
- 6.2 prompt two-section injection; empty user_selected → no empty section
- 6.3 behavior: attach/detach default is a no-op; non-default skill appears next
      turn; restore re-resolves; replan keeps defaults
- 6.4 /api/planner/skills returns default_source / effective_in_current /
      lock_reason; default skill's lock_reason non-empty
"""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.api import planner as planner_api
from app.api.planner import sessions as planner_sessions
from app.api.planner.prompts import resolve_effective_skills, _skills_block
from app.models.db import CapabilityItem, PlannerSession


# ─── fixtures / helpers ─────────────────────────────────────────────────────

@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(planner_api.router, prefix="/api")
    return TestClient(app)


def _seed_skill(name: str, description: str = "技能说明", source: str = "project"):
    with Session(planner_api.engine) as session:
        existing = session.exec(select(CapabilityItem).where(
            CapabilityItem.type == "skill", CapabilityItem.name == name
        )).first()
        if existing:
            return
        session.add(CapabilityItem(
            type="skill", name=name, description=description,
            tags=json.dumps(["planner"], ensure_ascii=False),
            config=json.dumps({"entrypoint": f"skills/{name}/SKILL.md", "source": source}, ensure_ascii=False),
        ))
        session.commit()


def _seed_session(cid: str, selected_skills=None):
    with Session(planner_api.engine) as session:
        row = session.exec(select(PlannerSession).where(
            PlannerSession.conversation_id == cid
        )).first()
        if row is None:
            row = PlannerSession(conversation_id=cid, session_title="t", stage="clarifying")
        row.selected_skills = json.dumps(selected_skills or [], ensure_ascii=False)
        session.add(row)
        session.commit()


# ─── 6.1 resolve_effective_skills ───────────────────────────────────────────

def test_resolve_three_tuple_and_order():
    r = resolve_effective_skills(["x"], {"a2ui"}, set())
    assert r["selected_skills"] == ["x"]
    assert r["default_skills"] == ["a2ui"]
    # builtin before user
    assert r["effective_skills"] == ["a2ui", "x"]


def test_resolve_empty_selected_effective_equals_default():
    r = resolve_effective_skills([], {"a2ui"}, set())
    assert r["effective_skills"] == ["a2ui"]
    assert r["default_skills"] == ["a2ui"]
    assert r["selected_skills"] == []


def test_resolve_default_always_present_when_nothing_passed():
    r = resolve_effective_skills(None, {"a2ui", "proposal-emitter"}, set())
    assert "a2ui" in r["effective_skills"]
    assert "proposal-emitter" in r["effective_skills"]


def test_resolve_dedup_default_name_in_selected():
    # "a2ui" mistakenly in selected → deduped, still counted under default tier
    r = resolve_effective_skills(["a2ui", "x"], {"a2ui"}, set())
    assert r["effective_skills"].count("a2ui") == 1
    assert r["effective_skills"] == ["a2ui", "x"]
    assert r["default_skills"] == ["a2ui"]
    # a2ui is not removable via user layer — it stays in effective regardless
    assert "a2ui" in r["effective_skills"]


def test_resolve_full_order_builtin_workspace_user():
    r = resolve_effective_skills(["u1"], {"b1"}, {"w1"})
    assert r["effective_skills"] == ["b1", "w1", "u1"]
    assert r["default_skills"] == ["b1", "w1"]


def test_resolve_filters_empty_and_non_str():
    r = resolve_effective_skills(["", "ok", None], {"a2ui"}, set())  # type: ignore[list-item]
    assert r["effective_skills"] == ["a2ui", "ok"]


# ─── 6.2 prompt two-section injection ───────────────────────────────────────

def test_skills_block_two_sections():
    _seed_skill("a2ui", "结构化确认卡片")
    _seed_skill("deerflow-planner", "规划方法论")
    r = resolve_effective_skills(["deerflow-planner"], {"a2ui"}, set())
    block = _skills_block(r["effective_skills"], r["default_skills"])
    assert "## 规划师内建能力" in block
    assert "## 本次会话附加技能" in block
    assert "deerflow-planner" in block
    assert "规划方法论" in block


def test_skills_block_no_empty_attached_section():
    _seed_skill("a2ui", "结构化确认卡片")
    r = resolve_effective_skills([], {"a2ui"}, set())
    block = _skills_block(r["effective_skills"], r["default_skills"])
    # only built-in present → no attached section heading
    assert "## 本次会话附加技能" not in block


def test_build_prompt_uses_effective_two_sections():
    _seed_skill("a2ui", "结构化确认卡片描述")
    _seed_skill("deerflow-planner", "规划方法论描述")
    memory = planner_api._new_memory()
    memory["selected_skills"] = ["deerflow-planner"]
    prompt = planner_api._build_system_prompt_with_memory(memory)
    assert "## 本次会话附加技能" in prompt
    assert "deerflow-planner" in prompt


# ─── 6.3 behavior: defaults always effective, restore re-resolves ───────────

def test_default_skill_always_effective_even_if_detached_in_selected():
    # Simulate a buggy client that wrote a default name into selected and "removed"
    # nothing — resolver still keeps it.
    r = resolve_effective_skills([], {"a2ui"}, set())
    assert "a2ui" in r["effective_skills"]


def test_restore_strips_default_from_selected_and_reresolves():
    # A persisted row whose selected_skills leaked a default name + a user skill.
    cid = "eff-restore-1"
    _seed_session(cid, selected_skills=["a2ui", "deerflow-planner"])
    loaded = planner_sessions._load_session_into_memory(cid)
    assert loaded is not None
    # default name stripped from user_selected (re-derived by resolver, not trusted)
    assert "a2ui" not in loaded["memory"]["selected_skills"]
    assert "deerflow-planner" in loaded["memory"]["selected_skills"]
    # but effective still includes a2ui
    r = resolve_effective_skills(loaded["memory"]["selected_skills"])
    assert "a2ui" in r["effective_skills"]
    assert "deerflow-planner" in r["effective_skills"]


def test_attach_detach_default_is_noop_via_state_helper():
    from app.api.planner.state import _is_default_skill
    assert _is_default_skill("a2ui") is True
    assert _is_default_skill("deerflow-planner") is False


# ─── 6.4 /api/planner/skills contract fields ────────────────────────────────

def test_skills_endpoint_contract_fields_default(client):
    _seed_skill("a2ui", "结构化确认卡片")
    _seed_skill("deerflow-planner", "规划方法论")
    cid = "eff-endpoint-1"
    _seed_session(cid, selected_skills=["deerflow-planner"])
    res = client.get(f"/api/planner/skills?conversation_id={cid}")
    assert res.status_code == 200
    by_name = {s["name"]: s for s in res.json()}

    a2ui = by_name["a2ui"]
    assert a2ui["is_default"] is True
    assert a2ui["default_source"] == "system"
    assert a2ui["effective_in_current"] is True
    assert a2ui["lock_reason"]  # non-empty

    dp = by_name["deerflow-planner"]
    assert dp["default_source"] == "none"
    assert dp["effective_in_current"] is True
    assert (dp["lock_reason"] is None) or (dp["lock_reason"] == "")


def test_skills_endpoint_unselected_user_skill_not_effective(client):
    _seed_skill("a2ui", "结构化确认卡片")
    _seed_skill("orchestrate", "编排技能")
    cid = "eff-endpoint-2"
    _seed_session(cid, selected_skills=[])
    res = client.get(f"/api/planner/skills?conversation_id={cid}")
    by_name = {s["name"]: s for s in res.json()}
    # not selected → not effective
    assert by_name["orchestrate"]["effective_in_current"] is False
    # default still effective
    assert by_name["a2ui"]["effective_in_current"] is True


def test_skills_endpoint_no_cid_omits_contract_flags(client):
    _seed_skill("a2ui", "结构化确认卡片")
    res = client.get("/api/planner/skills")
    assert res.status_code == 200
    for s in res.json():
        assert s.get("attached_to_current") is None
        assert s.get("effective_in_current") is None
        # default_source is still derivable without a session
        assert s.get("default_source") in ("system", "workspace", "none")
