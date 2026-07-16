"""Backend tests for change: planner-process-stream-cleanup.

Asserts the de-duplication contract: a parent phase step (context_load /
memory_recall) no longer emits a "completed" message that repeats what its
sub-steps already say. Same FakeWebSocket harness as the granular-steps tests.
"""

from __future__ import annotations

import json
import pytest


class FakeWebSocket:
    def __init__(self, inbound):
        self._inbound = list(inbound)
        self.sent = []

    async def accept(self):
        return None

    async def receive_text(self):
        if not self._inbound:
            from fastapi import WebSocketDisconnect
            raise WebSocketDisconnect()
        return json.dumps(self._inbound.pop(0))

    async def send_json(self, payload):
        self.sent.append(payload)


def _script_stream(events):
    async def _gen(agent, user_message, attachments=None):
        for ev in events:
            yield ev
    return _gen


@pytest.mark.asyncio
async def test_no_duplicate_parent_content(monkeypatch):
    from app.api.planner import ws as planner_ws
    from app.api.planner import state as planner_state
    # Pin the single-call path (fixed-stage steps); clear agentic flags so a dev
    # .env with PLANNER_AGENTIC_ALL can't reroute this test into the agentic path
    # (which intentionally suppresses these fixed steps).
    monkeypatch.delenv("PLANNER_AGENTIC", raising=False)
    monkeypatch.delenv("PLANNER_AGENTIC_ALL", raising=False)
    monkeypatch.setattr(planner_ws, "create_agent", lambda **kw: object())
    monkeypatch.setattr(planner_ws, "_get_langfuse", lambda: None)
    monkeypatch.setattr(planner_ws, "_persist_session", lambda *a, **k: None)
    monkeypatch.setattr(planner_ws, "run_conversation", _script_stream([
        ("token", "你好。"),
        ("done", ""),
    ]))
    planner_state.store_conv("conv-cleanup", {
        "messages": [], "model": {"model_name": "qwen3.6-27b", "provider": "glm"},
        "memory": planner_state._new_memory(), "file_artifacts": [],
    })
    sock = FakeWebSocket([{"type": "message", "content": "做个 sales 销售日报 agent"}])
    await planner_ws.planner_websocket(sock, "conv-cleanup")

    run_events = [m for m in sock.sent if m.get("type") == "run_event"]
    by_step = {e["step"]: e for e in run_events}

    # Parent steps still exist (as group headers / status), but carry NO message
    # that duplicates sub-step content.
    parent_ctx = by_step.get("context_load")
    parent_mem = by_step.get("memory_recall")
    # the completed parent emits exist with empty/no message
    ctx_completed = [e for e in run_events if e["step"] == "context_load" and e["status"] == "completed"]
    mem_completed = [e for e in run_events if e["step"] == "memory_recall" and e["status"] == "completed"]
    assert ctx_completed and not (ctx_completed[-1].get("message") or "").strip(), "context_load parent must not repeat sub-step content"
    assert mem_completed and not (mem_completed[-1].get("message") or "").strip(), "memory_recall parent must not repeat sub-step content"

    # "可用能力 N 项 / N 项能力" must appear at most once across all messages
    msgs = [e.get("message") or "" for e in run_events]
    cap_lines = [m for m in msgs if ("项能力" in m or "可用能力" in m)]
    assert len(cap_lines) <= 1, f"capability count duplicated: {cap_lines}"

    # "无长期记忆" must appear at most once (sub-step only, not parent+child)
    no_mem = [m for m in msgs if "无长期记忆" in m or "无相关长期记忆" in m]
    assert len(no_mem) <= 1, f"no-memory message duplicated: {no_mem}"

    # sub-steps themselves still present (de-dup removed only the parent total)
    assert "context_load:internal_context" in by_step


@pytest.mark.asyncio
async def test_agentic_mode_suppresses_fixed_steps(monkeypatch):
    # In agentic mode the planner gathers context via its OWN tools, so the fixed
    # pre-LLM stage steps (intent_inference / context_load:* / memory_recall:*)
    # must NOT be emitted — only the real tool_call:* steps from the loop appear.
    from app.api.planner import ws as planner_ws
    from app.api.planner import state as planner_state

    monkeypatch.setenv("PLANNER_AGENTIC", "on")
    monkeypatch.setenv("PLANNER_AGENTIC_ALL", "on")  # force any model onto agentic
    monkeypatch.setattr(planner_ws, "create_agent", lambda **kw: object())
    monkeypatch.setattr(planner_ws, "_get_langfuse", lambda: None)
    monkeypatch.setattr(planner_ws, "_persist_session", lambda *a, **k: None)

    async def _fake_agentic(*, run_emitter, **kw):
        # Emit a couple of real tool steps, as the loop would.
        await run_emitter.emit("tool_call:read_user_profile", "running", message="正在读取用户画像")
        await run_emitter.emit("tool_call:read_user_profile", "completed", message="读取用户画像完成")
        return {"full_response": "你好，这是回答。", "turn_state": {}}

    # run_agentic_turn is lazily imported from its source module inside ws.py.
    import app.core.planner_agent_loop as _loop
    monkeypatch.setattr(_loop, "run_agentic_turn", _fake_agentic)

    planner_state.store_conv("conv-agentic", {
        "messages": [], "model": {"model_name": "glm-4.7-flash", "provider": "glm"},
        "memory": planner_state._new_memory(), "file_artifacts": [],
    })
    sock = FakeWebSocket([{"type": "message", "content": "我是谁"}])
    await planner_ws.planner_websocket(sock, "conv-agentic")

    run_events = [m for m in sock.sent if m.get("type") == "run_event"]
    steps = [e["step"] for e in run_events]
    # Fixed pre-LLM stage steps are suppressed in agentic mode.
    assert not any(s == "intent_inference" for s in steps), steps
    assert not any(s.startswith("context_load") for s in steps), steps
    assert not any(s.startswith("memory_recall") for s in steps), steps
    # Real tool calls ARE surfaced.
    assert any(s == "tool_call:read_user_profile" for s in steps), steps
    # A dynamic reasoning opener replaces the fixed 理解需求 — emitted and closed.
    reasoning = [e for e in run_events if e["step"] == "reasoning"]
    assert reasoning, "agentic turn must emit a dynamic reasoning opener"
    assert any(e["status"] == "completed" for e in reasoning), "reasoning must be closed"
    # The coarse activity script (理解需求/起草回复/生成方案/等待确认) is suppressed
    # in agentic mode — the process stream is driven by real actions, not a script.
    activities = [m for m in sock.sent if m.get("type") == "activity"]
    assert activities == [], f"agentic mode must not emit the fixed activity script: {activities}"


@pytest.mark.asyncio
async def test_single_call_still_emits_activity_script(monkeypatch):
    # Regression guard: with agentic OFF, the coarse activity script (理解需求…)
    # is still emitted as before — this change only affects the agentic path.
    from app.api.planner import ws as planner_ws
    from app.api.planner import state as planner_state

    monkeypatch.setenv("PLANNER_AGENTIC", "off")
    monkeypatch.delenv("PLANNER_AGENTIC_ALL", raising=False)
    monkeypatch.setattr(planner_ws, "create_agent", lambda **kw: object())
    monkeypatch.setattr(planner_ws, "_get_langfuse", lambda: None)
    monkeypatch.setattr(planner_ws, "_persist_session", lambda *a, **k: None)
    monkeypatch.setattr(planner_ws, "run_conversation", _script_stream([
        ("token", "你好。"),
        ("done", ""),
    ]))
    planner_state.store_conv("conv-single", {
        "messages": [], "model": {"model_name": "qwen3.6-27b", "provider": "glm"},
        "memory": planner_state._new_memory(), "file_artifacts": [],
    })
    sock = FakeWebSocket([{"type": "message", "content": "做个 agent"}])
    await planner_ws.planner_websocket(sock, "conv-single")

    activities = [m for m in sock.sent if m.get("type") == "activity"]
    labels = {m.get("label") for m in activities}
    assert "理解需求" in labels, f"single-call path must still emit the activity script: {labels}"
    # And NO dynamic reasoning step on the single-call path.
    run_events = [m for m in sock.sent if m.get("type") == "run_event"]
    assert not any(e["step"] == "reasoning" for e in run_events)
