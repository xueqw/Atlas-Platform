from __future__ import annotations

import json

import pytest


class _FakeHTTPResponse:
    def __init__(self, lines, *, content_type: str = "text/event-stream"):
        self._lines = list(lines)
        self.headers = {"content-type": content_type}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    def raise_for_status(self):
        return None

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class _FakeHTTPClient:
    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    def stream(self, *args, **kwargs):
        return self._response


async def _collect(stream):
    out = []
    async for event in stream:
        out.append(event)
    return out


@pytest.mark.asyncio
async def test_openai_adapter_recovers_non_sse_json_completion(monkeypatch):
    import httpx
    from app.core.agentscope_runner import _run_openai_compatible_stream

    response = _FakeHTTPResponse(
        [
            json.dumps(
                {
                    "choices": [
                        {
                            "message": {"content": "恢复后的方案"},
                            "finish_reason": "stop",
                        }
                    ]
                },
                ensure_ascii=False,
            )
        ],
        content_type="application/json",
    )
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: _FakeHTTPClient(response),
    )

    events = await _collect(
        _run_openai_compatible_stream(
            base_url="https://provider.invalid/v1",
            api_key="test-only",
            model_name="qwen-test",
            system_prompt="system",
            user_content="user",
            temperature=0.1,
            max_tokens=32,
        )
    )

    assert ("token", "恢复后的方案") in events
    done_meta = json.loads(events[-1][1])
    assert events[-1][0] == "done"
    assert done_meta == {
        "finish_reason": "stop",
        "response_mode": "json",
        "visible_chars": len("恢复后的方案"),
    }


@pytest.mark.asyncio
async def test_openai_adapter_empty_sse_raises_without_done(monkeypatch):
    import httpx
    from app.core.agentscope_runner import (
        EmptyModelResponse,
        _run_openai_compatible_stream,
    )

    response = _FakeHTTPResponse(
        [
            'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
            "data: [DONE]",
        ]
    )
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: _FakeHTTPClient(response),
    )

    with pytest.raises(EmptyModelResponse) as caught:
        await _collect(
            _run_openai_compatible_stream(
                base_url="https://provider.invalid/v1",
                api_key="test-only",
                model_name="qwen-test",
                system_prompt="system",
                user_content="user",
                temperature=0.1,
                max_tokens=32,
            )
        )

    assert caught.value.finish_reason == "stop"
    assert caught.value.response_mode == "sse"
    assert caught.value.reason == "empty_content"


class _FakeWebSocket:
    def __init__(self, inbound):
        self._inbound = list(inbound)
        self.sent = []
        self.headers = {}
        self.closed = None

    async def accept(self):
        return None

    async def receive_text(self):
        if not self._inbound:
            from fastapi import WebSocketDisconnect

            raise WebSocketDisconnect()
        return json.dumps(self._inbound.pop(0), ensure_ascii=False)

    async def send_json(self, payload):
        self.sent.append(payload)


class _DisconnectBeforeAckWebSocket(_FakeWebSocket):
    async def send_json(self, payload):
        if payload.get("type") == "a2ui_recorded":
            from fastapi import WebSocketDisconnect

            raise WebSocketDisconnect()
        await super().send_json(payload)


class _DisconnectAfterClaimWebSocket(_FakeWebSocket):
    async def send_json(self, payload):
        if payload.get("type") == "continuation_claimed":
            from fastapi import WebSocketDisconnect

            raise WebSocketDisconnect()
        await super().send_json(payload)


def _store_pending_confirmation(planner_state, conversation_id: str) -> dict:
    memory = planner_state._new_memory()
    pending = {
        "id": "confirm-recover",
        "topic": "创建确认",
        "prompt": "继续创建？",
        "options": [
            {"id": "confirm", "label": "确认创建"},
            {"id": "revise", "label": "修改"},
        ],
    }
    planner_state.store_conv(
        conversation_id,
        {
            "messages": [{"role": "assistant", "content": "请确认方案"}],
            "model": {"model_name": "qwen-test", "provider": "glm"},
            "memory": memory,
            "pending_a2ui": pending,
            "file_artifacts": [],
        },
    )
    return memory

    async def close(self, code=1000, reason=""):
        self.closed = {"code": code, "reason": reason}


def _configure_single_call(monkeypatch, planner_ws):
    monkeypatch.setenv("PLANNER_AGENTIC", "off")
    monkeypatch.setenv("PLANNER_PLAN_LOOP", "off")
    monkeypatch.setattr(planner_ws, "create_agent", lambda **kwargs: object())
    monkeypatch.setattr(planner_ws, "_get_langfuse", lambda: None)
    monkeypatch.setattr(planner_ws, "_persist_session", lambda *args, **kwargs: None)


@pytest.mark.asyncio
async def test_empty_response_retries_once_same_run_and_succeeds(monkeypatch):
    from app.api.planner import state as planner_state
    from app.api.planner import ws as planner_ws

    _configure_single_call(monkeypatch, planner_ws)
    calls = 0

    async def scripted(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            yield ("done", "")
            return
        yield ("token", "已继续生成方案")
        yield ("done", "")

    monkeypatch.setattr(planner_ws, "run_conversation", scripted)
    conversation_id = "retry-success"
    planner_state.store_conv(
        conversation_id,
        {
            "messages": [],
            "model": {"model_name": "qwen-test", "provider": "glm"},
            "memory": planner_state._new_memory(),
            "file_artifacts": [],
        },
    )
    socket = _FakeWebSocket([{"type": "message", "content": "继续创建"}])

    await planner_ws.planner_websocket(socket, conversation_id)

    assert calls == 2
    restored = planner_state.load_conv(conversation_id)
    assert [m["role"] for m in restored["messages"]] == ["user", "assistant"]
    assert restored["messages"][-1]["content"] == "已继续生成方案"
    assert any(item.get("type") == "done" for item in socket.sent)
    assert not any(item.get("type") == "error" for item in socket.sent)
    failures = [
        item
        for item in socket.sent
        if item.get("type") == "run_event"
        and item.get("step") == "model_response"
        and item.get("status") == "failed"
    ]
    assert len(failures) == 1
    assert failures[0]["details"]["attempt"] == 1
    run_ids = {
        item["run_id"]
        for item in socket.sent
        if item.get("type") == "run_event" and item.get("step", "").startswith("model_response")
    }
    assert len(run_ids) == 1


@pytest.mark.asyncio
async def test_exhausted_empty_response_has_no_assistant_and_is_recoverable(monkeypatch):
    from app.api.planner import state as planner_state
    from app.api.planner import ws as planner_ws

    _configure_single_call(monkeypatch, planner_ws)
    calls = 0

    async def empty(*args, **kwargs):
        nonlocal calls
        calls += 1
        yield ("done", "")

    monkeypatch.setattr(planner_ws, "run_conversation", empty)
    conversation_id = "retry-exhausted"
    planner_state.store_conv(
        conversation_id,
        {
            "messages": [],
            "model": {"model_name": "qwen-test", "provider": "glm"},
            "memory": planner_state._new_memory(),
            "file_artifacts": [],
        },
    )
    socket = _FakeWebSocket([{"type": "message", "content": "继续创建"}])

    await planner_ws.planner_websocket(socket, conversation_id)

    assert calls == 2
    restored = planner_state.load_conv(conversation_id)
    assert [m["role"] for m in restored["messages"]] == ["user"]
    errors = [item for item in socket.sent if item.get("type") == "error"]
    assert errors[-1]["code"] == "empty_model_response"
    assert errors[-1]["recoverable"] is True
    assert not any(item.get("type") == "done" for item in socket.sent)
    attempts = [
        item["details"]["attempt"]
        for item in socket.sent
        if item.get("type") == "run_event"
        and item.get("step") == "model_response"
        and item.get("status") == "failed"
    ]
    assert attempts == [1, 2]


@pytest.mark.asyncio
async def test_duplicate_a2ui_is_acknowledged_once_and_conflict_rejected(monkeypatch):
    from app.api.planner import state as planner_state
    from app.api.planner import ws as planner_ws

    monkeypatch.setattr(planner_ws, "_persist_session", lambda *args, **kwargs: None)
    writes = []
    monkeypatch.setattr(
        planner_ws.planner_files,
        "append_decision",
        lambda *args, **kwargs: writes.append(kwargs) or {
            "path": "decisions.md",
            "filename": "decisions.md",
            "kind": "decisions",
            "summary": "ok",
        },
    )
    conversation_id = "a2ui-idempotent"
    memory = planner_state._new_memory()
    pending = {
        "id": "confirm-1",
        "topic": "创建确认",
        "prompt": "继续创建？",
        "options": [
            {"id": "confirm", "label": "确认创建"},
            {"id": "revise", "label": "修改"},
        ],
    }
    planner_state.store_conv(
        conversation_id,
        {
            "messages": [{"role": "assistant", "content": "请确认方案"}],
            "model": {"model_name": "qwen-test", "provider": "glm"},
            "memory": memory,
            "pending_a2ui": pending,
            "file_artifacts": [],
        },
    )
    socket = _FakeWebSocket(
        [
            {"type": "a2ui_response", "id": "confirm-1", "choice": "confirm"},
            {"type": "a2ui_response", "id": "confirm-1", "choice": "confirm"},
            {"type": "a2ui_response", "id": "confirm-1", "choice": "revise"},
        ]
    )

    await planner_ws.planner_websocket(socket, conversation_id)

    assert len(writes) == 1
    assert len(memory["decisions_confirmed"]) == 1
    acks = [item for item in socket.sent if item.get("type") == "a2ui_recorded"]
    assert [item["duplicate"] for item in acks] == [False, True]
    assert acks[0]["continuation_token"] == acks[1]["continuation_token"]
    conflicts = [
        item for item in socket.sent if item.get("code") == "a2ui_decision_conflict"
    ]
    assert len(conflicts) == 1


@pytest.mark.asyncio
async def test_disconnect_before_a2ui_ack_restores_pending_continuation(monkeypatch):
    """Decision persistence wins even when the ACK frame never reaches the UI."""
    from app.api.planner import state as planner_state
    from app.api.planner import ws as planner_ws

    monkeypatch.setattr(planner_ws, "_persist_session", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        planner_ws.planner_files,
        "append_decision",
        lambda *args, **kwargs: {
            "path": "decisions.md",
            "filename": "decisions.md",
            "kind": "decisions",
            "summary": "ok",
        },
    )
    conversation_id = "a2ui-disconnect-before-ack"
    _store_pending_confirmation(planner_state, conversation_id)
    first = _DisconnectBeforeAckWebSocket([
        {"type": "a2ui_response", "id": "confirm-recover", "choice": "confirm"},
    ])

    await planner_ws.planner_websocket(first, conversation_id)

    durable = planner_state.load_conv(conversation_id)["memory"]["pending_continuation"]
    assert durable["status"] == "pending"
    assert durable["request_id"] == "confirm-recover"
    assert durable["choice"] == "confirm"

    reconnect = _FakeWebSocket([])
    await planner_ws.planner_websocket(reconnect, conversation_id)
    restored = [
        item for item in reconnect.sent if item.get("type") == "session_restored"
    ]
    assert restored[-1]["pending_continuation"]["token"] == durable["token"]


@pytest.mark.asyncio
async def test_disconnect_after_ack_before_send_can_resume_pending_continuation(monkeypatch):
    """A fresh socket can resume the durable command after the ACK-only socket dies."""
    from app.api.planner import state as planner_state
    from app.api.planner import ws as planner_ws

    _configure_single_call(monkeypatch, planner_ws)
    monkeypatch.setattr(
        planner_ws.planner_files,
        "append_decision",
        lambda *args, **kwargs: {
            "path": "decisions.md",
            "filename": "decisions.md",
            "kind": "decisions",
            "summary": "ok",
        },
    )
    calls = 0

    async def scripted(*args, **kwargs):
        nonlocal calls
        calls += 1
        yield ("token", "续跑完成")
        yield ("done", "")

    monkeypatch.setattr(planner_ws, "run_conversation", scripted)
    conversation_id = "a2ui-disconnect-after-ack"
    _store_pending_confirmation(planner_state, conversation_id)
    ack_socket = _FakeWebSocket([
        {"type": "a2ui_response", "id": "confirm-recover", "choice": "confirm"},
    ])
    await planner_ws.planner_websocket(ack_socket, conversation_id)
    ack = next(item for item in ack_socket.sent if item.get("type") == "a2ui_recorded")

    resume_socket = _FakeWebSocket([{
        "type": "message",
        "content": "untrusted replacement",
        "a2ui_request_id": "confirm-recover",
        "a2ui_choice": "confirm",
        "continuation_token": ack["continuation_token"],
    }])
    await planner_ws.planner_websocket(resume_socket, conversation_id)

    restored = planner_state.load_conv(conversation_id)
    assert calls == 1
    assert restored["memory"]["pending_continuation"]["status"] == "completed"
    assert restored["messages"][-2]["content"] == "已选择「确认创建」"
    assert restored["messages"][-1]["content"] == "续跑完成"


@pytest.mark.asyncio
async def test_duplicate_continuation_token_runs_model_exactly_once(monkeypatch):
    from app.api.planner import state as planner_state
    from app.api.planner import ws as planner_ws

    _configure_single_call(monkeypatch, planner_ws)
    monkeypatch.setattr(
        planner_ws.planner_files,
        "append_decision",
        lambda *args, **kwargs: {
            "path": "decisions.md",
            "filename": "decisions.md",
            "kind": "decisions",
            "summary": "ok",
        },
    )
    calls = 0

    async def scripted(*args, **kwargs):
        nonlocal calls
        calls += 1
        yield ("token", "只执行一次")
        yield ("done", "")

    monkeypatch.setattr(planner_ws, "run_conversation", scripted)
    conversation_id = "a2ui-continuation-exactly-once"
    _store_pending_confirmation(planner_state, conversation_id)
    ack_socket = _FakeWebSocket([
        {"type": "a2ui_response", "id": "confirm-recover", "choice": "confirm"},
    ])
    await planner_ws.planner_websocket(ack_socket, conversation_id)
    token = next(
        item["continuation_token"]
        for item in ack_socket.sent
        if item.get("type") == "a2ui_recorded"
    )
    continuation = {
        "type": "message",
        "content": "已选择「确认创建」",
        "a2ui_request_id": "confirm-recover",
        "a2ui_choice": "confirm",
        "continuation_token": token,
    }
    socket = _FakeWebSocket([continuation, continuation])

    await planner_ws.planner_websocket(socket, conversation_id)

    restored = planner_state.load_conv(conversation_id)
    assert calls == 1
    assert len([m for m in restored["messages"] if m["role"] == "user"]) == 1
    claimed = [
        item for item in socket.sent if item.get("type") == "continuation_claimed"
    ]
    completed = [
        item for item in socket.sent if item.get("type") == "continuation_completed"
    ]
    assert [item["duplicate"] for item in claimed] == [False]
    assert [item["duplicate"] for item in completed] == [False, True]


@pytest.mark.asyncio
async def test_disconnect_immediately_after_claim_replays_without_duplicate_turn(monkeypatch):
    from app.api.planner import state as planner_state
    from app.api.planner import ws as planner_ws

    _configure_single_call(monkeypatch, planner_ws)
    monkeypatch.setattr(
        planner_ws.planner_files,
        "append_decision",
        lambda *args, **kwargs: {
            "path": "decisions.md",
            "filename": "decisions.md",
            "kind": "decisions",
            "summary": "ok",
        },
    )
    calls = 0

    async def scripted(*args, **kwargs):
        nonlocal calls
        calls += 1
        yield ("token", "恢复后完成")
        yield ("done", "")

    monkeypatch.setattr(planner_ws, "run_conversation", scripted)
    conversation_id = "a2ui-crash-after-claim"
    _store_pending_confirmation(planner_state, conversation_id)
    ack_socket = _FakeWebSocket([
        {"type": "a2ui_response", "id": "confirm-recover", "choice": "confirm"},
    ])
    await planner_ws.planner_websocket(ack_socket, conversation_id)
    token = next(
        item["continuation_token"]
        for item in ack_socket.sent
        if item.get("type") == "a2ui_recorded"
    )
    continuation = {
        "type": "message",
        "content": "ignored",
        "a2ui_request_id": "confirm-recover",
        "a2ui_choice": "confirm",
        "continuation_token": token,
    }

    await planner_ws.planner_websocket(
        _DisconnectAfterClaimWebSocket([continuation]), conversation_id
    )
    interrupted = planner_state.load_conv(conversation_id)
    assert interrupted["memory"]["pending_continuation"]["status"] == "processing"

    await planner_ws.planner_websocket(_FakeWebSocket([continuation]), conversation_id)
    restored = planner_state.load_conv(conversation_id)
    assert calls == 1
    assert len([
        m for m in restored["messages"]
        if m.get("role") == "user" and m.get("continuation_token") == token
    ]) == 1
    assert len([
        m for m in restored["messages"]
        if m.get("role") == "assistant" and m.get("continuation_token") == token
    ]) == 1
    assert restored["memory"]["pending_continuation"]["status"] == "completed"


@pytest.mark.asyncio
async def test_interrupted_model_replays_one_committed_turn_without_duplicate_user(monkeypatch):
    from app.api.planner import state as planner_state
    from app.api.planner import ws as planner_ws

    _configure_single_call(monkeypatch, planner_ws)
    monkeypatch.setattr(
        planner_ws.planner_files,
        "append_decision",
        lambda *args, **kwargs: {
            "path": "decisions.md",
            "filename": "decisions.md",
            "kind": "decisions",
            "summary": "ok",
        },
    )
    calls = 0

    async def interrupted_then_success(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("simulated provider interruption")
        yield ("token", "恢复后的唯一提交结果")
        yield ("done", "")

    monkeypatch.setattr(planner_ws, "run_conversation", interrupted_then_success)
    conversation_id = "a2ui-model-interruption"
    _store_pending_confirmation(planner_state, conversation_id)
    ack_socket = _FakeWebSocket([
        {"type": "a2ui_response", "id": "confirm-recover", "choice": "confirm"},
    ])
    await planner_ws.planner_websocket(ack_socket, conversation_id)
    token = next(
        item["continuation_token"]
        for item in ack_socket.sent
        if item.get("type") == "a2ui_recorded"
    )
    continuation = {
        "type": "message",
        "content": "ignored",
        "a2ui_request_id": "confirm-recover",
        "a2ui_choice": "confirm",
        "continuation_token": token,
    }

    await planner_ws.planner_websocket(_FakeWebSocket([continuation]), conversation_id)
    interrupted = planner_state.load_conv(conversation_id)
    run_id = interrupted["memory"]["pending_continuation"]["run_id"]
    assert interrupted["memory"]["pending_continuation"]["status"] == "processing"

    await planner_ws.planner_websocket(_FakeWebSocket([continuation]), conversation_id)
    restored = planner_state.load_conv(conversation_id)
    assert calls == 2  # one interrupted attempt, one successful replay
    assert len([
        m for m in restored["messages"]
        if m.get("role") == "user" and m.get("continuation_token") == token
    ]) == 1
    assistants = [
        m for m in restored["messages"]
        if m.get("role") == "assistant" and m.get("continuation_token") == token
    ]
    assert len(assistants) == 1
    assert assistants[0]["continuation_run_id"] == run_id
    assert restored["memory"]["pending_continuation"]["status"] == "completed"


@pytest.mark.asyncio
async def test_process_restart_replays_processing_with_same_run_id_and_dedupes_user(monkeypatch):
    from sqlmodel import Session, select
    from app.api.planner import state as planner_state
    from app.api.planner import ws as planner_ws
    from app.api.planner.sessions import _persist_session
    from app.core.database import engine
    from app.models.db import PlannerSession

    monkeypatch.setenv("PLANNER_AGENTIC", "off")
    monkeypatch.setenv("PLANNER_PLAN_LOOP", "off")
    monkeypatch.setattr(planner_ws, "create_agent", lambda **kwargs: object())
    monkeypatch.setattr(planner_ws, "_get_langfuse", lambda: None)
    calls = 0

    async def scripted(*args, **kwargs):
        nonlocal calls
        calls += 1
        yield ("token", "重启恢复完成")
        yield ("done", "")

    monkeypatch.setattr(planner_ws, "run_conversation", scripted)
    conversation_id = "a2ui-process-restart"
    token = "restart-stable-token"
    run_id = "restart-stable-run"
    content = "已选择「确认创建」"
    memory = planner_state._new_memory()
    memory["pending_continuation"] = {
        "request_id": "confirm-restart",
        "choice": "confirm",
        "choice_label": "确认创建",
        "token": token,
        "content": content,
        "status": "processing",
        "run_id": run_id,
        "claimed_at": "2026-07-17T00:00:00+00:00",
        "lease_expires_at": "2026-07-17T00:00:30+00:00",
    }
    _persist_session(
        conversation_id,
        {
            "messages": [{
                "role": "user",
                "content": content,
                "continuation_token": token,
                "continuation_run_id": run_id,
            }],
            "memory": memory,
            "file_artifacts": [],
        },
    )
    planner_state.drop_conv(conversation_id)
    planner_ws._active_continuation_runs.discard(token)
    continuation = {
        "type": "message",
        "content": "ignored",
        "a2ui_request_id": "confirm-restart",
        "a2ui_choice": "confirm",
        "continuation_token": token,
    }
    try:
        await planner_ws.planner_websocket(
            _FakeWebSocket([continuation]), conversation_id
        )
        restored = planner_state.load_conv(conversation_id)
        assert calls == 1
        tagged_users = [
            m for m in restored["messages"]
            if m.get("role") == "user" and m.get("continuation_token") == token
        ]
        tagged_assistants = [
            m for m in restored["messages"]
            if m.get("role") == "assistant" and m.get("continuation_token") == token
        ]
        assert len(tagged_users) == 1
        assert len(tagged_assistants) == 1
        durable = restored["memory"]["pending_continuation"]
        assert durable["status"] == "completed"
        assert durable["run_id"] == run_id
    finally:
        planner_state.drop_conv(conversation_id)
        with Session(engine) as session:
            row = session.exec(
                select(PlannerSession).where(
                    PlannerSession.conversation_id == conversation_id
                )
            ).first()
            if row is not None:
                session.delete(row)
                session.commit()


def test_proposal_related_stages_require_structured_evidence():
    from app.api.planner.sessions import _infer_stage

    memory = {
        "requirement_summary": "做一个选股智能体",
        "confirmed_constraints": [],
        "latest_proposal_summary": "模型声称已生成",
    }
    assert _infer_stage(memory, None, False) == "drafting"
    assert (
        _infer_stage(
            memory,
            None,
            False,
            pending_a2ui={
                "id": "confirm-1",
                "options": [{"id": "confirm", "label": "确认"}],
            },
        )
        == "awaiting_confirmation"
    )
    assert _infer_stage(memory, {"architecture_summary": 123}, False) == "drafting"


def test_pending_continuation_round_trips_through_planner_session_row():
    from sqlmodel import Session, select
    from app.api.planner import state as planner_state
    from app.api.planner.sessions import _load_session_into_memory, _persist_session
    from app.core.database import engine
    from app.models.db import PlannerSession

    conversation_id = "pending-continuation-db-roundtrip"
    memory = planner_state._new_memory()
    memory["pending_continuation"] = {
        "request_id": "confirm-db",
        "choice": "confirm",
        "choice_label": "确认创建",
        "token": "durable-token",
        "content": "已选择「确认创建」",
        "status": "pending",
        "created_at": "2026-07-17T00:00:00+00:00",
    }
    _persist_session(
        conversation_id,
        {
            "messages": [{"role": "assistant", "content": "请确认"}],
            "memory": memory,
            "file_artifacts": [],
        },
    )
    try:
        restored = _load_session_into_memory(conversation_id)
        assert restored is not None
        assert restored["memory"]["pending_continuation"] == memory["pending_continuation"]
    finally:
        with Session(engine) as session:
            row = session.exec(
                select(PlannerSession).where(
                    PlannerSession.conversation_id == conversation_id
                )
            ).first()
            if row is not None:
                session.delete(row)
                session.commit()


def test_proposal_extraction_skips_earlier_non_proposal_json_fence():
    from app.api.planner.proposal import _try_extract_proposal

    text = """
当前规划记忆：
```json
{"requirement_summary": "智能选股助手"}
```
确认后的方案：
```json
{"ready": true, "proposal": {"architecture_summary": "多智能体选股", "nodes": [], "edges": []}}
```
"""

    assert _try_extract_proposal(text) == {
        "architecture_summary": "多智能体选股",
        "nodes": [],
        "edges": [],
    }


@pytest.mark.asyncio
async def test_agentic_empty_then_fallback_uses_two_total_attempts(monkeypatch):
    from app.api.planner import state as planner_state
    from app.api.planner import ws as planner_ws
    import app.core.planner_agent_loop as planner_agent_loop

    monkeypatch.setenv("PLANNER_AGENTIC", "on")
    monkeypatch.setenv("PLANNER_AGENTIC_ALL", "on")
    monkeypatch.setenv("PLANNER_PLAN_LOOP", "off")
    monkeypatch.setattr(planner_ws, "create_agent", lambda **kwargs: object())
    monkeypatch.setattr(planner_ws, "_get_langfuse", lambda: None)
    monkeypatch.setattr(planner_ws, "_persist_session", lambda *args, **kwargs: None)

    agentic_calls = 0
    fallback_calls = 0

    async def empty_agentic(**kwargs):
        nonlocal agentic_calls
        agentic_calls += 1
        return {"full_response": "", "turn_state": {}}

    async def empty_fallback(*args, **kwargs):
        nonlocal fallback_calls
        fallback_calls += 1
        yield ("done", "")

    monkeypatch.setattr(planner_agent_loop, "run_agentic_turn", empty_agentic)
    monkeypatch.setattr(planner_ws, "run_conversation", empty_fallback)
    conversation_id = "agentic-shared-budget"
    planner_state.store_conv(
        conversation_id,
        {
            "messages": [],
            "model": {"model_name": "qwen-test", "provider": "glm"},
            "memory": planner_state._new_memory(),
            "file_artifacts": [],
        },
    )
    socket = _FakeWebSocket([{"type": "message", "content": "创建一个分析智能体"}])

    await planner_ws.planner_websocket(socket, conversation_id)

    assert agentic_calls == 1
    assert fallback_calls == 1
    assert agentic_calls + fallback_calls == 2
    restored = planner_state.load_conv(conversation_id)
    assert [item["role"] for item in restored["messages"]] == ["user"]
    errors = [item for item in socket.sent if item.get("type") == "error"]
    assert errors[-1]["code"] == "empty_model_response"
    failures = [
        item
        for item in socket.sent
        if item.get("type") == "run_event"
        and item.get("step") == "model_response"
        and item.get("status") == "failed"
    ]
    assert [item["details"]["attempt"] for item in failures] == [1, 2]
    assert [item["details"]["path"] for item in failures] == [
        "agentic",
        "agentic_fallback",
    ]
    assert len({item["run_id"] for item in failures}) == 1


@pytest.mark.asyncio
async def test_plan_loop_empty_response_retries_once_with_same_run(monkeypatch):
    from app.api.planner import state as planner_state
    from app.api.planner import ws as planner_ws

    monkeypatch.setenv("PLANNER_AGENTIC", "off")
    monkeypatch.setenv("PLANNER_PLAN_LOOP", "on")
    monkeypatch.setattr(planner_ws, "create_agent", lambda **kwargs: object())
    monkeypatch.setattr(planner_ws, "_get_langfuse", lambda: None)
    calls = 0
    proposal = json.dumps(
        {
            "ready": True,
            "proposal": {
                "architecture_summary": "Plan+Loop 可靠方案",
                "nodes": [],
                "edges": [],
            },
        },
        ensure_ascii=False,
    )

    async def empty_then_proposal(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            yield ("done", "")
            return
        yield ("token", proposal)
        yield ("done", "")

    monkeypatch.setattr(planner_ws, "run_conversation", empty_then_proposal)
    conversation_id = "plan-loop-shared-budget"
    planner_state.store_conv(
        conversation_id,
        {
            "messages": [],
            "model": {"model_name": "qwen-test", "provider": "glm"},
            "memory": planner_state._new_memory(),
            "file_artifacts": [],
        },
    )
    socket = _FakeWebSocket(
        [{"type": "message", "content": "创建一个销售分析智能体"}]
    )

    await planner_ws.planner_websocket(socket, conversation_id)

    assert calls == 2
    failures = [
        item
        for item in socket.sent
        if item.get("type") == "run_event"
        and item.get("step") == "model_response"
        and item.get("status") == "failed"
    ]
    successes = [
        item
        for item in socket.sent
        if item.get("type") == "run_event"
        and item.get("step") == "model_response"
        and item.get("status") == "completed"
    ]
    assert [item["details"]["attempt"] for item in failures] == [1]
    assert failures[0]["details"]["path"] == "plan_loop"
    assert successes[-1]["details"]["attempt"] == 2
    assert successes[-1]["details"]["path"] == "plan_loop"
    assert failures[0]["run_id"] == successes[-1]["run_id"]
    assert not any(item.get("type") == "error" for item in socket.sent)
