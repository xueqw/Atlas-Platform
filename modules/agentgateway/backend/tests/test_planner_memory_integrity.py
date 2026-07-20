"""Acceptance tests for governed planner-memory integrity.

The transcript/decision log is the ledger, planner memory is the materialized
view, and these tests exercise the policy boundary between model candidates and
that view.
"""

from __future__ import annotations

import json

from sqlmodel import Session

from app.api.planner import sessions as planner_sessions
from app.api.planner.state import (
    _ensure_goal_anchor,
    _merge_memory_update,
    _new_memory,
)
from app.models.db import PlannerSession


def test_first_substantive_user_turn_creates_bitemporal_goal_anchor():
    memory = _new_memory()

    anchor = _ensure_goal_anchor(
        memory,
        [
            {"role": "user", "content": "请继续"},
            {
                "id": "msg-stock-1",
                "role": "user",
                "content": "帮我创建一个面向 A 股的选股智能体",
                "created_at": "2026-07-16T08:30:00Z",
            },
        ],
        conversation_id="anchor-conv-1",
        transaction_time="2026-07-17T03:00:00Z",
    )

    assert anchor is not None
    assert anchor["text"] == "帮我创建一个面向 A 股的选股智能体"
    assert anchor["source"] == {
        "kind": "planner_message",
        "message_index": 1,
        "message_id": "msg-stock-1",
        "conversation_id": "anchor-conv-1",
    }
    assert anchor["valid_time"] == {
        "from": "2026-07-16T08:30:00Z",
        "to": None,
    }
    assert anchor["transaction_time"] == {
        "recorded_at": "2026-07-17T03:00:00Z",
    }


def test_goal_anchor_is_immutable_when_ensure_runs_again():
    memory = _new_memory()
    original = _ensure_goal_anchor(
        memory,
        [{"role": "user", "content": "创建股票筛选智能体"}],
        conversation_id="anchor-conv-2",
        transaction_time="2026-07-17T03:00:00Z",
    )

    repeated = _ensure_goal_anchor(
        memory,
        [{"role": "user", "content": "改成客服智能体"}],
        conversation_id="anchor-conv-2",
        transaction_time="2026-07-18T03:00:00Z",
    )

    assert repeated is original
    assert repeated["text"] == "创建股票筛选智能体"
    assert repeated["transaction_time"]["recorded_at"] == "2026-07-17T03:00:00Z"


def test_unrelated_summary_overwrite_without_user_evidence_is_rejected():
    memory = _new_memory()
    _ensure_goal_anchor(
        memory,
        [{"role": "user", "content": "帮我做一个股票筛选和行情分析智能体"}],
        conversation_id="stock-session",
        transaction_time="2026-07-17T03:00:00Z",
    )
    memory["requirement_summary"] = "股票筛选和行情分析智能体"

    _merge_memory_update(
        memory,
        {"requirement_summary": "处理售后工单的客服智能体"},
        recent_user_text="请继续创建",
        source_turn="turn-2",
        transaction_time="2026-07-17T03:01:00Z",
    )

    assert memory["requirement_summary"] == "股票筛选和行情分析智能体"
    decision = memory["memory_update_audit"][-1]
    assert decision["field"] == "requirement_summary"
    assert decision["decision"] == "rejected"
    assert decision["reason"] == "conflicts_with_goal_anchor_without_user_evidence"
    assert decision["source_turn"] == "turn-2"


def test_explicit_user_pivot_accepts_new_summary_and_records_evidence():
    memory = _new_memory()
    _ensure_goal_anchor(
        memory,
        [{"role": "user", "content": "帮我做一个股票筛选智能体"}],
        conversation_id="pivot-session",
    )
    memory["requirement_summary"] = "股票筛选智能体"

    _merge_memory_update(
        memory,
        {"requirement_summary": "处理售后工单的客服智能体"},
        recent_user_text="需求改成客服智能体，负责售后工单",
        source_turn={"message_id": "pivot-msg-1"},
        transaction_time="2026-07-17T03:02:00Z",
    )

    assert memory["requirement_summary"] == "处理售后工单的客服智能体"
    decision = memory["memory_update_audit"][-1]
    assert decision["decision"] == "accepted"
    assert decision["reason"] == "explicit_user_pivot"
    assert decision["source_turn"] == {"message_id": "pivot-msg-1"}


def test_pivot_marker_does_not_authorize_an_unrelated_candidate():
    memory = _new_memory()
    _ensure_goal_anchor(
        memory,
        [{"role": "user", "content": "帮我做一个股票筛选智能体"}],
        conversation_id="false-pivot-session",
    )
    memory["requirement_summary"] = "股票筛选智能体"

    _merge_memory_update(
        memory,
        {"requirement_summary": "处理售后工单的客服智能体"},
        recent_user_text="请重新设计股票筛选智能体的行情模块",
        source_turn="false-pivot-turn",
    )

    assert memory["requirement_summary"] == "股票筛选智能体"
    assert memory["memory_update_audit"][-1]["decision"] == "rejected"


def test_list_candidates_are_additive_and_normalized_deduplicated():
    memory = _new_memory()
    memory["confirmed_constraints"] = ["仅使用中文", {"region": "CN"}]

    _merge_memory_update(
        memory,
        {
            "confirmed_constraints": [
                "  仅使用中文  ",
                {"region": "CN"},
                "运行在 Docker 沙箱",
            ]
        },
        source_turn="turn-3",
    )

    assert memory["confirmed_constraints"] == [
        "仅使用中文",
        {"region": "CN"},
        "运行在 Docker 沙箱",
    ]
    assert memory["memory_update_audit"][-1]["reason"] == "additive_deduplicated_merge"


def test_ready_candidate_requires_validated_proposal():
    memory = _new_memory()

    _merge_memory_update(
        memory,
        {"apply_readiness": {"status": "ready", "missing": []}},
        source_turn="turn-ready-1",
        has_validated_proposal=False,
    )

    assert memory["apply_readiness"]["status"] == "not_ready"
    assert "validated_proposal" in memory["apply_readiness"]["missing"]
    assert memory["memory_update_audit"][-1]["decision"] == "rejected"
    assert memory["memory_update_audit"][-1]["reason"] == "validated_proposal_required"

    _merge_memory_update(
        memory,
        {"apply_readiness": {"status": "ready", "missing": []}},
        source_turn="turn-ready-2",
        has_validated_proposal=True,
    )

    assert memory["apply_readiness"]["status"] == "ready"
    assert memory["memory_update_audit"][-1]["decision"] == "accepted"
    assert memory["memory_update_audit"][-1]["reason"] == "validated_proposal_present"


def test_audit_candidate_redacts_credentials():
    memory = _new_memory()

    _merge_memory_update(
        memory,
        {"runtime_strategy": {"api_key": "sk-super-secret-value", "model": "qwen"}},
        source_turn="turn-secret",
    )

    audit_text = memory["memory_update_audit"][-1]["candidate_summary"]
    assert "sk-super-secret-value" not in audit_text
    assert "[REDACTED]" in audit_text


def test_audit_candidate_redacts_bearer_phone_and_email():
    memory = _new_memory()

    _merge_memory_update(
        memory,
        {
            "confirmed_constraints": [
                "Authorization: Bearer abcdefghijklmnopqrstuvwxyz",
                "联系 13812345678 或 owner@example.com",
            ]
        },
        source_turn="turn-sensitive",
    )

    audit_text = memory["memory_update_audit"][-1]["candidate_summary"]
    assert "abcdefghijklmnopqrstuvwxyz" not in audit_text
    assert "13812345678" not in audit_text
    assert "owner@example.com" not in audit_text
    assert "[REDACTED_PHONE]" in audit_text
    assert "[REDACTED_EMAIL]" in audit_text


def test_unknown_domain_summary_without_user_evidence_fails_closed():
    memory = _new_memory()
    _ensure_goal_anchor(
        memory,
        [{"role": "user", "content": "设计一个合同条款风险审阅智能体"}],
        conversation_id="contract-session",
    )
    memory["requirement_summary"] = "审阅合同条款并识别风险"

    _merge_memory_update(
        memory,
        {"requirement_summary": "安排办公室绿植养护和浇水计划"},
        recent_user_text="请继续",
        source_turn="turn-unrelated",
    )

    assert memory["requirement_summary"] == "审阅合同条款并识别风险"
    assert memory["memory_update_audit"][-1]["decision"] == "rejected"


def test_legacy_session_lazily_derives_and_persists_anchor():
    conv_id = "legacy-anchor-session"
    with Session(planner_sessions.engine) as session:
        row = PlannerSession(
            conversation_id=conv_id,
            session_title="老选股会话",
            stage="drafting",
            user_request="帮我创建一个选股智能体",
            requirement_summary="股票筛选助手",
            planner_messages=json.dumps(
                [{"id": "legacy-msg-1", "role": "user", "content": "帮我创建一个选股智能体"}],
                ensure_ascii=False,
            ),
        )
        session.add(row)
        session.commit()

    loaded = planner_sessions._load_session_into_memory(conv_id)

    assert loaded is not None
    anchor = loaded["memory"]["goal_anchor"]
    assert anchor["text"] == "帮我创建一个选股智能体"
    assert anchor["source"]["message_id"] == "legacy-msg-1"
    assert anchor["source"]["conversation_id"] == conv_id

    planner_sessions._persist_session(conv_id, loaded)
    loaded_again = planner_sessions._load_session_into_memory(conv_id)
    assert loaded_again is not None
    assert loaded_again["memory"]["goal_anchor"] == anchor


def test_legacy_ready_without_validated_proposal_is_downgraded():
    conv_id = "legacy-hallucinated-ready"
    with Session(planner_sessions.engine) as session:
        row = PlannerSession(
            conversation_id=conv_id,
            user_request="创建一个合同审阅智能体",
            planner_messages=json.dumps(
                [{"role": "user", "content": "创建一个合同审阅智能体"}],
                ensure_ascii=False,
            ),
            apply_readiness_json=json.dumps(
                {"status": "ready", "missing": [], "recommendation": "直接创建"},
                ensure_ascii=False,
            ),
        )
        session.add(row)
        session.commit()

    loaded = planner_sessions._load_session_into_memory(conv_id)

    assert loaded is not None
    assert loaded["memory"]["apply_readiness"]["status"] == "not_ready"
    assert "validated_proposal" in loaded["memory"]["apply_readiness"]["missing"]
    assert loaded["stage"] != "ready_to_apply"
    audit = loaded["memory"]["memory_update_audit"][-1]
    assert audit["decision"] == "rejected"
    assert audit["reason"] == "validated_proposal_required"


def test_memory_metadata_remains_conversation_isolated():
    first_id = "memory-isolation-stock"
    second_id = "memory-isolation-service"
    first = _new_memory()
    second = _new_memory()
    _ensure_goal_anchor(
        first,
        [{"role": "user", "content": "创建股票筛选智能体"}],
        conversation_id=first_id,
    )
    _ensure_goal_anchor(
        second,
        [{"role": "user", "content": "创建售后客服智能体"}],
        conversation_id=second_id,
    )
    _merge_memory_update(
        first,
        {"requirement_summary": "股票行情与筛选智能体"},
        recent_user_text="创建股票筛选智能体",
        source_turn="stock-turn",
    )
    _merge_memory_update(
        second,
        {"requirement_summary": "售后客服工单智能体"},
        recent_user_text="创建售后客服智能体",
        source_turn="service-turn",
    )
    planner_sessions._persist_session(
        first_id,
        {"memory": first, "messages": [{"role": "user", "content": "创建股票筛选智能体"}]},
    )
    planner_sessions._persist_session(
        second_id,
        {"memory": second, "messages": [{"role": "user", "content": "创建售后客服智能体"}]},
    )

    loaded_first = planner_sessions._load_session_into_memory(first_id)
    loaded_second = planner_sessions._load_session_into_memory(second_id)

    assert loaded_first is not None and loaded_second is not None
    assert loaded_first["memory"]["goal_anchor"]["source"]["conversation_id"] == first_id
    assert loaded_second["memory"]["goal_anchor"]["source"]["conversation_id"] == second_id
    assert loaded_first["memory"]["requirement_summary"] == "股票行情与筛选智能体"
    assert loaded_second["memory"]["requirement_summary"] == "售后客服工单智能体"
    assert loaded_first["memory"]["memory_update_audit"][-1]["source_turn"] == "stock-turn"
    assert loaded_second["memory"]["memory_update_audit"][-1]["source_turn"] == "service-turn"
