"""Backend tests for change: planner-activity-visibility-and-thinking-fix.

Drives the real planner WebSocket turn loop with a fake socket + a scripted
``run_conversation`` so we can assert the structured `activity` events without a
live model. Covers spec planner-activity-timeline:
- a turn emits at least one start/done activity pair (processing, drafting);
- a proposal turn emits a composing_proposal start/done pair;
- every started activity is closed by a matching done before `done` (no step
  left "in progress").
"""

from __future__ import annotations

import json

import pytest

from app.api.planner import ws as planner_ws


class FakeWebSocket:
    """Minimal WebSocket double: scripts inbound messages, captures outbound."""

    def __init__(self, inbound: list[dict]):
        self._inbound = list(inbound)
        self.sent: list[dict] = []

    async def accept(self):
        return None

    async def receive_text(self) -> str:
        if not self._inbound:
            # Signal the handler to exit its loop cleanly.
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


def _activities(sent: list[dict]) -> list[dict]:
    return [m for m in sent if m.get("type") == "activity"]


@pytest.fixture
def patched(monkeypatch):
    # Neutralize agent construction + Langfuse; we only test the event plumbing.
    monkeypatch.setattr(planner_ws, "create_agent", lambda **kw: object())
    monkeypatch.setattr(planner_ws, "_get_langfuse", lambda: None)
    return monkeypatch


@pytest.mark.asyncio
async def test_plain_turn_emits_start_and_done_activity(patched):
    # Stream long enough chunks that the memory/proposal strippers release
    # visible tokens mid-stream (they hold back a short tag-prefix suffix), so
    # the drafting phase fires as it does for real (non-trivial) replies.
    patched.setattr(planner_ws, "run_conversation", _script_stream([
        ("token", "你好，我理解你的需求了。" * 3),
        ("token", "下面我们再确认几个关键点，确保方案贴合实际场景。" * 3),
        ("done", ""),
    ]))
    sock = FakeWebSocket([{ "type": "message", "content": "做个客服机器人" }])
    await planner_ws.planner_websocket(sock, "conv-plain")

    acts = _activities(sock.sent)
    assert acts, "a plain turn must emit activity events"
    # Every started phase is closed.
    starts = {(a["id"]) for a in acts if a["status"] == "start"}
    dones = {(a["id"]) for a in acts if a["status"] == "done"}
    assert starts, "expected at least one start activity"
    assert starts <= dones, f"every started activity must be done; open={starts - dones}"
    # processing + drafting phases are present for a turn that streamed tokens.
    phases = {a["phase"] for a in acts}
    assert "processing" in phases
    assert "drafting" in phases


@pytest.mark.asyncio
async def test_proposal_turn_emits_composing_proposal_pair(patched):
    proposal = {
        "ready": True,
        "proposal": {
            "architecture_summary": "S", "rationale": "R",
            "nodes": [], "edges": [],
        },
    }
    fenced = "```json\n" + json.dumps(proposal, ensure_ascii=False) + "\n```"
    patched.setattr(planner_ws, "run_conversation", _script_stream([
        ("token", "方案如下："),
        ("token", fenced),
        ("done", ""),
    ]))
    sock = FakeWebSocket([{ "type": "message", "content": "出方案" }])
    await planner_ws.planner_websocket(sock, "conv-proposal")

    acts = _activities(sock.sent)
    composing = [a for a in acts if a["phase"] == "composing_proposal"]
    assert any(a["status"] == "start" for a in composing), "proposal turn emits composing_proposal start"
    assert any(a["status"] == "done" for a in composing), "proposal turn emits composing_proposal done"
    # No activity left open at the end of the turn.
    starts = {a["id"] for a in acts if a["status"] == "start"}
    dones = {a["id"] for a in acts if a["status"] == "done"}
    assert starts <= dones, f"open activities at turn end: {starts - dones}"
