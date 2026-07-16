"""Backend tests for change: add-planner-skill-transparency.

Drives the real planner WebSocket turn loop with a fake socket + a scripted
``run_conversation`` (same harness as test_planner_activity_events) to assert the
skill-transparency wire protocol without a live model. Covers spec
planner-skill-transparency:
- a proposal turn attributes the proposal skill on composing_proposal ONLY when
  it is mounted (in effective_skills); an A2UI turn attributes a2ui on
  awaiting_confirmation;
- a turn with neither action emits NO skill-attributed activity and an empty
  triggered_skills (honesty backstop);
- done carries triggered_skills; the persisted assistant message records it and
  survives a session restore;
- NO "本轮启用技能" turn-context list event is emitted (config layer is the
  skill panel's job, not the chat flow).
"""

from __future__ import annotations

import json

import pytest

from app.api.planner import ws as planner_ws
from app.api.planner import state as planner_state


class FakeWebSocket:
    """Minimal WebSocket double: scripts inbound messages, captures outbound."""

    def __init__(self, inbound: list[dict]):
        self._inbound = list(inbound)
        self.sent: list[dict] = []

    async def accept(self):
        return None

    async def receive_text(self) -> str:
        if not self._inbound:
            from fastapi import WebSocketDisconnect
            raise WebSocketDisconnect()
        return json.dumps(self._inbound.pop(0))

    async def send_json(self, payload: dict):
        self.sent.append(payload)


def _script_stream(events):
    async def _gen(agent, user_message, attachments=None):
        for ev in events:
            yield ev
    return _gen


def _of_type(sent: list[dict], t: str) -> list[dict]:
    return [m for m in sent if m.get("type") == t]


def _activities(sent: list[dict]) -> list[dict]:
    return _of_type(sent, "activity")


@pytest.fixture
def patched(monkeypatch):
    # Neutralize agent construction + Langfuse; we only test the event plumbing.
    monkeypatch.setattr(planner_ws, "create_agent", lambda **kw: object())
    monkeypatch.setattr(planner_ws, "_get_langfuse", lambda: None)
    return monkeypatch


def _seed_conv(cid: str, selected_skills=None):
    conv = {
        "messages": [],
        "model": {"model_name": "qwen3.6-27b", "provider": "glm"},
        "memory": planner_state._new_memory(),
        "file_artifacts": [],
    }
    if selected_skills:
        conv["memory"]["selected_skills"] = list(selected_skills)
    planner_state.store_conv(cid, conv)


# ─── no "本轮启用技能" list event (config layer is the panel's job) ───────────

@pytest.mark.asyncio
async def test_turn_emits_no_turn_context_list_event(patched):
    patched.setattr(planner_ws, "run_conversation", _script_stream([
        ("token", "你好，我理解你的需求了。" * 3),
        ("done", ""),
    ]))
    sock = FakeWebSocket([{"type": "message", "content": "做个客服机器人"}])
    await planner_ws.planner_websocket(sock, "conv-no-tc")

    # The reframed design must NOT re-list enabled skills in the chat flow.
    assert not _of_type(sock.sent, "planner_turn_context"), \
        "must not emit a 本轮启用技能 list event"


# ─── action-level skill TRIGGER attribution ─────────────────────────────────

@pytest.mark.asyncio
async def test_a2ui_turn_attributes_a2ui_on_confirmation(patched):
    a2ui = (
        "<a2ui_request>"
        + json.dumps({
            "id": "c1", "prompt": "确认关键约束？",
            "options": [{"id": "ok", "label": "确认"}],
        }, ensure_ascii=False)
        + "</a2ui_request>"
    )
    patched.setattr(planner_ws, "run_conversation", _script_stream([
        ("token", "我先和你确认几个关键点。" * 3),
        ("token", a2ui),
        ("done", ""),
    ]))
    sock = FakeWebSocket([{"type": "message", "content": "帮我设计"}])
    await planner_ws.planner_websocket(sock, "conv-a2ui")

    confirm = [a for a in _activities(sock.sent) if a["phase"] == "awaiting_confirmation"]
    assert confirm, "an a2ui turn must emit awaiting_confirmation activity"
    assert all(a.get("skill") == "a2ui" for a in confirm)
    assert all(a.get("skill_action") for a in confirm)
    # done credits a2ui as actually triggered.
    done = _of_type(sock.sent, "done")
    assert "a2ui" in done[-1].get("triggered_skills", [])


@pytest.mark.asyncio
async def test_proposal_turn_attributes_skill_only_when_mounted(patched):
    proposal = {
        "ready": True,
        "proposal": {"architecture_summary": "S", "rationale": "R", "nodes": [], "edges": []},
    }
    fenced = "```json\n" + json.dumps(proposal, ensure_ascii=False) + "\n```"
    script = [("token", "方案如下："), ("token", fenced), ("done", "")]

    # (a) proposal skill mounted → composing_proposal carries skill + triggered.
    patched.setattr(planner_ws, "run_conversation", _script_stream(script))
    cid = "conv-prop-mounted"
    _seed_conv(cid, selected_skills=["deerflow-planner"])
    sock = FakeWebSocket([{"type": "message", "content": "出方案"}])
    await planner_ws.planner_websocket(sock, cid)
    composing = [a for a in _activities(sock.sent) if a["phase"] == "composing_proposal"]
    assert composing, "proposal turn emits composing_proposal"
    assert any(a.get("skill") == "deerflow-planner" for a in composing)
    done = _of_type(sock.sent, "done")
    assert "deerflow-planner" in done[-1].get("triggered_skills", [])

    # (b) proposal skill NOT mounted → composing_proposal fires but carries no
    #     skill name, and triggered_skills stays empty (design D5).
    patched.setattr(planner_ws, "run_conversation", _script_stream(script))
    sock2 = FakeWebSocket([{"type": "message", "content": "出方案"}])
    await planner_ws.planner_websocket(sock2, "conv-prop-unmounted")
    composing2 = [a for a in _activities(sock2.sent) if a["phase"] == "composing_proposal"]
    assert composing2, "proposal turn still emits composing_proposal"
    assert all(a.get("skill") is None for a in composing2)
    done2 = _of_type(sock2.sent, "done")
    assert done2[-1].get("triggered_skills", []) == [], "unmounted proposal skill not credited"


@pytest.mark.asyncio
async def test_plain_turn_triggers_nothing(patched):
    # No proposal, no a2ui → honesty backstop: nothing claims a skill action and
    # triggered_skills is empty.
    patched.setattr(planner_ws, "run_conversation", _script_stream([
        ("token", "好的，我先了解一下背景信息。" * 3),
        ("done", ""),
    ]))
    sock = FakeWebSocket([{"type": "message", "content": "你好"}])
    await planner_ws.planner_websocket(sock, "conv-plain-skill")

    skilled = [a for a in _activities(sock.sent) if a.get("skill")]
    assert not skilled, f"plain turn must not attribute any skill action: {skilled}"
    done = _of_type(sock.sent, "done")
    assert done[-1].get("triggered_skills", []) == [], "plain turn triggers no skills"


# ─── result attribution: persistence/restore ────────────────────────────────

@pytest.mark.asyncio
async def test_assistant_message_records_triggered_skills_and_survives_restore(patched, monkeypatch):
    a2ui = (
        "<a2ui_request>"
        + json.dumps({"id": "c1", "prompt": "确认？", "options": [{"id": "ok", "label": "确认"}]},
                     ensure_ascii=False)
        + "</a2ui_request>"
    )
    patched.setattr(planner_ws, "run_conversation", _script_stream([
        ("token", "这是本轮的回复内容。" * 3),
        ("token", a2ui),
        ("done", ""),
    ]))
    cid = "conv-persist-skills"
    captured: dict = {}

    def _fake_persist(conv_id, conv_data):
        captured["conv_data"] = conv_data

    monkeypatch.setattr(planner_ws, "_persist_session", _fake_persist)

    sock = FakeWebSocket([{"type": "message", "content": "开始"}])
    await planner_ws.planner_websocket(sock, cid)

    msgs = captured["conv_data"]["messages"]
    asst = [m for m in msgs if m["role"] == "assistant"]
    assert asst, "an assistant message must be appended"
    assert "a2ui" in asst[-1].get("triggered_skills", [])

    # Round-trip through the same JSON path sessions.py uses (planner_messages
    # column is json.dumps(messages)); the extra key must survive verbatim.
    restored = json.loads(json.dumps(msgs, ensure_ascii=False))
    asst_r = [m for m in restored if m["role"] == "assistant"]
    assert "a2ui" in asst_r[-1].get("triggered_skills", [])


@pytest.mark.asyncio
async def test_plain_assistant_message_has_no_triggered_skills(patched, monkeypatch):
    patched.setattr(planner_ws, "run_conversation", _script_stream([
        ("token", "纯文字回复，无任何技能动作。" * 3),
        ("done", ""),
    ]))
    captured: dict = {}
    monkeypatch.setattr(planner_ws, "_persist_session",
                        lambda cid, cd: captured.__setitem__("conv_data", cd))
    sock = FakeWebSocket([{"type": "message", "content": "聊聊"}])
    await planner_ws.planner_websocket(sock, "conv-plain-persist")

    asst = [m for m in captured["conv_data"]["messages"] if m["role"] == "assistant"]
    assert asst and "triggered_skills" not in asst[-1], \
        "a turn that triggered nothing must not stamp triggered_skills"
