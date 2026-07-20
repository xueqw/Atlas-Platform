"""Tests for enhance-planner-state-schema (Batch A — Planner Core).

Covers the change's acceptance scenarios:
- 7.1 richer _new_memory() defaults + tolerant _merge_memory_update()
- 7.2 A2UI decision ledger (decisions_confirmed) + session restore round-trip
- 7.3 backward compat: old PlannerSession rows load with safe defaults; apply
      of an agent_spec-less proposal still lands; agent_spec-bearing proposal is
      read compatibly.

Pure-function tests call the helpers directly; persistence tests go through the
session persist/load helpers against the temp SQLite DB patched in conftest.
"""

from __future__ import annotations

import json

from app.api.planner import state as planner_state
from app.api.planner.state import (
    _new_memory,
    _merge_memory_update,
    _normalize_pending_a2ui,
    _build_decision_entry,
)
from app.api.planner.proposal import (
    _extract_agent_spec,
    _extract_proposal_meta,
    _proposal_meta,
)
from app.api.planner.prompts import _build_system_prompt_with_memory
from app.api.planner import sessions as planner_sessions


# ─── 7.1 richer state defaults ──────────────────────────────────────────────

_EXPECTED_FIELDS = {
    # legacy 6
    "requirement_summary", "confirmed_constraints", "task_classification",
    "latest_proposal_summary", "user_feedback", "selected_skills",
    # new 14
    "open_questions", "assumptions", "non_goals", "success_metrics",
    "decisions_pending", "decisions_confirmed", "tradeoffs", "risks",
    "architecture_pattern", "runtime_strategy", "memory_strategy",
    "knowledge_strategy", "evaluation_strategy", "apply_readiness",
    # planner memory integrity metadata
    "goal_anchor", "memory_update_audit", "pending_continuation",
}


def test_new_memory_has_all_fields_with_safe_defaults():
    mem = _new_memory()
    assert set(mem.keys()) == _EXPECTED_FIELDS
    assert len(mem) == 23
    # list fields default to []
    for k in ("confirmed_constraints", "user_feedback", "open_questions",
              "assumptions", "non_goals", "success_metrics", "decisions_pending",
              "decisions_confirmed", "tradeoffs", "risks", "selected_skills"):
        assert mem[k] == [], k
    # strategy fields default to {}
    for k in ("runtime_strategy", "memory_strategy", "knowledge_strategy",
              "evaluation_strategy"):
        assert mem[k] == {}, k
    # string fields default to ""
    for k in ("requirement_summary", "task_classification",
              "latest_proposal_summary", "architecture_pattern"):
        assert mem[k] == "", k
    # apply_readiness default status
    assert mem["apply_readiness"] == {"status": "not_ready", "missing": [], "recommendation": ""}
    assert mem["goal_anchor"] == {}
    assert mem["memory_update_audit"] == []
    assert mem["pending_continuation"] == {}


# ─── 7.1 tolerant merge ─────────────────────────────────────────────────────

def test_merge_updates_produced_fields_keeps_missing():
    mem = _new_memory()
    mem["requirement_summary"] = "旧需求"
    mem["risks"] = ["旧风险"]
    _merge_memory_update(mem, {"task_classification": "简单RAG", "risks": ["新风险A", "新风险B"]})
    # produced fields updated
    assert mem["task_classification"] == "简单RAG"
    assert mem["risks"] == ["旧风险", "新风险A", "新风险B"]
    # missing fields keep old value
    assert mem["requirement_summary"] == "旧需求"


def test_merge_ignores_unknown_fields():
    mem = _new_memory()
    _merge_memory_update(mem, {"totally_unknown": "x", "requirement_summary": "需求"})
    assert "totally_unknown" not in mem
    assert mem["requirement_summary"] == "需求"


def test_merge_empty_values_do_not_wipe_state():
    mem = _new_memory()
    mem["confirmed_constraints"] = ["c1"]
    mem["requirement_summary"] = "保留"
    # empty list / empty string count as "not produced this turn"
    _merge_memory_update(mem, {"confirmed_constraints": [], "requirement_summary": ""})
    assert mem["confirmed_constraints"] == ["c1"]
    assert mem["requirement_summary"] == "保留"


def test_merge_does_not_let_model_overwrite_selected_skills():
    mem = _new_memory()
    mem["selected_skills"] = ["a2ui"]
    _merge_memory_update(mem, {"selected_skills": ["evil"]})
    # selected_skills is attach/detach-managed, not model-driven
    assert mem["selected_skills"] == ["a2ui"]


def test_merge_malformed_field_is_skipped_not_fatal():
    mem = _new_memory()
    # risks expects a list; a string is malformed → skipped, other fields applied
    _merge_memory_update(mem, {"risks": "not-a-list", "task_classification": "内容生成"})
    assert mem["risks"] == []
    assert mem["task_classification"] == "内容生成"


def test_merge_apply_readiness_merges_onto_default():
    mem = _new_memory()
    _merge_memory_update(
        mem,
        {"apply_readiness": {"status": "ready"}},
        has_validated_proposal=True,
    )
    # status overridden, other keys keep safe defaults
    assert mem["apply_readiness"]["status"] == "ready"
    assert mem["apply_readiness"]["missing"] == []
    assert mem["apply_readiness"]["recommendation"] == ""


def test_merge_non_dict_update_is_noop():
    mem = _new_memory()
    assert _merge_memory_update(mem, None) is mem  # type: ignore[arg-type]
    assert mem == _new_memory()


# ─── 7.2 decision ledger ────────────────────────────────────────────────────

def test_normalize_pending_a2ui_fills_required_keys():
    norm = _normalize_pending_a2ui({"id": "d1", "prompt": "确认?", "options": [{"id": "y", "label": "是"}]})
    assert norm["id"] == "d1"
    assert norm["prompt"] == "确认?"
    assert norm["topic"] == "确认?"  # falls back to prompt
    assert norm["options"] == [{"id": "y", "label": "是"}]
    assert norm["allow_free_text"] is False


def test_normalize_pending_a2ui_tolerates_garbage():
    norm = _normalize_pending_a2ui({"options": "not-a-list"})
    assert norm["options"] == []
    assert norm["id"] == ""


def test_build_decision_entry_structure():
    pending = _normalize_pending_a2ui(
        {"id": "confirm_arch", "topic": "架构确认", "prompt": "方案 OK?",
         "options": [{"id": "yes", "label": "确认创建"}]}
    )
    entry = _build_decision_entry(pending, choice_id="yes", choice_label="确认创建", free_text="温度调到0.5")
    assert entry["id"] == "confirm_arch"
    assert entry["topic"] == "架构确认"
    assert entry["prompt"] == "方案 OK?"
    assert entry["selected_option"] == {"id": "yes", "label": "确认创建"}
    assert entry["free_text"] == "温度调到0.5"
    assert entry["status"] == "confirmed"
    assert "effect_on_plan" in entry
    # every contract field present
    for k in ("id", "topic", "prompt", "options", "selected_option", "free_text", "status", "effect_on_plan"):
        assert k in entry


def test_confirmed_decisions_injected_into_prompt_not_reasked():
    mem = _new_memory()
    mem["requirement_summary"] = "做一个客服bot"
    mem["decisions_confirmed"] = [{
        "id": "d1", "topic": "是否启用知识库", "prompt": "要不要知识库?",
        "options": [], "selected_option": {"id": "yes", "label": "启用"},
        "free_text": "", "status": "confirmed", "effect_on_plan": "",
    }]
    prompt = _build_system_prompt_with_memory(mem, mode="create")
    assert "已确认决策" in prompt
    assert "是否启用知识库" in prompt
    assert "启用" in prompt
    assert "勿重复询问" in prompt


# ─── 7.2 session restore round-trip ─────────────────────────────────────────

def test_persist_and_load_round_trip_preserves_richer_state():
    conv_id = "test-conv-richer-1"
    mem = _new_memory()
    mem["requirement_summary"] = "构建一个 RAG 助手"
    mem["architecture_pattern"] = "rag"
    mem["risks"] = ["知识库过期风险"]
    mem["runtime_strategy"] = {"model": "qwen3.6-27b"}
    mem["decisions_confirmed"] = [{
        "id": "d1", "topic": "知识库", "prompt": "用知识库?",
        "options": [], "selected_option": {"id": "yes", "label": "启用"},
        "free_text": "", "status": "confirmed", "effect_on_plan": "新增 K 节点",
    }]
    mem["apply_readiness"] = {"status": "ready", "missing": [], "recommendation": "可创建"}

    conv_data = {"memory": mem, "messages": [{"role": "user", "content": "hi"}], "file_artifacts": []}
    planner_sessions._persist_session(conv_id, conv_data)

    loaded = planner_sessions._load_session_into_memory(conv_id)
    assert loaded is not None
    lm = loaded["memory"]
    assert lm["requirement_summary"] == "构建一个 RAG 助手"
    assert lm["architecture_pattern"] == "rag"
    assert lm["risks"] == ["知识库过期风险"]
    assert lm["runtime_strategy"] == {"model": "qwen3.6-27b"}
    # decision ledger fully restored
    assert len(lm["decisions_confirmed"]) == 1
    assert lm["decisions_confirmed"][0]["topic"] == "知识库"
    assert lm["decisions_confirmed"][0]["effect_on_plan"] == "新增 K 节点"
    # A legacy/model-authored ready flag is revalidated on load and cannot
    # survive without a persisted validated proposal.
    assert lm["apply_readiness"]["status"] == "not_ready"
    assert "validated_proposal" in lm["apply_readiness"]["missing"]


def test_load_session_reconstructs_pending_a2ui_from_decisions_pending():
    """A restored legacy LLM planner session must re-push a pending A2UI card.

    The model may say "I have sent a confirmation card" and persist only
    memory.decisions_pending/apply_readiness=not_ready. If the live WS event was
    missed (refresh/reconnect/server restart), restore must rebuild pending_a2ui
    so the frontend can actually render the human-interaction card.
    """
    conv_id = "test-conv-pending-a2ui-restore-1"
    mem = _new_memory()
    mem["requirement_summary"] = "构建时政分析智能体"
    mem["decisions_pending"] = [{
        "id": "d1",
        "topic": "首版输出重心",
        "options": ["偏日报简报", "偏热点深度分析", "偏政策影响研判"],
    }]
    mem["apply_readiness"] = {
        "status": "not_ready",
        "missing": ["待确认首版输出重心"],
        "recommendation": "先确认首版输出场景，再固化系统提示词与输出模板",
    }
    conv_data = {
        "memory": mem,
        "messages": [
            {"role": "user", "content": "帮我做个时政的分析智能体"},
            {"role": "assistant", "content": "我已发起确认卡片。"},
        ],
        "file_artifacts": [],
    }
    planner_sessions._persist_session(conv_id, conv_data)

    loaded = planner_sessions._load_session_into_memory(conv_id)

    assert loaded is not None
    assert loaded["pending_a2ui"] == {
        "id": "d1",
        "topic": "首版输出重心",
        "prompt": "请选择：首版输出重心",
        "options": [
            {"id": "option_1", "label": "偏日报简报"},
            {"id": "option_2", "label": "偏热点深度分析"},
            {"id": "option_3", "label": "偏政策影响研判"},
        ],
        "allow_free_text": False,
    }


# ─── 7.3 backward compat ────────────────────────────────────────────────────

def test_old_row_without_new_columns_loads_with_safe_defaults():
    """A historical row whose new columns hold their DB defaults ("{}"/"[]"/"")
    must load into full richer state via safe defaults, not fail."""
    from sqlmodel import Session
    from app.api.planner import engine
    from app.models.db import PlannerSession

    conv_id = "test-conv-legacy-1"
    with Session(engine) as session:
        row = PlannerSession(
            conversation_id=conv_id,
            session_title="老会话",
            stage="drafting",
            requirement_summary="老需求",
            confirmed_constraints=json.dumps(["老约束"], ensure_ascii=False),
            task_classification="内容生成",
            # new columns left at their model defaults (simulating a pre-migration row)
        )
        session.add(row)
        session.commit()

    loaded = planner_sessions._load_session_into_memory(conv_id)
    assert loaded is not None
    lm = loaded["memory"]
    # legacy fields preserved
    assert lm["requirement_summary"] == "老需求"
    assert lm["confirmed_constraints"] == ["老约束"]
    assert lm["task_classification"] == "内容生成"
    # new fields present with safe defaults — full 20-field shape
    assert set(lm.keys()) == _EXPECTED_FIELDS
    assert lm["risks"] == []
    assert lm["decisions_confirmed"] == []
    assert lm["architecture_pattern"] == ""
    assert lm["apply_readiness"] == {"status": "not_ready", "missing": [], "recommendation": ""}


def test_extract_agent_spec_present_and_absent():
    # absent → None (old proposal)
    assert _extract_agent_spec({"nodes": [], "edges": []}) is None
    assert _extract_agent_spec({"agent_spec": {}}) is None
    # present → only documented sub-keys surfaced
    spec = _extract_agent_spec({
        "agent_spec": {
            "identity": {"role_name": "助手"},
            "runtime": {"model_name": "qwen3.6-27b"},
            "memory": {"enabled": False},
            "junk": "ignored",
        }
    })
    assert spec is not None
    assert set(spec.keys()) == {"identity", "runtime", "memory"}
    assert "junk" not in spec


def test_extract_proposal_meta_old_proposal_is_empty_not_raising():
    meta = _extract_proposal_meta({"nodes": [], "edges": []})
    assert meta == {}  # nothing to read, no raise


def test_extract_proposal_meta_reads_optional_fields():
    meta = _extract_proposal_meta({
        "architecture_pattern": "rag",
        "apply_readiness": {"status": "ready"},
        "agent_spec": {"identity": {"role_name": "x"}},
    })
    assert meta["architecture_pattern"] == "rag"
    assert meta["apply_readiness"] == {"status": "ready"}
    assert meta["agent_spec"]["identity"] == {"role_name": "x"}


def test_proposal_meta_kind_inference():
    # ready status → kind ready
    m = _proposal_meta({"apply_readiness": {"status": "ready"}})
    assert m["kind"] == "ready"
    # has agent_spec, not ready → kind spec
    m = _proposal_meta({"agent_spec": {"identity": {}}})
    assert m["kind"] == "spec"
    # old proposal, no spec, no readiness → draft + safe default readiness
    m = _proposal_meta({"nodes": [], "edges": []})
    assert m["kind"] == "draft"
    assert m["apply_readiness"]["status"] == "not_ready"
    assert m["architecture_pattern"] == ""
