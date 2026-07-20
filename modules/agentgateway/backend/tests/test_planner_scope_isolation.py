"""Planner session tenant/workspace/user isolation acceptance tests."""

from __future__ import annotations

import json

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlmodel import Session, select
from starlette.websockets import WebSocketDisconnect

from app.api.planner import apply as planner_apply
from app.api.planner import sessions as planner_sessions
from app.api.planner.scope import PlannerScope
from app.api.planner.state import _new_memory
from app.main import app
from app.models.db import Agent, ArchitectureProposal, PlannerSession


SCOPE_A = PlannerScope(tenant_id="tenant-a", workspace_id="workspace-a", user_id="user-a")
SCOPE_B = PlannerScope(tenant_id="tenant-b", workspace_id="workspace-b", user_id="user-b")


def _conv_data(summary: str, scope: PlannerScope) -> dict:
    memory = _new_memory()
    memory["requirement_summary"] = summary
    return {
        "messages": [{"role": "user", "content": summary}],
        "memory": memory,
        "file_artifacts": [],
        "_scope": scope.as_dict(),
    }


def test_same_conversation_id_cannot_be_read_listed_or_written_cross_scope():
    conversation_id = "scope-isolation-read-write"
    planner_sessions._persist_session(
        conversation_id,
        _conv_data("租户 A 的选股智能体", SCOPE_A),
        SCOPE_A,
    )

    assert planner_sessions._load_session_into_memory(conversation_id, SCOPE_A) is not None
    assert planner_sessions._load_session_into_memory(conversation_id, SCOPE_B) is None
    assert [item.conversation_id for item in planner_sessions.list_planner_sessions(scope=SCOPE_A)] == [
        conversation_id
    ]
    assert planner_sessions.list_planner_sessions(scope=SCOPE_B) == []

    with pytest.raises(HTTPException) as denied_read:
        planner_sessions.get_planner_session(conversation_id, scope=SCOPE_B)
    assert denied_read.value.status_code == 404

    with pytest.raises(HTTPException) as denied_write:
        planner_sessions._persist_session(
            conversation_id,
            _conv_data("租户 B 的恶意覆盖", SCOPE_B),
            SCOPE_B,
        )
    assert denied_write.value.status_code == 404
    assert (
        planner_sessions.get_planner_session(conversation_id, scope=SCOPE_A)
        .memory["requirement_summary"]
        == "租户 A 的选股智能体"
    )


@pytest.mark.asyncio
async def test_same_conversation_id_cannot_be_deleted_cross_scope():
    conversation_id = "scope-isolation-delete"
    planner_sessions._persist_session(
        conversation_id,
        _conv_data("只能由 A 删除", SCOPE_A),
        SCOPE_A,
    )

    with pytest.raises(HTTPException) as denied:
        await planner_sessions.delete_planner_session(conversation_id, scope=SCOPE_B)
    assert denied.value.status_code == 404
    assert planner_sessions._load_session_into_memory(conversation_id, SCOPE_A) is not None


def test_apply_preflight_rejects_cross_scope_before_side_effects():
    conversation_id = "scope-isolation-apply"
    planner_sessions._persist_session(
        conversation_id,
        _conv_data("A 的待应用方案", SCOPE_A),
        SCOPE_A,
    )

    with pytest.raises(HTTPException) as denied:
        planner_apply._assert_conversation_scope(conversation_id, SCOPE_B)
    assert denied.value.status_code == 404


def test_from_agent_does_not_resume_another_scopes_session():
    with Session(planner_sessions.engine) as session:
        agent = Agent(name="scope-agent")
        session.add(agent)
        session.flush()
        proposal = ArchitectureProposal(
            agent_id=agent.id,
            trigger_type="create",
            user_request="创建客服智能体",
            requirement_summary="客服智能体",
            proposed_graph_json=json.dumps({"nodes": [], "edges": []}),
            status="applied",
        )
        session.add(proposal)
        session.commit()
        agent_id = agent.id

    first = planner_sessions.create_session_from_agent(agent_id, scope=SCOPE_A)
    second = planner_sessions.create_session_from_agent(agent_id, scope=SCOPE_B)

    assert first.conversation_id != second.conversation_id
    with Session(planner_sessions.engine) as session:
        rows = session.exec(
            select(PlannerSession).where(PlannerSession.linked_agent_id == agent_id)
        ).all()
    assert {
        (row.conversation_id, row.tenant_id, row.workspace_id, row.user_id)
        for row in rows
    } >= {
        (first.conversation_id, "tenant-a", "workspace-a", "user-a"),
        (second.conversation_id, "tenant-b", "workspace-b", "user-b"),
    }


def test_rest_and_websocket_headers_enforce_the_same_scope():
    conversation_id = "scope-isolation-transport"
    planner_sessions._persist_session(
        conversation_id,
        _conv_data("A 的传输层会话", SCOPE_A),
        SCOPE_A,
    )
    headers_b = {
        "X-Tenant-ID": SCOPE_B.tenant_id,
        "X-Workspace-ID": SCOPE_B.workspace_id,
        "X-User-ID": SCOPE_B.user_id,
    }
    client = TestClient(app)

    response = client.get(f"/api/planner/sessions/{conversation_id}", headers=headers_b)
    assert response.status_code == 404

    with pytest.raises(WebSocketDisconnect) as disconnected:
        with client.websocket_connect(
            f"/api/planner/ws/{conversation_id}",
            headers=headers_b,
        ) as websocket:
            websocket.receive_json()
    assert disconnected.value.code == 4404
