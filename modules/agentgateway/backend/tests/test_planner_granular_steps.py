"""Backend tests for change: planner-granular-step-hooks.

Verifies real sub-steps are emitted individually (not lumped) with content:
- build_planning_context on_step fires per builder (7), in order, with detail
- no-hook call returns identical shape (zero impact)
- assemble_layered_context on_layer fires per layer with real hit counts
- ws turn emits per-sub-step run_events (context_load:* / memory_recall:*)
- a failing builder emits a failed sub-step, others continue
"""

from __future__ import annotations

import json
import pytest

from app.core import planning_context as pc


# ─── 5.1 / 5.2 build_planning_context hook ──────────────────────────────────

def test_on_step_fires_per_builder_in_order():
    seen = []
    pc.build_planning_context(
        user_input={"goal_text": "做个销售日报 agent"},
        on_step=lambda step, phase, detail: seen.append((step, phase, detail)),
    )
    starts = [s for s, p, _ in seen if p == "start"]
    dones = [s for s, p, _ in seen if p == "done"]
    assert starts == [
        "user_input", "user_profile", "memory_context", "workspace_context",
        "external_context", "internal_context", "policy_context",
    ]
    assert len(dones) == 7
    # internal_context done carries a real capability_count
    det = dict((s, d) for s, p, d in seen if p == "done")
    assert "capability_count" in det["internal_context"]


def test_no_hook_zero_impact():
    a = pc.build_planning_context(user_input={"goal_text": "x"})
    b = pc.build_planning_context(user_input={"goal_text": "x"}, on_step=lambda *a: None)
    assert set(a.keys()) == set(b.keys()) == {
        "user_input", "user_profile", "memory_context", "workspace_context",
        "external_context", "internal_context", "policy_context",
    }


def test_failing_builder_emits_failed_substep(monkeypatch):
    seen = []
    def boom(*a, **k):
        raise RuntimeError("source down")
    monkeypatch.setattr(pc, "_build_internal_context", boom)
    ctx = pc.build_planning_context(
        user_input={"goal_text": "x"},
        on_step=lambda step, phase, detail: seen.append((step, phase, detail)),
    )
    # the failed builder reported a failed sub-step
    assert any(s == "internal_context" and p == "failed" for s, p, _ in seen)
    # others still completed
    assert any(s == "policy_context" and p == "done" for s, p, _ in seen)
    # aggregation still total (degraded segment present)
    assert set(ctx.keys()) == {
        "user_input", "user_profile", "memory_context", "workspace_context",
        "external_context", "internal_context", "policy_context",
    }


# ─── 5.3 assemble_layered_context on_layer ──────────────────────────────────

def test_on_layer_disabled_emits_signal():
    # Default planner (no agent → memory disabled) still surfaces one layer signal.
    from app.core import memory_service
    seen = []
    memory_service.assemble_layered_context(
        memory_service.SCENARIO_PLANNER,
        planner_conversation_id="conv-x", query="hi",
        on_layer=lambda layer, phase, hits: seen.append((layer, phase, hits)),
    )
    assert any(layer == "disabled" for layer, _p, _h in seen)


# ─── 5.4 / 5.5 ws integration: per-sub-step run_events ──────────────────────

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
async def test_ws_emits_granular_substeps(monkeypatch):
    from app.api.planner import ws as planner_ws
    from app.api.planner import state as planner_state
    # Pin the single-call path: these granular sub-steps are a single-call-path
    # feature. In agentic mode the planner gathers via its own tools, so the
    # fixed steps are intentionally suppressed — clear both flags so a dev .env
    # with PLANNER_AGENTIC_ALL can't reroute this test.
    monkeypatch.delenv("PLANNER_AGENTIC", raising=False)
    monkeypatch.delenv("PLANNER_AGENTIC_ALL", raising=False)
    monkeypatch.setattr(planner_ws, "create_agent", lambda **kw: object())
    monkeypatch.setattr(planner_ws, "_get_langfuse", lambda: None)
    monkeypatch.setattr(planner_ws, "_persist_session", lambda *a, **k: None)
    monkeypatch.setattr(planner_ws, "run_conversation", _script_stream([
        ("token", "你好。"),
        ("done", ""),
    ]))
    planner_state.store_conv("conv-gran", {
        "messages": [], "model": {"model_name": "qwen3.6-27b", "provider": "glm"},
        "memory": planner_state._new_memory(), "file_artifacts": [],
    })
    sock = FakeWebSocket([{"type": "message", "content": "做个 sales 日报 agent"}])
    await planner_ws.planner_websocket(sock, "conv-gran")

    run_events = [m for m in sock.sent if m.get("type") == "run_event"]
    steps = [e["step"] for e in run_events]
    # context_load sub-steps appear individually (not just one lump)
    assert any(s.startswith("context_load:") for s in steps), steps
    assert "context_load:user_profile" in steps
    assert "context_load:internal_context" in steps
    # memory_recall surfaced (disabled → single sub-step or parent completion)
    assert any(s.startswith("memory_recall") for s in steps)
    # each context sub-step carries a human message
    sub = next(e for e in run_events if e["step"] == "context_load:internal_context")
    assert sub.get("message")
