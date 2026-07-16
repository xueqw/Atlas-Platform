"""Agentic ReAct turn for the planner (planner-agentic-react-loop).

Drives ONE planner turn through AgentScope's native ReAct loop (Agent.reply_stream
+ Toolkit), so the model genuinely thinks → calls read-only tools → thinks again,
and the real multi-step process is observable.

Two tool families are registered:
- Read-only gathering tools (read_user_profile / recall_memory /
  match_capabilities / read_skill) — thin wrappers over existing T1–T9 services,
  no writes.
- Structured output tools (emit_proposal / request_confirmation / update_memory)
  — let the model produce its proposal / confirmation / memory as OBSERVABLE tool
  calls instead of the caller regex-parsing the final text. They record into a
  ``turn_state`` dict; after the loop ``run_agentic_turn`` re-emits the canonical
  tags (``<memory_update>`` / ``<a2ui_request>`` / fenced proposal JSON) into the
  returned ``full_response`` so the caller's EXISTING done-branch extraction +
  finalize runs unchanged for both paths (design D4/D8 "归一").

Only used when the agentic feature flag is on AND the model supports
function-calling (probed). Any failure raises so the caller can fall back to the
single-call ``run_conversation`` path — existing behaviour is never broken.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

_log = logging.getLogger(__name__)


def _build_tools(*, conversation_id: str, planning_context: Optional[dict],
                 memory: Optional[dict] = None, turn_state: Optional[dict] = None):
    """Build the AgentScope FunctionTool list for the planner.

    Without ``turn_state`` (e.g. capability probes / unit checks) only the three
    pure read-only gathering tools are returned. When ``turn_state`` is provided
    (the real agentic turn) ``read_skill`` plus the three structured output tools
    are added — these record into ``turn_state`` (and ``memory``) which the caller
    persists via its shared done-branch. All tools return ``ToolChunk``."""
    from agentscope.tool import FunctionTool, ToolChunk
    from agentscope.message import TextBlock

    has_turn_state = turn_state is not None
    turn_state = turn_state if turn_state is not None else {}
    memory = memory if memory is not None else {}

    def _resp(text: str) -> "ToolChunk":
        return ToolChunk(content=[TextBlock(type="text", text=text)], is_last=True)

    # ── Read-only gathering tools ────────────────────────────────────────────

    async def read_user_profile() -> "ToolChunk":
        """读取当前用户画像：角色、部门、行业、偏好。在为用户规划前先了解他是谁。"""
        prof = (planning_context or {}).get("user_profile") or {}
        if not prof:
            from app.core.planning_context import build_planning_context
            ctx = build_planning_context(conversation_id=conversation_id, user_input={})
            prof = ctx.get("user_profile") or {}
        bits = [f"{k}={prof.get(k)}" for k in ("role", "department", "industry") if prof.get(k)]
        prefs = prof.get("preferences") or {}
        if prefs:
            bits.append("偏好=" + json.dumps(prefs, ensure_ascii=False))
        return _resp("用户画像：" + ("；".join(bits) if bits else "（暂无画像信息）"))

    async def recall_memory(query: str = "") -> "ToolChunk":
        """按需召回与当前规划相关的长期记忆（用户偏好 / 历史方案模式 / 近期活动）。"""
        try:
            from app.core import memory_service
            ctx = memory_service.assemble_layered_context(
                memory_service.SCENARIO_PLANNER,
                planner_conversation_id=conversation_id,
                query=query or "",
            )
            hits = getattr(ctx, "hit_counts", None) or {}
            total = sum(v for v in hits.values() if isinstance(v, int))
            if not total:
                return _resp("记忆召回：无相关长期记忆")
            return _resp(f"记忆召回：命中 {total} 条（分层 {json.dumps(hits, ensure_ascii=False)}）")
        except Exception as exc:
            return _resp(f"记忆召回：暂不可用（{exc}）")

    async def match_capabilities(goal: str = "") -> "ToolChunk":
        """根据规划目标匹配平台可用能力（工具/技能/模型），返回候选能力清单。"""
        try:
            from app.api.planner.proposal import _match_capability_candidates
            from sqlmodel import Session
            from app.core.database import engine
            with Session(engine) as s:
                cands = _match_capability_candidates(
                    planning_context, {"goal": goal}, session=s,
                )
            if not cands:
                return _resp("能力匹配：暂无匹配能力")
            names = "、".join(c.get("name", "") for c in cands[:8])
            return _resp(f"能力匹配：候选 {len(cands)} 项（{names}）")
        except Exception as exc:
            return _resp(f"能力匹配：暂不可用（{exc}）")

    async def read_skill(name: str) -> "ToolChunk":
        """按需读取某个技能的方法论（SKILL.md 内容），在需要运用该技能时调用。"""
        try:
            from sqlmodel import Session, select
            from app.core.database import engine
            from app.models.db import CapabilityItem
            with Session(engine) as s:
                row = s.exec(select(CapabilityItem).where(
                    CapabilityItem.type == "skill", CapabilityItem.name == name
                )).first()
            if row is None:
                return _resp(f"技能「{name}」未找到")
            cfg = json.loads(row.config or "{}")
            entry = cfg.get("entrypoint") or ""
            text = row.description or ""
            # Best-effort: load the SKILL.md body if the entrypoint resolves.
            if entry:
                import os
                root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
                p = os.path.join(root, entry)
                if os.path.isfile(p):
                    with open(p, "r", encoding="utf-8") as f:
                        text = f.read()[:4000]
            turn_state.setdefault("skills_read", []).append(name)
            return _resp(f"技能「{name}」方法论：\n{text}")
        except Exception as exc:
            return _resp(f"读取技能「{name}」失败：{exc}")

    # ── Structured output tools (record into turn_state; caller persists) ─────

    async def emit_proposal(proposal: dict) -> "ToolChunk":
        """提交最终的结构化方案（agent 架构方案）。proposal 必须包含完整 DAG 投影：
        - nodes: 非空列表，每项 {id, type, config}（至少含一个 agent 节点及其 P/M 依赖）
        - edges: 列表，每项 {source, target}（描述节点间连线）
        - 以及 architecture_summary、rationale 等 Proposal Schema 字段。
        校验通过后方案会被保存；校验失败会返回错误，请按提示修正后重新调用。"""
        from app.api.planner.proposal import _validate_proposal_payload
        if not isinstance(proposal, dict):
            return _resp("emit_proposal 失败：proposal 必须是一个对象")
        _payload, ok = _validate_proposal_payload(proposal)
        if not ok:
            return _resp("emit_proposal 校验未通过：proposal 不符合 Proposal Schema，请修正字段后重试")
        # DAG projection guard: nodes must be non-empty for a usable DAG.
        _nodes = proposal.get("nodes")
        if not isinstance(_nodes, list) or len(_nodes) == 0:
            return _resp(
                "emit_proposal 校验未通过：proposal 必须包含 nodes 列表（DAG 投影），"
                "每个节点需含 id/type/config。请补全 nodes 和 edges 后重新调用。"
            )
        # Early persistence (design D4 / task 4.1): write the draft now and record
        # into turn_state so the caller's done-branch enrichment (expert retrieval
        # / capability match / memory mirror) upserts the SAME draft in place.
        try:
            from app.core import proposal_store
            proposal_store.upsert_proposal(
                conversation_id=conversation_id,
                proposal_json=json.dumps(proposal, ensure_ascii=False),
                selected_runtime_mode=str(proposal.get("runtime_mode") or ""),
                selected_expert_template_id=proposal.get("primary_expert_template_id"),
            )
        except Exception as exc:
            _log.warning("emit_proposal: draft persist failed (%s); caller will retry", exc)
        turn_state["proposal"] = proposal
        return _resp("方案已通过校验并保存，可以结束本轮。")

    async def request_confirmation(prompt: str, options: list) -> "ToolChunk":
        """向用户发起一次确认选择（如「确认创建 / 调整方案」）。prompt 为问题文本，
        options 为可选项列表（每项 {id, label} 形式）。调用后前端会弹出确认卡片。"""
        if not isinstance(options, list) or not options:
            return _resp("request_confirmation 失败：options 必须是非空列表")
        # The frontend renders the confirmation card ONLY when the a2ui payload
        # carries an id (it keys the decision ledger + the a2ui_response round
        # trip on it). The tool signature has no id param, so synthesize a stable
        # one from the prompt — otherwise the card never appears and the turn
        # looks "stuck" after 「发起确认完成」.
        import hashlib
        _pid = "confirm_" + hashlib.md5(str(prompt or "").encode("utf-8")).hexdigest()[:8]
        turn_state["a2ui"] = {
            "id": _pid,
            "topic": str(prompt or "")[:40] or "确认",
            "prompt": str(prompt or ""),
            "options": options,
            "allow_free_text": True,
        }
        return _resp(f"已向用户发起确认（{len(options)} 个选项）。")

    async def update_memory(patch: dict) -> "ToolChunk":
        """更新规划记忆（如 requirement_summary / task_classification / 已确认约束）。
        patch 为部分字段对象，会被容错合并进已有记忆，不会清空既有状态。"""
        if not isinstance(patch, dict) or not patch:
            return _resp("update_memory 失败：patch 必须是非空对象")
        # Accumulate across multiple calls in the same turn; the caller merges the
        # accumulated patch into planner memory via _merge_memory_update.
        acc = turn_state.setdefault("memory_update", {})
        acc.update(patch)
        return _resp(f"记忆已更新（{len(patch)} 个字段）。")

    tools = [
        FunctionTool(read_user_profile, is_read_only=True),
        FunctionTool(recall_memory, is_read_only=True),
        FunctionTool(match_capabilities, is_read_only=True),
    ]
    if has_turn_state:
        tools += [
            FunctionTool(read_skill, is_read_only=True),
            FunctionTool(emit_proposal, is_read_only=False),
            FunctionTool(request_confirmation, is_read_only=False),
            FunctionTool(update_memory, is_read_only=False),
        ]
    return tools


# Map tool name → friendly process-stream label. Tool-call events carry
# tool_call_name; we render "正在<verb>".
_TOOL_LABELS = {
    "read_user_profile": "读取用户画像",
    "recall_memory": "召回记忆",
    "match_capabilities": "匹配能力",
    "read_skill": "读取技能方法论",
    "emit_proposal": "组装方案",
    "update_memory": "更新记忆",
}


# Agentic-mode system-prompt addendum (design D4: "system prompt 强约束产出方案
# 必须调 emit_proposal"). The base PLANNER_SYSTEM_PROMPT teaches a TEXT protocol
# (narrate clarifications, write <memory_update> tags, emit a JSON proposal block)
# — that is correct for the single-call fallback but WRONG for the ReAct loop,
# where the model has REAL callable tools. Without this addendum the model just
# narrates ("让我先读取用户画像") instead of calling read_user_profile, so the
# ReAct loop never iterates and the turn ends after one step. This block tells the
# model it is a tool-using ReAct expert and MUST act through the tools.
_AGENTIC_PROMPT_ADDENDUM = """

# ⚙️ 本轮运行在 Agentic ReAct 模式（务必遵守，覆盖上文的纯文本协议）

你不是只能输出文字的助手——你是一个能**自主调用工具**、多步推进的智能体专家。本轮你拥有以下**可直接调用的工具**（function calling），不要用文字描述"我将要读取/我需要了解"，而要**真正调用对应工具**：

只读采集工具（先用它们把信息查清楚，再推理）：
- `read_user_profile()`：读取当前用户画像（角色/部门/行业/偏好）。
- `recall_memory(query)`：召回与本次规划相关的长期记忆。
- `match_capabilities(goal)`：匹配平台可用能力（工具/技能/模型）。
- `read_skill(name)`：按需读取某技能的方法论。

产出工具（信息足够后用它们结构化产出，而不是把内容写进正文）：
- `update_memory(patch)`：更新规划记忆（requirement_summary / task_classification / 已确认约束等）。
- `request_confirmation(prompt, options)`：需要用户确认关键决策时，发起确认卡片（不要用文字罗列选项）。
- `emit_proposal(proposal)`：方案就绪时**必须**调用它提交结构化方案（不要把 JSON 写进聊天正文）。

工作准则：
1. **先行动后叙述**：需要某信息时直接调用对应工具，连续调用多个工具来收集足够上下文，每一步基于上一步结果继续推理。**绝不要**只说"让我先读取用户画像"然后就停下——那是错误的，必须真的调用 `read_user_profile()`。
2. **多步推进**：一轮内可以调用多个工具（如先 read_user_profile，再 match_capabilities，再 recall_memory），直到你有足够依据；信息足够时立即推进到方案。
3. **关键缺口才问**：仅当存在无法自行推断、且会显著改变方案的关键缺口时，才用 `request_confirmation` 发起确认；否则用 assumptions 记录合理假设，直接推进。
4. **结构化产出**：方案就绪时调用 `emit_proposal` 提交（其 proposal 字段沿用上文定义的 Proposal Schema：architecture_summary / architecture_pattern / agent_spec / nodes / edges 等）。确认类交互走 `request_confirmation`，记忆更新走 `update_memory`——都通过工具，不要写进聊天正文。
5. 给用户的可见正文只用于**自然语言说明**（你在做什么、方案概要、依据），结构化数据一律走工具。
6. **禁止用文字问问题**：需要用户回答/确认/选择时，**必须**调用 `request_confirmation`，绝不能在正文末尾写"是否符合？"、"确认后我将..."、"请选择..."等文字问句然后停下。如果你觉得方案已经可以确定，直接调 `emit_proposal`；如果必须等用户选择，调 `request_confirmation`。正文中出现问号等待回复 = 严重违规。
"""


def _build_agentic_system_prompt(base: str) -> str:
    """Append the ReAct tool-using directive to the base planner prompt (D4)."""
    return (base or "") + _AGENTIC_PROMPT_ADDENDUM


def _assemble_tail(turn_state: dict) -> str:
    """Re-emit the structured tool outputs as the canonical tags the caller's
    done-branch already understands, so both execution paths converge on the
    same extraction + finalize (design D8 "归一")."""
    tail = ""
    mu = turn_state.get("memory_update")
    if isinstance(mu, dict) and mu:
        tail += "\n<memory_update>" + json.dumps(mu, ensure_ascii=False) + "</memory_update>"
    a2ui = turn_state.get("a2ui")
    if isinstance(a2ui, dict) and a2ui.get("options"):
        tail += "\n<a2ui_request>" + json.dumps(a2ui, ensure_ascii=False) + "</a2ui_request>"
        _log.info("_assemble_tail: injected <a2ui_request> tag, options=%d", len(a2ui.get("options", [])))
    prop = turn_state.get("proposal")
    if isinstance(prop, dict) and prop:
        block = json.dumps({"ready": True, "proposal": prop}, ensure_ascii=False)
        tail += "\n```json\n" + block + "\n```"
    return tail


class _AgenticTextFilter:
    """Stream filter for the agentic text channel.

    AgentScope streams raw model text deltas; with ``thinking_enable=False`` some
    models (e.g. minimax) wrap reasoning in literal ``<think>...</think>`` text and
    "fake" structured output as ``<memory_update>`` / ``<a2ui_request>`` / fenced
    proposal JSON in the SAME text channel. The single-call path strips all of
    these before they reach the chat bubble — the agentic path must do the same.

    Because the agentic loop is multi-iteration ReAct, a single turn produces
    MANY ``<think>...</think>`` blocks — one (or more) per model call, interleaved
    with prose. The filter therefore tracks an arbitrary number of think blocks at
    any position in the stream, not just a leading one.

    ``feed(delta)`` returns ``(think_text, visible_text)``:
    - ``think_text`` — content inside ``<think>...</think>`` (routed to the WS
      ``thinking_content`` channel, shown in the collapsible 思考过程 panel).
    - ``visible_text`` — chat-safe prose with think + the three structured tags
      removed (the structured payloads are re-emitted by ``_assemble_tail`` for
      the caller's done-branch, so dropping them here only affects display)."""

    _OPEN = "<think>"
    _CLOSE = "</think>"

    def __init__(self) -> None:
        from app.api.planner.proposal import (
            _MemoryTagStripper, _A2UITagStripper, _ProposalJsonStripper,
        )
        self._buffer = ""
        self._in_think = False
        self._mem = _MemoryTagStripper()
        self._a2ui = _A2UITagStripper()
        self._prop = _ProposalJsonStripper()

    def _strip_tags(self, text: str) -> str:
        if not text:
            return ""
        return self._prop.feed(self._a2ui.feed(self._mem.feed(text)))

    @staticmethod
    def _hold_len(buf: str, tag: str) -> int:
        """Longest suffix of ``buf`` that is a (proper) prefix of ``tag`` — i.e. a
        partial tag split across deltas that must be held back until more arrives."""
        for i in range(min(len(tag) - 1, len(buf)), 0, -1):
            if tag.startswith(buf[-i:]):
                return i
        return 0

    def feed(self, delta: str):
        """Split a text delta into (think_text, visible_text). Handles any number
        of <think>...</think> blocks at any position, spanning deltas. Also tolerates
        a STRAY </think> with no matching opener (some gateways/models drop the
        opening tag and emit ``reasoning</think>answer``): the text before the stray
        close is treated as reasoning, not leaked into the chat bubble."""
        self._buffer += delta
        think_out = ""
        visible_raw = ""
        while True:
            if not self._in_think:
                idx_open = self._buffer.find(self._OPEN)
                idx_close = self._buffer.find(self._CLOSE)
                # A stray </think> that appears before any <think> means the opener
                # was dropped: treat everything up to it as reasoning, drop the tag.
                if idx_close != -1 and (idx_open == -1 or idx_close < idx_open):
                    think_out += self._buffer[:idx_close]
                    self._buffer = self._buffer[idx_close + len(self._CLOSE):]
                    continue
                if idx_open == -1:
                    # No opening tag yet; emit all but a possible partial-tag tail.
                    # Hold back a suffix that could grow into EITHER tag.
                    hold = max(self._hold_len(self._buffer, self._OPEN),
                               self._hold_len(self._buffer, self._CLOSE))
                    if hold:
                        visible_raw += self._buffer[:-hold]
                        self._buffer = self._buffer[-hold:]
                    else:
                        visible_raw += self._buffer
                        self._buffer = ""
                    break
                visible_raw += self._buffer[:idx_open]
                self._buffer = self._buffer[idx_open + len(self._OPEN):]
                self._in_think = True
            else:
                idx = self._buffer.find(self._CLOSE)
                if idx == -1:
                    # Still inside think; emit all but a possible partial close tag.
                    hold = self._hold_len(self._buffer, self._CLOSE)
                    if hold:
                        think_out += self._buffer[:-hold]
                        self._buffer = self._buffer[-hold:]
                    else:
                        think_out += self._buffer
                        self._buffer = ""
                    break
                think_out += self._buffer[:idx]
                self._buffer = self._buffer[idx + len(self._CLOSE):]
                self._in_think = False
        return think_out, self._strip_tags(visible_raw)

    def flush(self):
        """Drain any held buffers at stream end. Returns (think_text, visible_text)."""
        think_out = ""
        visible_raw = ""
        if self._in_think:
            think_out = self._buffer  # unterminated think — treat the rest as reasoning
        else:
            visible_raw = self._buffer
        self._buffer = ""
        visible = self._strip_tags(visible_raw)
        visible += self._prop.feed(self._a2ui.feed(self._mem.flush()))
        visible += self._prop.feed(self._a2ui.flush())
        visible += self._prop.flush()
        return think_out, visible


async def run_agentic_turn(
    *,
    system_prompt: str,
    model_cfg: dict,
    user_content: str,
    conversation_id: str,
    planning_context: Optional[dict],
    websocket,
    run_emitter,
    memory: Optional[dict] = None,
) -> dict:
    """Run one planner turn via AgentScope ReAct. Streams tokens/thinking/tool
    events to the websocket + run_emitter, and returns ``{"full_response": str}``
    with the structured tool outputs re-emitted as canonical tags appended to the
    text, so the caller's existing extraction (memory_update / a2ui / proposal)
    runs unchanged. Raises on any failure so the caller can fall back."""
    from agentscope.tool import Toolkit
    from agentscope.message import Msg, TextBlock
    from app.core.agentscope_runner import create_agent

    turn_state: dict = {}
    toolkit = Toolkit(tools=_build_tools(
        conversation_id=conversation_id, planning_context=planning_context,
        memory=memory, turn_state=turn_state,
    ))
    agent = create_agent(
        system_prompt=_build_agentic_system_prompt(system_prompt),
        model_name=model_cfg["model_name"],
        provider=model_cfg["provider"],
        stream=True,
        temperature=0.7,
        max_tokens=4096,
        base_url=model_cfg.get("base_url"),
        api_key=model_cfg.get("api_key"),
        toolkit=toolkit,
        react_max_iters=8,
    )

    full_response = ""
    text_filter = _AgenticTextFilter()
    _tool_args: dict = {}
    _tool_result: dict = {}
    _tool_name: dict = {}
    _event_counts: dict = {}  # diagnostics: how many of each AgentScope event type
    _tool_calls_seen = 0
    _iter_counter = 0  # monotonic tool-call counter for activity event IDs
    msg = Msg(name="user", content=[TextBlock(type="text", text=user_content)], role="user")

    async for ev in agent.reply_stream(msg):
        t = type(ev).__name__
        _event_counts[t] = _event_counts.get(t, 0) + 1
        if t == "ToolCallStartEvent":
            _tool_calls_seen += 1
        if t == "TextBlockDeltaEvent":
            delta = getattr(ev, "delta", "") or ""
            full_response += delta
            think_text, visible = text_filter.feed(delta)
            if think_text:
                try:
                    await websocket.send_json({"type": "thinking_content",
                                               "content": think_text})
                except Exception:
                    pass
                try:
                    await websocket.send_json({
                        "type": "activity",
                        "id": f"step_text_{_iter_counter}",
                        "phase": "step_text",
                        "label": "推理过程",
                        "status": "stream",
                        "detail": think_text[:500],
                    })
                except Exception:
                    pass
            if visible:
                try:
                    await websocket.send_json({"type": "token", "content": visible})
                except Exception:
                    pass
        elif t == "ThinkingBlockDeltaEvent":
            # Native thinking channel (thinking_enable models). Route to the
            # collapsible 思考过程 panel, never the chat bubble.
            _think_delta = getattr(ev, "delta", "") or ""
            try:
                await websocket.send_json({"type": "thinking_content",
                                           "content": _think_delta})
            except Exception:
                pass
            if _think_delta:
                try:
                    await websocket.send_json({
                        "type": "activity",
                        "id": f"step_text_{_iter_counter}",
                        "phase": "step_text",
                        "label": "推理过程",
                        "status": "stream",
                        "detail": _think_delta[:500],
                    })
                except Exception:
                    pass
        elif t == "ToolCallStartEvent":
            name = getattr(ev, "tool_call_name", "") or ""
            tcid = getattr(ev, "tool_call_id", "") or name
            label = _TOOL_LABELS.get(name, f"调用 {name}")
            _tool_calls_seen += 1
            _iter_counter += 1
            _tool_args[tcid] = ""
            _tool_name[tcid] = (name, _iter_counter)
            # request_confirmation is a silent tool — no UI step shown
            if name == "request_confirmation":
                pass
            else:
                try:
                    await run_emitter.emit(f"tool_call:{name}", "running", message=f"正在{label}")
                except Exception:
                    pass
                try:
                    await websocket.send_json({
                        "type": "activity",
                        "id": f"tool_{_iter_counter}_{name}",
                        "phase": "tool_call",
                        "label": label,
                        "status": "start",
                        "detail": "",
                    })
                except Exception:
                    pass
        elif t == "ToolCallDeltaEvent":
            # Streamed tool-call arguments (JSON fragments). Accumulate so the
            # completed step can surface a short args summary as detail.
            tcid = getattr(ev, "tool_call_id", "") or ""
            _tool_args[tcid] = _tool_args.get(tcid, "") + (getattr(ev, "delta", "") or "")
        elif t == "ToolResultTextDeltaEvent":
            # Streamed tool result text. Accumulate for the result summary detail.
            tcid = getattr(ev, "tool_call_id", "") or ""
            _tool_result[tcid] = _tool_result.get(tcid, "") + (getattr(ev, "delta", "") or "")
        elif t in ("ToolResultStartEvent", "ToolResultEndEvent"):
            name = getattr(ev, "tool_call_name", "") or ""
            tcid = getattr(ev, "tool_call_id", "") or name
            if t == "ToolResultStartEvent":
                continue
            # ToolResultEndEvent: the tool finished — emit completed with an
            # args/result summary so 「执行工具」 can be expanded to show the
            # real inputs + outputs (white-box).
            _name_iter = _tool_name.get(tcid)
            name = name or (_name_iter[0] if isinstance(_name_iter, tuple) else (_name_iter or ""))
            _tc_iter = _name_iter[1] if isinstance(_name_iter, tuple) else _iter_counter
            label = _TOOL_LABELS.get(name, name)
            details: dict = {}
            args_raw = (_tool_args.get(tcid) or "").strip()
            if args_raw:
                details["args"] = args_raw[:300]
            result_raw = (_tool_result.get(tcid) or "").strip()
            if result_raw:
                details["result"] = result_raw[:300]
            # request_confirmation is silent — no UI step
            if name == "request_confirmation":
                pass
            else:
                try:
                    await run_emitter.emit(f"tool_call:{name}", "completed",
                                           message=f"{label}完成", details=details)
                except Exception:
                    pass
                try:
                    await websocket.send_json({
                        "type": "activity",
                        "id": f"tool_{_tc_iter}_{name}",
                        "phase": "tool_call",
                        "label": label,
                        "status": "done",
                        "detail": (result_raw[:120] if result_raw else ""),
                    })
                except Exception:
                    pass
        elif t == "ExceedMaxItersEvent":
            # ReAct hit its iteration ceiling — surface a graceful note rather
            # than failing the turn (the assembled text/structured output, if any,
            # still flows to the caller's done-branch).
            try:
                await run_emitter.emit("tool_call:max_iters", "completed",
                                       message="已达推理步数上限，整理当前结果")
            except Exception:
                pass
        elif t == "RequireUserConfirmEvent":
            # All gathering tools are read-only and the agent runs in BYPASS
            # permission mode (set in create_agent), so this should not fire. If
            # it does, just log — the loop continues without blocking.
            _log.debug("agentic: unexpected RequireUserConfirmEvent (read-only/BYPASS)")
        # other events (ReplyStart/End, ModelCall*, *BlockStart/End) are ignored

    # Drain any text held back by the filter (partial tags / trailing think).
    tail_think, tail_visible = text_filter.flush()
    if tail_think:
        try:
            await websocket.send_json({"type": "thinking_content", "content": tail_think})
        except Exception:
            pass
    if tail_visible:
        try:
            await websocket.send_json({"type": "token", "content": tail_visible})
        except Exception:
            pass

    # Re-emit structured tool outputs as canonical tags for the shared done-branch.
    # Strip <think> blocks from the persisted text first: reasoning was already
    # streamed on the thinking_content channel and must not leak into the stored
    # assistant prose. Inline <memory_update>/<a2ui_request>/proposal tags are
    # KEPT — for models that "fake" tool calls as text, the caller's regex
    # fallback extracts them from full_response (turn_state stays empty).
    import re as _re
    cleaned = _re.sub(r"<think>[\s\S]*?</think>", "", full_response)
    # Stray </think> with no opener (dropped opening tag): drop everything up to
    # and including the first stray close — that prefix was reasoning.
    if "</think>" in cleaned and "<think>" not in cleaned:
        cleaned = _re.sub(r"^[\s\S]*?</think>", "", cleaned)
    # Unterminated leading think (no closing tag): drop everything from it on.
    if "<think>" in cleaned and "</think>" not in cleaned:
        cleaned = _re.sub(r"<think>[\s\S]*$", "", cleaned)
    full_response = cleaned.strip()

    # ── Post-hoc A2UI synthesis: model described a confirmation card in text
    # but never called request_confirmation tool ──────────────────────────
    # gpt-5.4 often writes "我已发起确认卡片" + bulleted options without calling
    # the tool. Detect this pattern and synthesize turn_state["a2ui"] so
    # _assemble_tail injects the <a2ui_request> tag for the done-branch.
    if (
        _tool_calls_seen == 0
        and not turn_state.get("a2ui")
        and not turn_state.get("proposal")
    ):
        _CONFIRM_HINTS = ("确认卡片", "等你选定", "请选择", "请确认", "发起了确认",
                          "发起确认", "等待确认", "确认后")
        if any(h in full_response for h in _CONFIRM_HINTS):
            # Try to extract bulleted options from the text
            # Pattern: "- **选项文字**" or "- 或 **选项文字**" or "- 选项文字"
            _opt_matches = _re.findall(
                r"[-•]\s*(?:或\s*)?\*{0,2}([^*\n]+?)\*{0,2}\s*$",
                full_response,
                _re.MULTILINE,
            )
            if _opt_matches and len(_opt_matches) <= 6:
                options = [
                    {"id": f"opt_{i}", "label": opt.strip()}
                    for i, opt in enumerate(_opt_matches)
                    if opt.strip() and len(opt.strip()) < 80
                ]
                if options:
                    import hashlib
                    _pid = "confirm_" + hashlib.md5(
                        full_response[:100].encode("utf-8")
                    ).hexdigest()[:8]
                    turn_state["a2ui"] = {
                        "id": _pid,
                        "topic": "方案确认",
                        "prompt": "请选择方案方向",
                        "options": options,
                        "allow_free_text": True,
                    }
                    _log.info(
                        "agentic post-hoc: synthesized a2ui from text options (%d opts)",
                        len(options),
                    )

    full_response += _assemble_tail(turn_state)

    # ── Post-hoc detection: model "narrated" completion without calling tools ──
    # Some models (notably gpt-5.4 through certain gateways) respond with text
    # like "已创建为草案" / "方案已提交" without actually invoking emit_proposal.
    # When this happens: tool_calls_seen == 0, turn_state has no proposal/a2ui,
    # and the text contains completion-suggesting phrases. In this case, append a
    # synthetic "你必须调用 emit_proposal 工具" reminder into full_response so the
    # caller's done-branch marks it as a parse failure with auto-repair hint —
    # triggering a retry turn where the model is explicitly told to use the tool.
    _COMPLETION_PHRASES = ("已创建", "已按方案", "已提交", "创建为草案", "方案已保存",
                           "创建成功", "已生成", "已落地")
    if (
        _tool_calls_seen == 0
        and not turn_state.get("proposal")
        and not turn_state.get("a2ui")
        and any(p in full_response for p in _COMPLETION_PHRASES)
    ):
        _log.warning(
            "agentic post-hoc: model=%s narrated completion without tool calls; "
            "injecting retry hint",
            model_cfg.get("model_name"),
        )
        # Clear the fake-completion text so the done-branch doesn't persist it
        # as a valid assistant message. Replace with an explicit retry directive.
        full_response = (
            '[系统提示：你在上一步只用文字描述了"已创建"，但并未调用 emit_proposal 工具，'
            "方案实际未保存。请现在调用 emit_proposal(proposal) 提交结构化方案。"
            "不要用文字描述创建过程，必须通过工具调用完成。]"
        )

    # Diagnostics: surface what the model actually did this turn. A model that
    # "narrates" tool use as text but never emits a real function call shows
    # tool_calls=0 here — the signal that it isn't truly agentic on this gateway.
    _log.info(
        "agentic turn done: model=%s tool_calls=%d events=%s structured=%s resp_len=%d",
        model_cfg.get("model_name"), _tool_calls_seen, _event_counts,
        {k: bool(turn_state.get(k)) for k in ("proposal", "a2ui", "memory_update", "skills_read")},
        len(full_response),
    )
    return {"full_response": full_response, "turn_state": turn_state}
