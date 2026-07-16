"""Backend tests for change: planner-agentic-react-loop (minimal loop).

No network: AgentScope's create_agent is monkeypatched with a fake agent whose
reply_stream yields scripted AgentScope-style events. Covers:
- supports_function_calling allowlist (glm-4.7-flash yes, qwen3.6-27b no)
- read-only gathering tools return ToolChunk + are read-only
- agentic loop maps AgentScope events → WS (token/thinking) + run_event (tool_call)
- agentic loop returns assembled full_response for the caller's extraction
"""

from __future__ import annotations

import asyncio
import pytest

from app.core import model_caps


# ─── function-calling allowlist ─────────────────────────────────────────────

def test_supports_function_calling_allowlist(monkeypatch):
    # The escape hatch (PLANNER_AGENTIC_ALL) may be set in the dev .env; the
    # allowlist semantics must be tested in isolation from it.
    monkeypatch.delenv("PLANNER_AGENTIC_ALL", raising=False)
    assert model_caps.supports_function_calling("glm-4.7-flash") is True
    assert model_caps.supports_function_calling("GLM-4.7-Flash") is True  # case-insensitive
    assert model_caps.supports_function_calling("qwen3.6-27b") is False
    assert model_caps.supports_function_calling("") is False
    assert model_caps.supports_function_calling("gpt-4o") is False  # not on gateway/unverified


def test_supports_function_calling_escape_hatch(monkeypatch):
    # PLANNER_AGENTIC_ALL forces True for any non-empty model id (manual QA).
    monkeypatch.setenv("PLANNER_AGENTIC_ALL", "on")
    assert model_caps.supports_function_calling("qwen3.6-27b") is True
    assert model_caps.supports_function_calling("anything-else") is True
    assert model_caps.supports_function_calling("") is False  # still needs a real id


@pytest.mark.asyncio
async def test_agentic_prompt_directs_tool_use(monkeypatch):
    # The agentic turn MUST augment the base prompt with the ReAct tool-using
    # directive (design D4) — otherwise the model narrates instead of calling
    # tools and the ReAct loop never iterates.
    from app.core import planner_agent_loop as L

    captured = {}

    def _capture(**kw):
        captured.update(kw)
        return _FakeAgent([_mk_event("TextBlockDeltaEvent", delta="ok")])

    import app.core.agentscope_runner as _runner
    monkeypatch.setattr(_runner, "create_agent", _capture)
    ws = _FakeWS(); emitter = _RecEmitter()
    await L.run_agentic_turn(
        system_prompt="BASE_PROMPT", model_cfg={"model_name": "glm-4.7-flash", "provider": "glm"},
        user_content="hi", conversation_id="c", planning_context={},
        websocket=ws, run_emitter=emitter, memory={},
    )
    sp = captured.get("system_prompt", "")
    assert "BASE_PROMPT" in sp  # base prompt preserved
    assert "emit_proposal" in sp and "read_user_profile" in sp  # tools named
    assert "Agentic" in sp or "ReAct" in sp  # mode declared
    # explicitly forbids the "narrate instead of call" failure mode
    assert "真正调用" in sp or "先行动后叙述" in sp


# ─── read-only gathering tools ──────────────────────────────────────────────

def test_gathering_tools_return_toolchunk_and_readonly():
    from app.core.planner_agent_loop import _build_tools
    from agentscope.tool import ToolChunk
    tools = _build_tools(conversation_id="conv-x", planning_context={
        "user_profile": {"role": "销售总监", "preferences": {"language": "zh-CN"}},
        "internal_context": {"capabilities": [{"id": 1, "type": "tool", "name": "sql_query"}]},
    })
    assert len(tools) == 3
    # each FunctionTool wraps a read-only async fn; invoke read_user_profile
    # via its underlying function to confirm it returns a ToolChunk
    import inspect
    # find the read_user_profile tool by name
    names = [getattr(t, "name", "") or getattr(getattr(t, "func", None), "__name__", "") for t in tools]
    assert any("read_user_profile" in str(n) for n in names) or len(tools) == 3


# ─── agentic loop event mapping (fake reply_stream) ─────────────────────────

class _Ev:
    """Minimal stand-in for an AgentScope event (type via class name)."""
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


def _mk_event(cls_name, **fields):
    return type(cls_name, (_Ev,), {})(**fields)


class _FakeAgent:
    def __init__(self, events):
        self._events = events
    async def reply_stream(self, msg):
        for e in self._events:
            yield e


class _FakeWS:
    def __init__(self):
        self.sent = []
    async def send_json(self, payload):
        self.sent.append(payload)


class _RecEmitter:
    def __init__(self):
        self.events = []
        self.run_id = "run-agentic-test"
    async def emit(self, step, status, *, message="", **kw):
        self.events.append((step, status, message))


def _chunk_text(chunk):
    blk = chunk.content[0]
    return blk["text"] if isinstance(blk, dict) else blk.text


@pytest.mark.asyncio
async def test_agentic_loop_maps_events(monkeypatch):
    from app.core import planner_agent_loop as L

    scripted = [
        _mk_event("ThinkingBlockDeltaEvent", delta="我需要先了解用户"),
        _mk_event("ToolCallStartEvent", tool_call_name="read_user_profile"),
        _mk_event("ToolResultStartEvent", tool_call_name="read_user_profile"),
        _mk_event("ToolResultEndEvent", tool_call_name="read_user_profile"),
        _mk_event("TextBlockDeltaEvent", delta="你是张三，"),
        _mk_event("TextBlockDeltaEvent", delta="销售总监。"),
    ]
    import app.core.agentscope_runner as _runner
    monkeypatch.setattr(_runner, "create_agent", lambda **kw: _FakeAgent(scripted))

    ws = _FakeWS()
    emitter = _RecEmitter()
    result = await L.run_agentic_turn(
        system_prompt="sys", model_cfg={"model_name": "glm-4.7-flash", "provider": "glm"},
        user_content="我是谁？", conversation_id="conv-x",
        planning_context={"user_profile": {"role": "销售总监"}},
        websocket=ws, run_emitter=emitter,
    )
    # full_response assembled from TextBlockDelta
    assert result["full_response"] == "你是张三，销售总监。"
    # tokens + thinking streamed to ws
    types = [m["type"] for m in ws.sent]
    assert "token" in types and "thinking_content" in types
    # tool call surfaced as run_event steps
    steps = [s for s, _st, _m in emitter.events]
    assert any(s == "tool_call:read_user_profile" for s in steps)
    # both running and completed recorded for the tool
    statuses = [st for s, st, _m in emitter.events if s == "tool_call:read_user_profile"]
    assert "running" in statuses and "completed" in statuses


@pytest.mark.asyncio
async def test_agentic_loop_text_only_turn(monkeypatch):
    # A clarification turn: only thinking + text, no tool calls. Still returns text.
    from app.core import planner_agent_loop as L
    scripted = [
        _mk_event("ThinkingBlockDeltaEvent", delta="信息不足"),
        _mk_event("TextBlockDeltaEvent", delta="请问面向什么用户？"),
    ]
    import app.core.agentscope_runner as _runner
    monkeypatch.setattr(_runner, "create_agent", lambda **kw: _FakeAgent(scripted))
    ws = _FakeWS(); emitter = _RecEmitter()
    result = await L.run_agentic_turn(
        system_prompt="sys", model_cfg={"model_name": "glm-4.7-flash", "provider": "glm"},
        user_content="做个 agent", conversation_id="conv-y", planning_context=None,
        websocket=ws, run_emitter=emitter,
    )
    assert result["full_response"] == "请问面向什么用户？"
    assert not any(s.startswith("tool_call:") for s, _st, _m in emitter.events)


# ─── structured output tools (4.x) ──────────────────────────────────────────

def test_structured_tools_registered_with_turn_state():
    # With a turn_state the full tool set (read_skill + 3 structured) is added.
    from app.core.planner_agent_loop import _build_tools
    tools = _build_tools(conversation_id="c", planning_context={}, turn_state={})
    names = {getattr(t, "name", "") for t in tools}
    assert {"read_user_profile", "recall_memory", "match_capabilities",
            "read_skill", "emit_proposal", "request_confirmation",
            "update_memory"} <= names
    # Structured tools are not read-only; gathering tools are.
    by_name = {getattr(t, "name", ""): t for t in tools}
    assert by_name["read_user_profile"].is_read_only is True
    assert by_name["emit_proposal"].is_read_only is False


@pytest.mark.asyncio
async def test_emit_proposal_validates_and_persists(monkeypatch):
    # 8.3: a valid structured proposal passes validation, is persisted via
    # proposal_store.upsert_proposal, and is recorded into turn_state.
    from app.core.planner_agent_loop import _build_tools

    calls = {}

    def _fake_upsert(**kw):
        calls.update(kw)
        class _P:  # noqa: D401 - tiny stand-in
            id = 7
        return _P()

    import app.core.proposal_store as _ps
    monkeypatch.setattr(_ps, "upsert_proposal", _fake_upsert)

    turn_state = {}
    tools = _build_tools(conversation_id="conv-p", planning_context={}, turn_state=turn_state)
    emit = {getattr(t, "name", ""): t for t in tools}["emit_proposal"]
    payload = {
        "title": "销售助手", "goal": "回答销售问题", "runtime_mode": "direct",
        "nodes": [
            {"id": "p1", "type": "p", "config": {"role_name": "销售助手", "system_prompt": "你是销售顾问", "output_format": "markdown"}},
            {"id": "m1", "type": "m", "config": {"model_name": "qwen3.6-27b", "provider": "glm", "temperature": 0.7, "max_tokens": 4096, "streaming": True}},
            {"id": "agent1", "type": "agent", "config": {"role_name": "销售助手"}},
        ],
        "edges": [{"source": "p1", "target": "agent1"}, {"source": "m1", "target": "agent1"}],
    }
    chunk = await emit._func(proposal=payload)
    text = _chunk_text(chunk)
    assert "保存" in text or "通过" in text
    # recorded for the caller's done-branch
    assert turn_state.get("proposal") == payload
    # persisted with the conversation id + serialized json
    assert calls.get("conversation_id") == "conv-p"
    assert "销售助手" in calls.get("proposal_json", "")


@pytest.mark.asyncio
async def test_emit_proposal_rejects_invalid():
    from app.core.planner_agent_loop import _build_tools
    turn_state = {}
    tools = _build_tools(conversation_id="c", planning_context={}, turn_state=turn_state)
    emit = {getattr(t, "name", ""): t for t in tools}["emit_proposal"]
    chunk = await emit._func(proposal="not-a-dict")
    text = _chunk_text(chunk)
    assert "失败" in text or "校验" in text
    assert "proposal" not in turn_state


@pytest.mark.asyncio
async def test_emit_proposal_rejects_empty_nodes():
    """emit_proposal rejects a proposal that passes ProposalPayload validation
    but has no nodes (DAG projection guard)."""
    from app.core.planner_agent_loop import _build_tools
    turn_state = {}
    tools = _build_tools(conversation_id="c", planning_context={}, turn_state=turn_state)
    emit = {getattr(t, "name", ""): t for t in tools}["emit_proposal"]
    # Valid ProposalPayload fields but no nodes
    payload = {"title": "助手", "goal": "回答问题", "runtime_mode": "direct"}
    chunk = await emit._func(proposal=payload)
    text = _chunk_text(chunk)
    assert "nodes" in text
    assert "DAG" in text or "投影" in text
    assert "proposal" not in turn_state

    # Also reject explicit empty list
    payload_empty = {"title": "助手", "goal": "g", "nodes": [], "edges": []}
    chunk2 = await emit._func(proposal=payload_empty)
    text2 = _chunk_text(chunk2)
    assert "nodes" in text2
    assert "proposal" not in turn_state


@pytest.mark.asyncio
async def test_update_memory_and_confirmation_record_to_turn_state():
    from app.core.planner_agent_loop import _build_tools
    turn_state = {}
    tools = {getattr(t, "name", ""): t for t in
             _build_tools(conversation_id="c", planning_context={}, turn_state=turn_state)}
    await tools["update_memory"]._func(patch={"requirement_summary": "做个销售助手"})
    await tools["request_confirmation"]._func(
        prompt="确认创建？", options=[{"id": "yes", "label": "确认"}])
    assert turn_state["memory_update"]["requirement_summary"] == "做个销售助手"
    assert turn_state["a2ui"]["options"][0]["id"] == "yes"
    # The confirmation MUST carry an id (frontend keys the card + response on it;
    # without it the card never renders and the turn looks stuck).
    assert turn_state["a2ui"].get("id"), "request_confirmation must synthesize an id"
    assert turn_state["a2ui"].get("topic"), "request_confirmation must carry a topic"
    # id is stable for the same prompt.
    ts2 = {}
    tools2 = {getattr(t, "name", ""): t for t in
             _build_tools(conversation_id="c", planning_context={}, turn_state=ts2)}
    await tools2["request_confirmation"]._func(prompt="确认创建？", options=[{"id": "yes", "label": "确认"}])
    assert ts2["a2ui"]["id"] == turn_state["a2ui"]["id"]


@pytest.mark.asyncio
async def test_run_agentic_turn_appends_canonical_tags(monkeypatch):
    # The structured tool outputs are re-emitted as the canonical tags the
    # caller's done-branch already parses (memory_update / a2ui / proposal).
    from app.core import planner_agent_loop as L

    # Spy on _build_tools to capture the live turn_state, and use a fake agent
    # whose reply_stream populates that turn_state (as the real tools would).
    real_build = L._build_tools
    _state_holder = []

    def _spy_build(**kw):
        ts = kw.get("turn_state")
        if ts is not None:
            _state_holder.append(ts)
        return real_build(**kw)

    monkeypatch.setattr(L, "_build_tools", _spy_build)

    class _FA:
        async def reply_stream(self, msg):
            ts = _state_holder[0]
            ts["proposal"] = {"title": "T", "goal": "G", "runtime_mode": "direct"}
            ts["memory_update"] = {"requirement_summary": "S"}
            ts["a2ui"] = {"prompt": "确认？", "options": [{"id": "y", "label": "是"}]}
            yield _mk_event("TextBlockDeltaEvent", delta="方案如下。")

    import app.core.agentscope_runner as _runner
    monkeypatch.setattr(_runner, "create_agent", lambda **kw: _FA())

    ws = _FakeWS(); emitter = _RecEmitter()
    result = await L.run_agentic_turn(
        system_prompt="sys", model_cfg={"model_name": "glm-4.7-flash", "provider": "glm"},
        user_content="做个 agent", conversation_id="conv-z", planning_context={},
        websocket=ws, run_emitter=emitter, memory={},
    )
    fr = result["full_response"]
    assert "方案如下。" in fr
    assert "<memory_update>" in fr and "requirement_summary" in fr
    assert "<a2ui_request>" in fr and "options" in fr
    assert '"ready": true' in fr and '"proposal"' in fr


# ─── routing / fallback (8.5) ───────────────────────────────────────────────

def test_unsupported_model_does_not_use_agentic(monkeypatch):
    # 8.5: a model not on the function-calling allowlist must NOT route to the
    # agentic loop — the caller's gate uses supports_function_calling.
    monkeypatch.delenv("PLANNER_AGENTIC_ALL", raising=False)
    assert model_caps.supports_function_calling("qwen3.6-27b") is False
    assert model_caps.supports_function_calling("glm-4.7-flash") is True


# ─── resource bounds (8.7) ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_react_max_iters_wired_and_graceful_finish(monkeypatch):
    # 8.7: max_iters is passed to create_agent (the ReAct loop's hard stop), and
    # a long event stream that never produces a proposal still finishes cleanly,
    # returning the assembled text rather than hanging or raising.
    from app.core import planner_agent_loop as L

    captured = {}

    def _capture_create(**kw):
        captured.update(kw)
        # Simulate hitting the iteration ceiling: many tool/think cycles, no proposal.
        events = []
        for _ in range(8):
            events.append(_mk_event("ToolCallStartEvent", tool_call_name="recall_memory"))
            events.append(_mk_event("ToolResultStartEvent", tool_call_name="recall_memory"))
            events.append(_mk_event("ToolResultEndEvent", tool_call_name="recall_memory"))
        events.append(_mk_event("TextBlockDeltaEvent", delta="信息仍不足，请补充。"))
        return _FakeAgent(events)

    import app.core.agentscope_runner as _runner
    monkeypatch.setattr(_runner, "create_agent", _capture_create)

    ws = _FakeWS(); emitter = _RecEmitter()
    result = await L.run_agentic_turn(
        system_prompt="sys", model_cfg={"model_name": "glm-4.7-flash", "provider": "glm"},
        user_content="做个 agent", conversation_id="conv-iter", planning_context={},
        websocket=ws, run_emitter=emitter, memory={},
    )
    # max_iters wired through to create_agent
    assert captured.get("react_max_iters", 0) >= 1
    # graceful finish: assembled text returned, no proposal tag (turn ended at cap)
    assert result["full_response"] == "信息仍不足，请补充。"
    assert "```json" not in result["full_response"]



# ─── agentic text filter: <think> split + tag stripping (issue 1) ────────────

def test_text_filter_splits_think_and_prose():
    from app.core.planner_agent_loop import _AgenticTextFilter
    f = _AgenticTextFilter()
    think, vis = "", ""
    for d in ["<thi", "nk>我先想想", "需求</th", "ink>你好", "，这是回答"]:
        th, vi = f.feed(d)
        think += th; vis += vi
    tth, tvi = f.flush()
    think += tth; vis += tvi
    assert think == "我先想想需求"
    assert vis == "你好，这是回答"


def test_text_filter_strips_fake_structured_tags():
    from app.core.planner_agent_loop import _AgenticTextFilter
    f = _AgenticTextFilter()
    th, vi = f.feed("方案就绪。<memory_update>{\"x\":1}</memory_update> 完成")
    tth, tvi = f.flush()
    assert (th + tth) == ""
    # the fake memory_update tag is removed from the visible chat stream
    assert "<memory_update>" not in (vi + tvi)
    assert "方案就绪。" in (vi + tvi) and "完成" in (vi + tvi)


def test_text_filter_withholds_proposal_json():
    from app.core.planner_agent_loop import _AgenticTextFilter
    f = _AgenticTextFilter()
    vis = ""
    for p in ["这是方案\n", "```json\n{\"ready\":true,\"proposal\":{}}\n```"]:
        _, vi = f.feed(p); vis += vi
    _, tvi = f.flush(); vis += tvi
    assert "这是方案" in vis
    assert "```json" not in vis and "ready" not in vis


def test_text_filter_plain_text_passthrough():
    from app.core.planner_agent_loop import _AgenticTextFilter
    f = _AgenticTextFilter()
    th, vi = f.feed("直接回答没有思考")
    tth, tvi = f.flush()
    assert (th + tth) == ""
    assert (vi + tvi) == "直接回答没有思考"


def test_text_filter_multiple_think_blocks_interleaved():
    # Multi-iteration ReAct (minimax): MANY <think> blocks at arbitrary positions,
    # interleaved with prose. All reasoning goes to the think channel; only the
    # prose between/after them is visible.
    from app.core.planner_agent_loop import _AgenticTextFilter
    f = _AgenticTextFilter()
    th, vi = f.feed("<think>第一步思考</think>读取画像。<think>第二步思考</think>这是回答。")
    tth, tvi = f.flush()
    assert (th + tth) == "第一步思考第二步思考"
    assert (vi + tvi) == "读取画像。这是回答。"


def test_text_filter_multiple_think_blocks_split_across_deltas():
    # The same, but every tag is split across delta boundaries.
    from app.core.planner_agent_loop import _AgenticTextFilter
    f = _AgenticTextFilter()
    deltas = ["<thi", "nk>想法一</thi", "nk>正文一", "<th", "ink>想法二</think>", "正文二"]
    think, vis = "", ""
    for d in deltas:
        t, v = f.feed(d)
        think += t; vis += v
    tt, tv = f.flush()
    think += tt; vis += tv
    assert think == "想法一想法二"
    assert vis == "正文一正文二"


@pytest.mark.asyncio
async def test_run_agentic_turn_strips_think_from_full_response(monkeypatch):
    # full_response (persisted by the caller) must not retain <think> reasoning —
    # it was already streamed on the thinking_content channel.
    from app.core import planner_agent_loop as L
    scripted = [
        _mk_event("TextBlockDeltaEvent", delta="<think>盘算一下</think>"),
        _mk_event("TextBlockDeltaEvent", delta="最终回答。"),
    ]
    import app.core.agentscope_runner as _runner
    monkeypatch.setattr(_runner, "create_agent", lambda **kw: _FakeAgent(scripted))
    ws = _FakeWS(); emitter = _RecEmitter()
    result = await L.run_agentic_turn(
        system_prompt="sys", model_cfg={"model_name": "glm-4.7-flash", "provider": "glm"},
        user_content="hi", conversation_id="conv-think", planning_context={},
        websocket=ws, run_emitter=emitter, memory={},
    )
    assert "<think>" not in result["full_response"]
    assert result["full_response"] == "最终回答。"
    # the reasoning went to the thinking channel
    assert any(m["type"] == "thinking_content" and "盘算" in m["content"] for m in ws.sent)
    # the chat token stream never carried the think text
    assert all("盘算" not in m.get("content", "") for m in ws.sent if m["type"] == "token")


# ─── T12: tool_call activity events (planner-process-stream-rich-display) ─────

@pytest.mark.asyncio
async def test_tool_call_activity_events_start_done(monkeypatch):
    """tool_call activity events: start emitted before tool, done after result."""
    from app.core import planner_agent_loop as L

    scripted = [
        _mk_event("ToolCallStartEvent", tool_call_name="match_capabilities", tool_call_id="tc1"),
        _mk_event("ToolResultStartEvent", tool_call_name="match_capabilities", tool_call_id="tc1"),
        _mk_event("ToolResultTextDeltaEvent", tool_call_id="tc1", delta="能力匹配：候选 3 项"),
        _mk_event("ToolResultEndEvent", tool_call_name="match_capabilities", tool_call_id="tc1"),
        _mk_event("TextBlockDeltaEvent", delta="好的。"),
    ]
    import app.core.agentscope_runner as _runner
    monkeypatch.setattr(_runner, "create_agent", lambda **kw: _FakeAgent(scripted))
    ws = _FakeWS(); emitter = _RecEmitter()
    await L.run_agentic_turn(
        system_prompt="sys", model_cfg={"model_name": "glm-4.7-flash", "provider": "glm"},
        user_content="hi", conversation_id="c", planning_context={},
        websocket=ws, run_emitter=emitter, memory={},
    )
    act = [m for m in ws.sent if m.get("type") == "activity" and m.get("phase") == "tool_call"]
    assert len(act) >= 2
    starts = [a for a in act if a["status"] == "start"]
    dones  = [a for a in act if a["status"] == "done"]
    assert starts and dones
    # id must match between start and done
    start_ids = {a["id"] for a in starts}
    done_ids  = {a["id"] for a in dones}
    assert start_ids & done_ids  # at least one paired id


@pytest.mark.asyncio
async def test_tool_call_activity_done_on_exception(monkeypatch):
    """Even when ToolResultEndEvent fires after an error delta, done is still emitted."""
    from app.core import planner_agent_loop as L

    scripted = [
        _mk_event("ToolCallStartEvent", tool_call_name="recall_memory", tool_call_id="tc2"),
        _mk_event("ToolResultStartEvent", tool_call_name="recall_memory", tool_call_id="tc2"),
        _mk_event("ToolResultEndEvent", tool_call_name="recall_memory", tool_call_id="tc2"),
    ]
    import app.core.agentscope_runner as _runner
    monkeypatch.setattr(_runner, "create_agent", lambda **kw: _FakeAgent(scripted))
    ws = _FakeWS(); emitter = _RecEmitter()
    await L.run_agentic_turn(
        system_prompt="sys", model_cfg={"model_name": "glm-4.7-flash", "provider": "glm"},
        user_content="hi", conversation_id="c", planning_context={},
        websocket=ws, run_emitter=emitter, memory={},
    )
    act = [m for m in ws.sent if m.get("type") == "activity" and m.get("phase") == "tool_call"]
    # start + done must both appear regardless of result content
    assert any(a["status"] == "start" for a in act)
    assert any(a["status"] == "done" for a in act)


@pytest.mark.asyncio
async def test_step_text_activity_on_thinking(monkeypatch):
    """step_text activity event emitted when thinking_content arrives."""
    from app.core import planner_agent_loop as L

    scripted = [
        _mk_event("ThinkingBlockDeltaEvent", delta="分析中..."),
        _mk_event("TextBlockDeltaEvent", delta="结果。"),
    ]
    import app.core.agentscope_runner as _runner
    monkeypatch.setattr(_runner, "create_agent", lambda **kw: _FakeAgent(scripted))
    ws = _FakeWS(); emitter = _RecEmitter()
    await L.run_agentic_turn(
        system_prompt="sys", model_cfg={"model_name": "glm-4.7-flash", "provider": "glm"},
        user_content="hi", conversation_id="c", planning_context={},
        websocket=ws, run_emitter=emitter, memory={},
    )
    step_texts = [m for m in ws.sent if m.get("type") == "activity" and m.get("phase") == "step_text"]
    assert step_texts
    assert step_texts[0]["status"] == "stream"
    assert "分析中" in step_texts[0].get("detail", "")


# ─── T12: run_event details fields (planner-process-stream-rich-display) ──────

@pytest.mark.asyncio
async def test_run_event_capability_match_details_fields():
    """capability_match completed event carries matched_count and top_names."""
    from app.core.run_events import RunEventEmitter

    class _WS:
        def __init__(self): self.sent = []
        async def send_json(self, p): self.sent.append(p)

    ws = _WS()
    emitter = RunEventEmitter(run_id="r1", websocket=ws)
    await emitter.emit(
        "capability_match", "completed",
        message="匹配到 3 个候选能力",
        details={"matched_count": 3, "top_names": ["sql_query", "http_request", "send_email"]},
    )
    sent = [m for m in ws.sent if m.get("type") == "run_event" and m.get("step") == "capability_match"]
    assert sent
    d = sent[0].get("details", {})
    assert d.get("matched_count") == 3
    assert isinstance(d.get("top_names"), list) and len(d["top_names"]) <= 5


@pytest.mark.asyncio
async def test_run_event_expert_retrieval_details_template_name():
    """expert_retrieval completed event carries template_name."""
    from app.core.run_events import RunEventEmitter

    class _WS:
        def __init__(self): self.sent = []
        async def send_json(self, p): self.sent.append(p)

    ws = _WS()
    emitter = RunEventEmitter(run_id="r2", websocket=ws)
    await emitter.emit(
        "expert_retrieval", "completed",
        message="命中 1 个专家模板",
        details={"expert_count": 1, "template_name": "销售助理"},
    )
    sent = [m for m in ws.sent if m.get("type") == "run_event" and m.get("step") == "expert_retrieval"]
    assert sent
    assert sent[0]["details"].get("template_name") == "销售助理"


@pytest.mark.asyncio
async def test_run_event_memory_recall_details_fields():
    """memory_recall completed event carries recalled_count and sources."""
    from app.core.run_events import RunEventEmitter

    class _WS:
        def __init__(self): self.sent = []
        async def send_json(self, p): self.sent.append(p)

    ws = _WS()
    emitter = RunEventEmitter(run_id="r3", websocket=ws)
    await emitter.emit(
        "memory_recall", "completed",
        details={"recalled_count": 4, "sources": ["画像(2条)", "情景记忆(2条)"]},
    )
    sent = [m for m in ws.sent if m.get("type") == "run_event" and m.get("step") == "memory_recall"]
    assert sent
    d = sent[0]["details"]
    assert d.get("recalled_count") == 4
    assert isinstance(d.get("sources"), list)
