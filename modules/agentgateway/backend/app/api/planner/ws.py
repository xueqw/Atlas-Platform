"""Planner WebSocket handler — streaming turn loop and message dispatch."""

import json
import os
import re
import uuid
from typing import Optional
from datetime import datetime, timedelta, timezone
from fastapi import WebSocket, WebSocketDisconnect
from app.core.agentscope_runner import EmptyModelResponse, create_agent, run_conversation
from app.core import redis_client
from app.core.observability import (
    _get_langfuse,
    set_active_span,
    reset_active_span,
)
from app.core import planner_files
from . import state
from .state import (
    _new_memory,
    _ensure_goal_anchor,
    _merge_memory_update,
    _normalize_pending_a2ui,
    _build_decision_entry,
    _is_default_skill,
    MAX_ATTACHMENTS_PER_TURN,
    MAX_TOTAL_ATTACHMENT_BYTES,
    _PROPOSAL_FILE_MARKER,
)
from .proposal import (
    _MemoryTagStripper,
    _A2UITagStripper,
    _ProposalJsonStripper,
    _extract_memory_update,
    _extract_a2ui_request,
    _try_extract_proposal,
    _strip_proposal_text,
    _build_short_summary,
    _proposal_meta,
    _validate_proposal_payload,
    _match_capability_candidates,
    _persist_proposal_to_memory,
    recommend_runtime_mode,
)
from .prompts import (
    _build_system_prompt_with_memory,
    _render_context_block,
    _resolve_model,
    resolve_effective_skills,
    resolve_action_skill,
)
from .sessions import (
    _load_session_into_memory,
    _infer_stage,
    _persist_session,
    _pending_a2ui_from_memory,
    _pending_continuation_from_memory,
)
from .sessions import _session_exists_any_scope
from .scope import coerce_planner_scope, scope_from_websocket
from app.core.model_caps import DEFAULT_CHAT_MODEL_ID, DEFAULT_CHAT_PROVIDER


# Human-readable labels for the activity phases the planner emits each turn.
# Phases are limited to boundaries we can detect reliably from existing signals
# (turn start, first visible token, proposal generation, confirmation request) —
# no dependency on the model self-reporting its stage.
_ACTIVITY_LABELS = {
    "processing": "理解需求",
    "drafting": "起草回复",
    "composing_proposal": "生成方案",
    "awaiting_confirmation": "等待确认",
    "tool_call": "工具调用",
    "step_text": "推理过程",
}

_A2UI_ADVANCE_RE = re.compile(
    r"(确认|创建|生成|同意|继续|批准|采纳|就这样|没问题|可以了|开始|^是$|"
    r"\b(?:yes|confirm|create|apply|proceed|approve|ok|go)\b)",
    re.IGNORECASE,
)
_A2UI_NEGATIVE_RE = re.compile(
    r"(修改|调整|重新|再想|换成|取消|放弃|返回|^不|^否|"
    r"\b(?:cancel|modify|change|edit|no|back)\b)",
    re.IGNORECASE,
)
_CONTINUATION_LEASE_SECONDS = 300
_active_continuation_runs: set[str] = set()


def _is_advancing_a2ui_choice(choice_id: str, choice_label: str) -> bool:
    """Mirror the public A2UI proceed/modify contract on the authoritative side."""
    text = f"{choice_label} {choice_id}".strip()
    if not text or _A2UI_NEGATIVE_RE.search(text):
        return False
    # Explicit positive cues advance; all other non-negative options also
    # advance because selection cards commonly use domain labels only.
    return True


def _record_pending_continuation(
    memory: dict,
    *,
    request_id: str,
    choice_id: str,
    choice_label: str,
    continuation_token: str,
    free_text: str = "",
) -> Optional[dict]:
    """Create (or retain) the durable, exactly-once continuation hand-off."""
    if not _is_advancing_a2ui_choice(choice_id, choice_label):
        memory.pop("pending_continuation", None)
        return None
    current = memory.get("pending_continuation")
    if (
        isinstance(current, dict)
        and current.get("request_id") == request_id
        and current.get("choice") == choice_id
        and current.get("token") == continuation_token
        and current.get("status") in {"pending", "processing", "completed"}
    ):
        return current
    user_line = (
        f"已选择「{choice_label}」：{free_text.strip()}"
        if free_text.strip()
        else f"已选择「{choice_label}」"
    )
    pending = {
        "request_id": request_id,
        "choice": choice_id,
        "choice_label": choice_label,
        "token": continuation_token,
        "content": user_line,
        "status": "pending",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    memory["pending_continuation"] = pending
    return pending


def _complete_continuation(memory: dict, continuation_token: str) -> bool:
    """Move the claimed command to completed after its result is durable."""
    item = memory.get("pending_continuation")
    if not isinstance(item, dict) or item.get("token") != continuation_token:
        return False
    completed = dict(item)
    completed["status"] = "completed"
    completed["completed_at"] = datetime.now(timezone.utc).isoformat()
    completed.pop("lease_expires_at", None)
    memory["pending_continuation"] = completed
    _active_continuation_runs.discard(continuation_token)
    return True


def _continuation_lease_expired(item: dict) -> bool:
    raw = str(item.get("lease_expires_at") or "")
    try:
        expires_at = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        return expires_at <= datetime.now(timezone.utc)
    except (TypeError, ValueError):
        return True


def _planning_context_enabled() -> bool:
    """Feature flag for plan-first planning_context injection (T2, design D3).

    Default ON; set ``PLANNER_PLANNING_CONTEXT=off`` (or 0/false/no) to fall back
    to the legacy freeform path. Read per-call so it can be toggled without a
    process restart in tests."""
    raw = (os.environ.get("PLANNER_PLANNING_CONTEXT") or "on").strip().lower()
    return raw not in {"0", "off", "false", "no"}


def _agentic_enabled() -> bool:
    """Feature flag for the agentic ReAct planner loop (planner-agentic-react-loop).

    Default OFF — opt in via ``PLANNER_AGENTIC=on`` (1/true/yes). Even when on, a
    turn only runs agentic if the model supports function-calling; otherwise it
    falls back to the single-call path. Read per-call (toggle without restart)."""
    raw = (os.environ.get("PLANNER_AGENTIC") or "off").strip().lower()
    return raw in {"1", "on", "true", "yes"}


def _memory_used_explanation(mem_ctx) -> list:
    """Summarize which memories actually fed this turn (T9 memory_used_explanation).

    Reads the assembled LayeredContext's profile / semantic / episodic hits into
    short human-readable lines. Empty list when nothing was recalled — never
    fabricates. Best-effort; any shape mismatch yields []."""
    out: list = []
    try:
        for p in (getattr(mem_ctx, "profile", None) or [])[:5]:
            txt = getattr(p, "summary", "") or getattr(p, "content", "")
            if txt:
                out.append(f"画像：{txt[:80]}")
        for r in (getattr(mem_ctx, "semantic", None) or [])[:5]:
            txt = getattr(r, "summary", "") or getattr(r, "content", "")
            if txt:
                out.append(f"相关记忆：{txt[:80]}")
        for r in (getattr(mem_ctx, "episodic", None) or [])[:3]:
            txt = getattr(r, "summary", "") or getattr(r, "content", "")
            if txt:
                out.append(f"近期：{txt[:80]}")
    except Exception:
        return []
    return out


def _derive_dag_from_agent_spec(structured: dict) -> None:
    """Auto-derive a minimal DAG projection from agent_spec when nodes are empty.

    When the agentic loop's emit_proposal delivers a proposal with a populated
    agent_spec but empty nodes/edges (LLM omitted the DAG projection), this
    function generates the canonical P+M+Agent+O topology so the downstream
    apply path always receives a renderable graph.

    Mutates ``structured`` in place — only called when nodes is empty.
    """
    nodes = structured.get("nodes")
    if isinstance(nodes, list) and len(nodes) > 0:
        return  # Already has nodes — nothing to do.

    spec = structured.get("agent_spec")
    if not isinstance(spec, dict):
        return  # No agent_spec to derive from.

    identity = spec.get("identity") or {}
    runtime = spec.get("runtime") or {}
    if not identity and not runtime:
        return  # Both empty — can't derive anything meaningful.

    role_name = identity.get("role_name") or "AI助手"
    system_prompt = identity.get("system_prompt") or identity.get("role_description") or ""
    output_format = identity.get("output_format") or "markdown"

    model_name = runtime.get("model_name") or "glm-4-flash"
    provider = runtime.get("provider") or "glm"
    temperature = runtime.get("temperature", 0.7)
    max_tokens = runtime.get("max_tokens", 4096)
    streaming = runtime.get("streaming", True)

    structured["nodes"] = [
        {
            "id": "p1", "type": "p",
            "config": {"role_name": role_name, "system_prompt": system_prompt, "output_format": output_format},
        },
        {
            "id": "m1", "type": "m",
            "config": {"model_name": model_name, "provider": provider, "temperature": temperature,
                       "max_tokens": max_tokens, "streaming": streaming},
        },
        {
            "id": "agent1", "type": "agent",
            "config": {"role_name": role_name, "system_prompt": system_prompt, "output_format": output_format,
                       "model_name": model_name, "provider": provider, "temperature": temperature,
                       "max_tokens": max_tokens, "streaming": streaming},
        },
        {
            "id": "o1", "type": "o",
            "config": {},
        },
    ]
    structured["edges"] = [
        {"source": "p1", "target": "agent1", "targetHandle": "prompt"},
        {"source": "m1", "target": "agent1", "targetHandle": "model"},
        {"source": "agent1", "target": "o1"},
    ]


async def _send_activity(websocket: WebSocket, activity_id: str, phase: str,
                         status: str, detail: str | None = None,
                         skill: str | None = None,
                         skill_action: str | None = None) -> None:
    """Best-effort emit of a structured activity event.

    The activity timeline is purely informational; a send failure must never
    break the turn, so every emission is wrapped and swallowed. ``id`` pairs a
    ``start`` with its later ``done`` so the frontend can mark steps complete.

    ``skill``/``skill_action`` (change: add-planner-skill-transparency) attribute
    a deterministic action to the skill that conventionally owns it. They are
    additive — a frontend that only knows the base activity fields ignores them
    and still renders label/status. ``skill`` is set ONLY when the caller already
    confirmed it is in this turn's effective_skills (design D5).
    """
    try:
        payload = {
            "type": "activity",
            "id": activity_id,
            "phase": phase,
            "label": _ACTIVITY_LABELS.get(phase, phase),
            "status": status,
        }
        if detail:
            payload["detail"] = detail
        if skill:
            payload["skill"] = skill
        if skill_action:
            payload["skill_action"] = skill_action
        await websocket.send_json(payload)
    except Exception:
        pass


def _persist_and_store(conversation_id: str, conv_data: dict) -> None:
    """Persist the planner session to DB and flush working-state to the cache.

    ``_persist_session`` writes the durable PlannerSession row; ``store_conv``
    flushes the in-turn conv_data mutations to the working-state backend (a no-op
    extra cost on the in-process backend, a JSON flush on the Redis backend).
    Best-effort — a cache/DB hiccup must not break the turn.
    """
    try:
        _persist_session(conversation_id, conv_data)
    except Exception:
        pass
    try:
        state.store_conv(conversation_id, conv_data)
    except Exception:
        pass


async def planner_websocket(websocket: WebSocket, conversation_id: str):
    await websocket.accept()
    try:
        request_scope = scope_from_websocket(websocket)
    except Exception:
        await websocket.close(code=4400, reason="invalid planner scope")
        return
    state._active_websockets[conversation_id] = websocket

    restored_from_db = False
    conv_data = state.load_conv(conversation_id)
    if (
        conv_data is not None
        and coerce_planner_scope(conv_data.get("_scope")) != request_scope
    ):
        state._active_websockets.pop(conversation_id, None)
        await websocket.close(code=4404, reason="planner session not found")
        return
    if conv_data is None:
        loaded = _load_session_into_memory(conversation_id, request_scope)
        if loaded is not None:
            conv_data = loaded
            restored_from_db = True
        else:
            if _session_exists_any_scope(conversation_id):
                state._active_websockets.pop(conversation_id, None)
                await websocket.close(code=4404, reason="planner session not found")
                return
            conv_data = {
                "messages": [],
                "model": {"model_name": DEFAULT_CHAT_MODEL_ID, "provider": DEFAULT_CHAT_PROVIDER},
                "memory": _new_memory(),
                "file_artifacts": [],
                "_scope": request_scope.as_dict(),
            }
        state.store_conv(conversation_id, conv_data)

    model_cfg = conv_data.get("model", {"model_name": DEFAULT_CHAT_MODEL_ID, "provider": DEFAULT_CHAT_PROVIDER})
    memory = conv_data.setdefault("memory", _new_memory())
    conv_data.setdefault("file_artifacts", [])
    conv_data.setdefault("mode", "create")
    conv_data.setdefault("replan_context", "")
    if not conv_data.get("pending_a2ui"):
        pending_a2ui = _pending_a2ui_from_memory(memory)
        if pending_a2ui:
            conv_data["pending_a2ui"] = pending_a2ui

    await websocket.send_json({"type": "model_resolved", "model": model_cfg["model_name"], "provider": model_cfg["provider"]})

    # Send session_restored if there's existing memory or messages, or this is a
    # replan session (which carries injected context even before the first turn).
    if (
        restored_from_db
        or memory.get("requirement_summary")
        or memory.get("confirmed_constraints")
        or conv_data.get("messages")
        or conv_data.get("mode") == "replan"
    ):
        await websocket.send_json({
            "type": "session_restored",
            "memory": memory,
            "stage": _infer_stage(
                memory,
                conv_data.get("proposal"),
                conv_data.get("linked_agent_id") is not None,
                pending_a2ui=conv_data.get("pending_a2ui"),
            ),
            "messages": conv_data.get("messages", []),
            "file_artifacts": conv_data.get("file_artifacts", []),
            "mode": conv_data.get("mode") or "create",
            "replan_context": conv_data.get("replan_context") or "",
            "linked_agent_id": conv_data.get("linked_agent_id"),
            "pending_continuation": _pending_continuation_from_memory(memory),
        })
        pending_a2ui = conv_data.get("pending_a2ui")
        if isinstance(pending_a2ui, dict) and pending_a2ui.get("options"):
            await websocket.send_json({"type": "a2ui_request", **pending_a2ui})

    # ── Plan+Loop reconnect: push plan_update + pending confirmation ──
    try:
        from app.core.planner_plan import (
            load_plan, determine_session_mode, build_plan_update_payload, PlanStatus,
            plan_loop_enabled,
        )
        _reconnect_mode = "legacy"
        if plan_loop_enabled():
            _reconnect_mode = determine_session_mode(
                type("_S", (), {"planning_state_json": conv_data.get("planning_state_json", "{}")})()
            )
        if _reconnect_mode == "plan_loop":
            _rc_session = type("_S", (), {
                "planning_state_json": conv_data.get("planning_state_json", "{}"),
            })()
            _rc_plan = load_plan(_rc_session)
            if _rc_plan is not None:
                # Push plan snapshot
                await websocket.send_json(build_plan_update_payload(
                    conversation_id=conversation_id,
                    run_id="reconnect",
                    plan=_rc_plan,
                    stop_reason=None,
                ))
                # Re-push pending confirmation card if waiting_user
                if _rc_plan.status == PlanStatus.waiting_user:
                    for _rcs in _rc_plan.steps:
                        if _rcs.status.value == "waiting_user" and _rcs.confirmation:
                            await websocket.send_json({
                                "type": "a2ui_request",
                                "id": _rcs.confirmation.request_id,
                                "prompt": _rcs.confirmation.prompt,
                                "options": _rcs.confirmation.options,
                                "allow_free_text": _rcs.confirmation.kind == "missing_info",
                            })
                            break
    except Exception:
        pass

    try:
        while True:
            data = await websocket.receive_text()
            msg = json.loads(data)

            if msg.get("type") == "message":
                user_content = msg.get("content", "")
                attachments = msg.get("attachments") or []
                if not isinstance(attachments, list):
                    attachments = []
                continuation_token = str(msg.get("continuation_token") or "").strip()
                continuation_run_id = ""
                if continuation_token:
                    # Claim a durable command under the shared working-state lock.
                    # ``processing`` is leased, not terminal: if the socket/process
                    # disappears before a result is durable, a later replay can
                    # recover it with the same token/run_id.
                    lock_token = redis_client.acquire_lock(
                        f"planner-continuation:{conversation_id}", ttl=30
                    )
                    if not lock_token:
                        await websocket.send_json({
                            "type": "error",
                            "code": "continuation_busy",
                            "message": "确认续跑正在处理中，请稍后重试",
                            "recoverable": True,
                        })
                        continue
                    claim_error = ""
                    completed_continuation = False
                    busy_continuation = False
                    claimed_continuation: dict = {}
                    try:
                        # Redis working state is JSON round-tripped, so reload
                        # inside the distributed lock before checking the token.
                        latest = state.load_conv(conversation_id)
                        if isinstance(latest, dict):
                            conv_data = latest
                            memory = conv_data.setdefault("memory", _new_memory())
                        candidate = memory.get("pending_continuation")
                        request_id = str(msg.get("a2ui_request_id") or "").strip()
                        choice_id = str(msg.get("a2ui_choice") or "").strip()
                        if not isinstance(candidate, dict):
                            claim_error = "missing"
                        elif (
                            candidate.get("token") != continuation_token
                            or candidate.get("request_id") != request_id
                            or candidate.get("choice") != choice_id
                        ):
                            claim_error = "mismatch"
                        elif candidate.get("status") == "completed":
                            completed_continuation = True
                            claimed_continuation = dict(candidate)
                        else:
                            # A live local run owns its lease. A processing record
                            # without a live local owner is a crash/restart orphan
                            # and is safely reclaimed with the same run_id.
                            if (
                                candidate.get("status") == "processing"
                                and continuation_token in _active_continuation_runs
                                and not _continuation_lease_expired(candidate)
                            ):
                                busy_continuation = True
                                claimed_continuation = dict(candidate)
                            elif candidate.get("status") not in {
                                "pending", "processing", "consumed"
                            }:
                                claim_error = "invalid_status"
                            else:
                                _active_continuation_runs.discard(continuation_token)
                                claimed_continuation = dict(candidate)
                                now = datetime.now(timezone.utc)
                                continuation_run_id = str(
                                    claimed_continuation.get("run_id")
                                    or uuid.uuid5(
                                        uuid.NAMESPACE_URL,
                                        f"atlas-planner-continuation:{continuation_token}",
                                    ).hex
                                )
                                claimed_continuation.update({
                                    "status": "processing",
                                    "run_id": continuation_run_id,
                                    "claimed_at": now.isoformat(),
                                    "lease_expires_at": (
                                        now + timedelta(seconds=_CONTINUATION_LEASE_SECONDS)
                                    ).isoformat(),
                                })
                                claimed_continuation.pop("consumed_at", None)
                                memory["pending_continuation"] = claimed_continuation
                                conv_data["memory"] = memory
                                _persist_and_store(conversation_id, conv_data)
                                _active_continuation_runs.add(continuation_token)
                    finally:
                        redis_client.release_lock(
                            f"planner-continuation:{conversation_id}", lock_token
                        )
                    if claim_error:
                        await websocket.send_json({
                            "type": "error",
                            "code": "invalid_continuation",
                            "message": "确认续跑凭证无效或已失效",
                            "recoverable": False,
                        })
                        continue
                    if completed_continuation:
                        await websocket.send_json({
                            "type": "continuation_completed",
                            "continuation_token": continuation_token,
                            "duplicate": True,
                        })
                        continue
                    if busy_continuation:
                        await websocket.send_json({
                            "type": "error",
                            "code": "continuation_busy",
                            "message": "确认续跑正在处理中，请等待当前运行完成",
                            "recoverable": True,
                        })
                        continue
                    # Canonical content comes from the durable decision record,
                    # preventing a valid token from being paired with new text.
                    user_content = str(claimed_continuation.get("content") or "")
                    attachments = []
                    await websocket.send_json({
                        "type": "continuation_claimed",
                        "continuation_token": continuation_token,
                        "run_id": continuation_run_id,
                        "duplicate": False,
                    })
                # Per-turn caps (design D7): count + total bytes. Reject loudly
                # rather than silently truncating.
                if len(attachments) > MAX_ATTACHMENTS_PER_TURN:
                    await websocket.send_json({
                        "type": "error",
                        "message": f"附件数量超过上限（最多 {MAX_ATTACHMENTS_PER_TURN} 个）",
                    })
                    continue
                total_bytes = sum(int(a.get("size") or 0) for a in attachments if isinstance(a, dict))
                if total_bytes > MAX_TOTAL_ATTACHMENT_BYTES:
                    await websocket.send_json({
                        "type": "error",
                        "message": "附件总大小超过上限（12 MiB）",
                    })
                    continue
                existing_continuation_result = False
                if continuation_token:
                    existing_continuation_result = any(
                        isinstance(item, dict)
                        and item.get("role") == "assistant"
                        and item.get("continuation_token") == continuation_token
                        for item in conv_data["messages"]
                    )
                    if not any(
                        isinstance(item, dict)
                        and item.get("role") == "user"
                        and item.get("continuation_token") == continuation_token
                        for item in conv_data["messages"]
                    ):
                        conv_data["messages"].append({
                            "role": "user",
                            "content": user_content,
                            "continuation_token": continuation_token,
                            "continuation_run_id": continuation_run_id,
                        })
                else:
                    conv_data["messages"].append({"role": "user", "content": user_content})
                if existing_continuation_result:
                    _complete_continuation(memory, continuation_token)
                    conv_data["memory"] = memory
                    _persist_and_store(conversation_id, conv_data)
                    await websocket.send_json({
                        "type": "continuation_completed",
                        "continuation_token": continuation_token,
                        "run_id": continuation_run_id,
                        "duplicate": True,
                    })
                    continue
                # Anchor user-authored goal evidence before any model-authored
                # memory candidate is allowed to update the materialized view.
                _ensure_goal_anchor(
                    memory,
                    conv_data["messages"],
                    conversation_id=conversation_id,
                )

                # Per-turn activity tracking. ``turn_seq`` namespaces this turn's
                # activity ids so start/done pairs never collide across turns.
                turn_seq = len(conv_data["messages"])
                act_processing = f"t{turn_seq}-processing"
                act_drafting = f"t{turn_seq}-drafting"
                drafting_started = False
                # NOTE: the opening activity/reasoning step is emitted below, once
                # the execution path (_use_agentic) is known — agentic turns get a
                # dynamic "正在分析" run event instead of the fixed 理解需求 activity.

                # Resolve this turn's effective skills ONCE (used to GATE action
                # attribution, design D5 — a proposal action is only credited to a
                # skill that is actually mounted this turn). NOT displayed as a
                # banner: which skills are enabled is already visible in the skill
                # panel; what the user wants to see is which skill actually fired.
                turn_effective = resolve_effective_skills(
                    memory.get("selected_skills") or []
                )["effective_skills"]
                # Skills this turn ACTUALLY triggered (an observable backend action
                # ran that conventionally belongs to the skill). Populated at the
                # action points below; drives the result badge.
                turn_triggered: list[str] = []

                # Planner run-event timeline (T8). One run_id per turn groups the
                # normalized stage events; the emitter both persists a row and
                # pushes a real-time ``run_event`` (alongside the unchanged
                # ``activity`` stream). All emits are best-effort.
                from app.core.run_events import RunEventEmitter
                run_emitter = RunEventEmitter(
                    run_id=continuation_run_id or uuid.uuid4().hex,
                    websocket=websocket,
                )

                # Re-read the model config each turn so a mid-session set_model
                # takes effect on the next turn. Credentials (if any) come from
                # the capability-library config and stay backend-only.
                model_cfg = conv_data.get("model", {"model_name": DEFAULT_CHAT_MODEL_ID, "provider": DEFAULT_CHAT_PROVIDER})

                # One retry budget per logical planner turn, shared by every
                # execution family.  This prevents an agentic empty response
                # from falling back to a single-call path that then retries
                # twice (three provider attempts total).
                _model_attempt_count = 0
                _MODEL_ATTEMPT_LIMIT = 2
                _empty_response_exhausted = False

                def _reserve_model_attempt(path: str) -> int:
                    nonlocal _model_attempt_count
                    if _model_attempt_count >= _MODEL_ATTEMPT_LIMIT:
                        raise EmptyModelResponse(
                            model_name=str(model_cfg.get("model_name") or ""),
                            response_mode=path,
                            reason="retry_budget_exhausted",
                        )
                    _model_attempt_count += 1
                    return _model_attempt_count

                async def _emit_model_failure(
                    exc: EmptyModelResponse,
                    *,
                    attempt: int,
                    path: str,
                ) -> bool:
                    nonlocal _empty_response_exhausted
                    retrying = _model_attempt_count < _MODEL_ATTEMPT_LIMIT
                    await run_emitter.emit(
                        "model_response",
                        "failed",
                        message=(
                            "模型返回空响应，正在重试"
                            if retrying
                            else "模型连续返回空响应"
                        ),
                        details={
                            "attempt": attempt,
                            "retrying": retrying,
                            "reason": exc.reason,
                            "finish_reason": exc.finish_reason,
                            "response_mode": exc.response_mode,
                            "model": exc.model_name,
                            "path": path,
                        },
                    )
                    if retrying:
                        await run_emitter.emit(
                            "model_response_retry",
                            "running",
                            message="正在重试模型请求",
                            details={
                                "attempt": _model_attempt_count + 1,
                                "cause": exc.reason,
                                "from_path": path,
                            },
                        )
                    else:
                        _empty_response_exhausted = True
                        await run_emitter.emit(
                            "model_response_retry",
                            "failed",
                            message="模型重试仍为空响应",
                            details={
                                "attempt": attempt,
                                "reason": exc.reason,
                                "finish_reason": exc.finish_reason,
                                "path": path,
                            },
                        )
                    return retrying

                async def _budgeted_run_conversation(
                    agent,
                    content: str,
                    attachments=None,
                    *,
                    path: str = "plan_loop",
                ):
                    """Run a model stream under the turn-wide two-attempt budget."""
                    while _model_attempt_count < _MODEL_ATTEMPT_LIMIT:
                        attempt = _reserve_model_attempt(path)
                        saw_usable_content = False
                        try:
                            async for ev in run_conversation(
                                agent,
                                content,
                                attachments=attachments,
                            ):
                                event_kind, event_content = ev
                                if (
                                    event_kind == "token"
                                    and str(event_content or "").strip()
                                ):
                                    saw_usable_content = True
                                if event_kind == "done":
                                    try:
                                        done_meta = json.loads(event_content or "{}")
                                    except (json.JSONDecodeError, TypeError):
                                        done_meta = {}
                                    if not isinstance(done_meta, dict):
                                        done_meta = {}
                                    if not saw_usable_content:
                                        raise EmptyModelResponse(
                                            model_name=str(model_cfg.get("model_name") or ""),
                                            finish_reason=done_meta.get("finish_reason"),
                                            response_mode=str(
                                                done_meta.get("response_mode")
                                                or "orchestration_contract"
                                            ),
                                            reason="empty_content",
                                        )
                                    done_meta.update({"attempt": attempt, "path": path})
                                    await run_emitter.emit(
                                        "model_response",
                                        "completed",
                                        message="模型响应完成",
                                        details=done_meta,
                                    )
                                    if attempt > 1:
                                        await run_emitter.emit(
                                            "model_response_retry",
                                            "completed",
                                            message="模型重试成功",
                                            details={"attempt": attempt, "path": path},
                                        )
                                yield ev
                            return
                        except EmptyModelResponse as exc:
                            retrying = await _emit_model_failure(
                                exc,
                                attempt=attempt,
                                path=path,
                            )
                            if not retrying:
                                raise

                # Decide the execution path UP FRONT (planner-agentic-react-loop):
                # agentic ReAct runs when the flag is on AND the model supports
                # function-calling. We need this before emitting the fixed pre-LLM
                # stage events, because in agentic mode the planner gathers context
                # via its OWN tools — so the fixed intent/context/memory steps are
                # redundant and must NOT be shown (only the real tool calls are).
                _use_agentic = False
                _use_plan_loop = False
                if _agentic_enabled():
                    try:
                        from app.core.model_caps import supports_function_calling
                        _use_agentic = supports_function_calling(model_cfg.get("model_name", ""))
                    except Exception:
                        _use_agentic = False

                # Plan+Loop mode: when feature flag is on, new sessions use the
                # Plan+Loop engine instead of the agentic or single-call path.
                # Existing plan_loop sessions always resume via this path regardless
                # of the feature flag state — UNLESS the flag is explicitly OFF,
                # in which case we ignore stored plan_loop state and use legacy.
                from app.core.planner_plan import plan_loop_enabled, determine_session_mode
                if plan_loop_enabled():
                    _plan_loop_session_mode = determine_session_mode(
                        type("_S", (), {"planning_state_json": conv_data.get("planning_state_json", "{}")})()
                    )
                else:
                    _plan_loop_session_mode = "legacy"
                if _plan_loop_session_mode == "plan_loop":
                    _use_plan_loop = True
                    _use_agentic = False  # plan_loop supersedes agentic

                # Fixed-stage emitter: in agentic/plan_loop mode these pre-LLM
                # stage steps are suppressed. Plan+Loop has its own step events.
                async def _stage_emit(*a, **kw):
                    if not _use_agentic and not _use_plan_loop:
                        await run_emitter.emit(*a, **kw)

                # Coarse activity script (理解需求 / 起草回复 / 生成方案 / 等待确认):
                # honest only for the single-call path, which has fixed boundaries.
                # In agentic/plan_loop mode the process stream is driven by real
                # actions, so this static script is suppressed.
                async def _activity(websocket, activity_id, phase, status, *a, **kw):
                    if not _use_agentic and not _use_plan_loop:
                        await _send_activity(websocket, activity_id, phase, status, *a, **kw)

                if _use_agentic:
                    # Give the agentic turn an honest opener BEFORE the first tool
                    # call: a single "reasoning" step so the user sees real activity
                    # immediately, dynamically replaced by tool_call:* steps as the
                    # model acts. No fixed 理解需求/读取信息/召回记忆 script.
                    await run_emitter.emit("reasoning", "running", message="正在分析")
                elif not _use_plan_loop:
                    await _send_activity(websocket, act_processing, "processing", "start")

                await _stage_emit("intent_inference", "running", message="正在理解需求")

                # Rebuild agent with latest memory each turn
                system_prompt = _build_system_prompt_with_memory(
                    memory,
                    mode=conv_data.get("mode") or "create",
                    replan_context=conv_data.get("replan_context") or "",
                )

                # Layered-memory injection (task 4.2). Replan sessions are agent-
                # scoped (linked_agent_id drives the L3 policy); create sessions
                # have no agent yet, so assemble resolves to disabled→empty and the
                # block is "". Best-effort; never breaks the planner turn.
                await _stage_emit("intent_inference", "completed", message="需求已理解")
                await _stage_emit("memory_recall", "running", message="正在读取分层记忆")
                # Granular per-layer sub-steps (planner-granular-step-hooks):
                # collect synchronously during assembly, then emit each real layer
                # as its own run_event so the recall shows step-by-step instead of
                # one lump. Labels for the white-box process stream.
                _mem_layer_labels = {
                    "session_history": "读取会话历史", "profile": "召回用户画像记忆",
                    "procedural": "加载已挂载技能", "episodic": "召回情景记忆",
                    "disabled": "长期记忆",
                }
                _mem_substeps: list = []
                def _on_layer(layer, phase, hits, _c=_mem_substeps):
                    _c.append((layer, phase, hits))
                try:
                    from app.core import memory_service
                    is_replan = (conv_data.get("mode") or "create") == "replan"
                    mem_ctx = memory_service.assemble_layered_context(
                        memory_service.SCENARIO_REPLAN if is_replan else memory_service.SCENARIO_PLANNER,
                        agent_id=conv_data.get("linked_agent_id"),
                        planner_conversation_id=conversation_id,
                        query=user_content,
                        on_layer=_on_layer,
                    )
                    # Replay collected layer sub-steps as individual run_events.
                    # These ARE the memory_recall detail — the parent step stays a
                    # group header only (no duplicate "完成/无记忆" total line).
                    _total_recalled = 0
                    _recall_sources: list = []
                    for _layer, _phase, _hits in _mem_substeps:
                        if _phase != "done":
                            continue
                        _label = _mem_layer_labels.get(_layer, _layer)
                        _msg = (f"{_label} · 命中 {_hits} 条" if _layer != "disabled"
                                else "无长期记忆")
                        await _stage_emit(f"memory_recall:{_layer}", "completed",
                                               message=_msg, details={"hit_count": _hits})
                        if _layer != "disabled" and isinstance(_hits, int) and _hits > 0:
                            _total_recalled += _hits
                            _recall_sources.append(f"{_label}({_hits}条)")
                    mem_block = memory_service.render_layered_context_block(mem_ctx)
                    if mem_block:
                        system_prompt = system_prompt + mem_block
                    await _stage_emit("memory_recall", "completed",
                                      details={"recalled_count": _total_recalled,
                                               "sources": _recall_sources[:5]})
                    # Memory observability (T9): push a memory_event over the live
                    # socket + persist it, so the white-box view reflects the
                    # recall. Best-effort. memory_used_explanation surfaces which
                    # memories actually fed the turn.
                    try:
                        from app.core.memory_events import emit_memory_event
                        from app.core.memory_provider import get_memory_provider
                        mem_used = _memory_used_explanation(mem_ctx)
                        conv_data["memory_used_explanation"] = mem_used
                        await emit_memory_event(
                            websocket=websocket,
                            provider=getattr(get_memory_provider(), "name", "none"),
                            event_type="recall",
                            status="success",
                            run_id=run_emitter.run_id,
                            source="planner_recall",
                            query_or_reason=user_content[:500],
                            payload_summary=("命中记忆" if mem_used else "无相关长期记忆"),
                            details={"memory_used_explanation": mem_used},
                        )
                    except Exception:
                        pass
                except Exception as exc:
                    await _stage_emit("memory_recall", "failed", message=f"记忆读取失败：{exc}")

                # Plan-first planning_context injection (T2, design D1/D3).
                # Aggregate the seven-segment context and render it as a compact
                # structured block appended to the system prompt — replacing
                # reliance on freeform memory alone. Best-effort + feature-gated:
                # the aggregator never raises (T1 degrade contract), and the flag
                # lets us fall back to the legacy path. The block is also stashed
                # on conv_data so proposal extraction can derive capability_candidates.
                conv_data.pop("planning_context", None)
                if _planning_context_enabled():
                    await _stage_emit("context_load", "running", message="正在聚合规划上下文")
                    # Granular per-builder sub-steps (planner-granular-step-hooks):
                    # collect each real builder synchronously, then emit one
                    # run_event per sub-step (读画像/读工作区/读能力/读策略…) so the
                    # context phase shows step-by-step instead of one lump.
                    _ctx_labels = {
                        "user_input": "解析用户输入", "user_profile": "读取用户画像",
                        "memory_context": "读取记忆上下文", "workspace_context": "读取工作区",
                        "external_context": "读取外部系统", "internal_context": "读取平台能力",
                        "policy_context": "读取治理策略",
                    }
                    _ctx_substeps: list = []
                    def _on_ctx_step(seg, phase, detail, _c=_ctx_substeps):
                        _c.append((seg, phase, detail))
                    try:
                        from app.core.planning_context import build_planning_context
                        planning_ctx = build_planning_context(
                            conversation_id=conversation_id,
                            user_input={"goal_text": user_content},
                            on_step=_on_ctx_step,
                        )
                        # Replay collected builder sub-steps as individual run_events
                        # with their real detail (role / systems / capability count…).
                        for _seg, _phase, _detail in _ctx_substeps:
                            if _phase == "start":
                                continue
                            _label = _ctx_labels.get(_seg, _seg)
                            _msg = _label
                            if _phase == "failed":
                                _msg = f"{_label} · 失败"
                            elif _seg == "user_profile" and _detail.get("role"):
                                _msg = f"{_label} · {_detail['role']}"
                            elif _seg == "internal_context":
                                _msg = f"{_label} · {_detail.get('capability_count', 0)} 项能力"
                            elif _seg == "workspace_context" and _detail.get("connected_systems"):
                                _msg = f"{_label} · {'、'.join(map(str, _detail['connected_systems']))}"
                            await _stage_emit(
                                f"context_load:{_seg}",
                                "failed" if _phase == "failed" else "completed",
                                message=_msg, details=_detail,
                            )
                        ctx_block = _render_context_block(planning_ctx)
                        if ctx_block:
                            system_prompt = system_prompt + ctx_block
                        conv_data["planning_context"] = planning_ctx
                        # Parent context_load: completed for status only — NO
                        # message. The "可用能力 N 项" content is already shown by
                        # the context_load:internal_context sub-step; a parent
                        # total line here would duplicate it.
                        await _stage_emit("context_load", "completed")
                    except Exception as exc:
                        await _stage_emit("context_load", "failed", message=f"上下文聚合失败：{exc}")

                # ── Plan+Loop execution path ──────────────────────────────────
                # When plan_loop mode is active, the Loop Runner drives step
                # execution. ws.py only provides the WS transport and persists
                # the session. The entire "create agent → stream → done-branch"
                # below is skipped; the loop emits its own plan_update/run_event
                # events and produces a final assistant message.
                if _use_plan_loop:
                    class _PlanLoopSkip(Exception):
                        """Raised when Plan+Loop should be skipped (e.g. greetings)."""
                        pass
                    try:
                        from app.core.planner_plan import (
                            create_initial_plan, load_plan, save_plan,
                            build_plan_update_payload, PlanStatus, StepStatus,
                        )
                        from app.core.planner_loop import (
                            run_until_pause_or_complete, StepResult,
                        )
                        from app.core.planner_event_emitter import (
                            PlannerEventEmitter, DefaultPlanPersister,
                        )
                        from app.core.planner_step_executors import (
                            ExecutorDispatcher, DeterministicExecutor,
                            LLMExecutor, UserConfirmationExecutor, ToolReactExecutor,
                        )
                        from app.core.planner_tool_executor import execute_tool_react_step
                        from app.core.planner_step_handlers import (
                            PlannerLoopContext, PlannerStepDeps,
                            build_step_handler_registry,
                            resolve_a2ui_response, resolve_missing_info_text,
                            is_plan_waiting_user, get_waiting_step,
                            A2UIResolutionError,
                        )

                        # Use real session row for persistence
                        from app.core.planner_session_repo import get_or_create_planner_session
                        _session_row = await get_or_create_planner_session(
                            conversation_id,
                            request_scope,
                        )

                        # Load or create plan from real session
                        plan = load_plan(_session_row)
                        if plan is None:
                            # Only create a plan if the user message expresses
                            # clear intent to build/create an agent. Casual greetings
                            # or questions should fall through to the normal LLM path.
                            import re as _re
                            _CREATE_INTENT_RE = _re.compile(
                                r"(创建|构建|做一个|设计|搭建|开发|生成|制作|帮我做|帮我建|我要做|我想做|我想创建|我需要一个|"
                                r"build|create|make|design|develop|set up)",
                                _re.IGNORECASE,
                            )
                            if not _CREATE_INTENT_RE.search(user_content):
                                # Not a creation request — skip Plan+Loop, fall through
                                # to the normal single-call LLM path below.
                                pass  # will exit the plan_loop block via the flag below
                            else:
                                plan = create_initial_plan(user_content, conv_data.get("mode", "create"))

                        # If no plan (greeting/question), skip Plan+Loop entirely
                        if plan is None:
                            # Fall through to normal LLM path
                            raise _PlanLoopSkip()

                        # If plan is already completed, don't re-run the loop.
                        # This handles the case where frontend auto-continues after
                        # a confirmation but the plan finished in the same turn.
                        if plan.status == PlanStatus.completed:
                            if continuation_token:
                                _complete_continuation(memory, continuation_token)
                                conv_data["memory"] = memory
                            _persist_and_store(conversation_id, conv_data)
                            if continuation_token:
                                await websocket.send_json({
                                    "type": "continuation_completed",
                                    "continuation_token": continuation_token,
                                    "run_id": continuation_run_id,
                                    "duplicate": False,
                                })
                            await websocket.send_json({"type": "done"})
                            continue

                        # ── Handle waiting_user state ──
                        # Plain text does NOT auto-confirm. Only missing_info steps
                        # accept plain text as supplement.
                        if plan.status == PlanStatus.waiting_user:
                            waiting_step = get_waiting_step(plan)
                            if waiting_step and waiting_step.confirmation:
                                if waiting_step.confirmation.kind == "missing_info":
                                    # Plain text resolves missing_info
                                    updated = resolve_missing_info_text(plan, user_content)
                                    if updated:
                                        plan = updated
                                else:
                                    # For proposal_confirm etc, plain text is NOT auto-confirm.
                                    # Send a hint and continue waiting.
                                    await websocket.send_json({
                                        "type": "run_event",
                                        "run_id": run_emitter.run_id,
                                        "conversation_id": conversation_id,
                                        "step": waiting_step.id,
                                        "event_kind": "hint",
                                        "status": "waiting_user",
                                        "message": "请通过确认卡操作（确认/修改/拒绝），或补充信息后再试",
                                    })
                                    # Persist user message for context
                                    if continuation_token:
                                        _complete_continuation(memory, continuation_token)
                                        conv_data["memory"] = memory
                                    _persist_and_store(conversation_id, conv_data)
                                    await websocket.send_json({"type": "done"})
                                    continue
                            # Clear pending a2ui for next round
                            conv_data.pop("pending_a2ui", None)

                        # ── Build step handler deps ──
                        _step_deps = PlannerStepDeps(
                            conversation_id=conversation_id,
                            run_id=run_emitter.run_id,
                            user_content=user_content,
                            conv_data=conv_data,
                            memory=memory,
                            model_cfg=model_cfg,
                            websocket=websocket,
                            build_planning_context=lambda **kw: __import__(
                                "app.core.planning_context", fromlist=["build_planning_context"]
                            ).build_planning_context(**kw),
                            render_context_block=_render_context_block,
                            build_system_prompt_with_memory=_build_system_prompt_with_memory,
                            match_capability_candidates=_match_capability_candidates,
                            try_extract_proposal=_try_extract_proposal,
                            extract_a2ui_request=_extract_a2ui_request,
                            extract_memory_update=_extract_memory_update,
                            merge_memory_update=_merge_memory_update,
                            normalize_pending_a2ui=_normalize_pending_a2ui,
                            validate_proposal_payload=_validate_proposal_payload,
                            write_proposal_json=planner_files.write_proposal_json,
                            merge_artifacts=planner_files.merge_artifacts,
                            create_agent=create_agent,
                            run_conversation=_budgeted_run_conversation,
                            create_strippers=lambda: (_MemoryTagStripper(), _A2UITagStripper(), _ProposalJsonStripper()),
                            emitter=None,  # Set after emitter creation below
                        )

                        # Build handler registry from deps
                        _det_services, _llm_step_handlers = build_step_handler_registry(_step_deps)

                        async def _llm_dispatch(step, ctx):
                            handler = _llm_step_handlers.get(step.id)
                            if handler:
                                return await handler(step, ctx)
                            return {"warning": f"No LLM handler for {step.id}"}

                        # Attach execution context to session row for dispatcher
                        _session_row._exec_context = {
                            "conversation_id": conversation_id,
                            "run_id": run_emitter.run_id,
                            "user_input": user_content,
                            "model_cfg": model_cfg,
                            "memory": memory,
                            "planning_context": conv_data.get("planning_context"),
                        }

                        dispatcher = ExecutorDispatcher(
                            deterministic=DeterministicExecutor(services=_det_services),
                            llm=LLMExecutor(llm_handler=_llm_dispatch),
                            user_confirmation=UserConfirmationExecutor(),
                            tool_react=ToolReactExecutor(react_handler=execute_tool_react_step),
                        )

                        # Build event emitter and persister
                        emitter = PlannerEventEmitter(
                            websocket=websocket,
                            conversation_id=conversation_id,
                            run_id=run_emitter.run_id,
                        )
                        _step_deps.emitter = emitter
                        persister = DefaultPlanPersister()

                        # Run the loop
                        loop_result = await run_until_pause_or_complete(
                            session=_session_row,
                            plan=plan,
                            executor=dispatcher,
                            emitter=emitter,
                            persister=persister,
                        )

                        # Auto-skip confirm_proposal when proposal was already
                        # generated (user confirmed via LLM's multi-turn a2ui).
                        # This prevents a redundant "是否确认" card after the
                        # proposal.json is already delivered.
                        if (
                            loop_result.stop_reason.value == "waiting_user"
                            and conv_data.get("_skip_confirm_proposal")
                        ):
                            _skip_step = None
                            for _ws in loop_result.plan.steps:
                                if _ws.id == "confirm_proposal" and _ws.status.value == "waiting_user":
                                    _skip_step = _ws
                                    break
                            if _skip_step:
                                # Auto-confirm: mark done and resume loop
                                _skip_step.status = StepStatus.done
                                _skip_step.outputs["confirmation_selected"] = "auto_confirmed"
                                _skip_step.outputs["reason"] = "proposal already confirmed via LLM a2ui"
                                loop_result.plan.status = PlanStatus.running
                                from app.core.planner_plan import next_runnable_step as _nrs
                                _next, _ = _nrs(loop_result.plan)
                                loop_result.plan.current_step_id = _next.id if _next else None
                                # Emit plan_update so frontend knows confirm is done
                                await emitter.emit_step_completed(_skip_step, 0)
                                await emitter.emit_plan_update(loop_result.plan)
                                # Continue loop for remaining steps (compile_draft)
                                loop_result = await run_until_pause_or_complete(
                                    session=_session_row,
                                    plan=loop_result.plan,
                                    executor=dispatcher,
                                    emitter=emitter,
                                    persister=persister,
                                )
                            conv_data.pop("_skip_confirm_proposal", None)

                        # Persist plan state to conv_data for compatibility
                        conv_data["planning_state_json"] = _session_row.planning_state_json

                        if _empty_response_exhausted:
                            # The loop runner converts executor exceptions into a
                            # failed StepResult. Preserve that durable plan state,
                            # but do not turn an exhausted empty model response
                            # into a successful assistant summary/done frame.
                            if continuation_token:
                                _complete_continuation(memory, continuation_token)
                                conv_data["memory"] = memory
                            _persist_and_store(conversation_id, conv_data)
                            await websocket.send_json({
                                "type": "error",
                                "code": "empty_model_response",
                                "message": "模型未返回可用内容，请重试",
                                "recoverable": True,
                            })
                            continue

                        # If loop paused at waiting_user, push confirmation as a2ui_request
                        if loop_result.stop_reason.value == "waiting_user":
                            if not conv_data.get("pending_a2ui"):
                                for _ws in loop_result.plan.steps:
                                    if _ws.status.value == "waiting_user" and _ws.confirmation:
                                        a2ui_data = {
                                            "id": _ws.confirmation.request_id,
                                            "prompt": _ws.confirmation.prompt,
                                            "options": _ws.confirmation.options,
                                            "allow_free_text": _ws.confirmation.kind == "missing_info",
                                        }
                                        await websocket.send_json({
                                            "type": "a2ui_request",
                                            **a2ui_data,
                                        })
                                        conv_data["pending_a2ui"] = a2ui_data
                                        break

                        # Generate ONE summary assistant message
                        stop_label = {
                            "completed": "计划执行完成",
                            "waiting_user": "等待确认",
                            "budget_exhausted": "执行预算已用尽",
                            "step_failed": "步骤执行失败",
                        }.get(loop_result.stop_reason.value, "已暂停")
                        summary_msg = f"[Plan+Loop] {stop_label}"
                        if loop_result.error:
                            summary_msg += f" — {loop_result.error}"

                        # Append ONE assistant message
                        plan_loop_assistant = {"role": "assistant", "content": summary_msg}
                        if continuation_token:
                            plan_loop_assistant.update({
                                "continuation_token": continuation_token,
                                "continuation_run_id": continuation_run_id,
                            })
                        conv_data["messages"].append(plan_loop_assistant)
                        if continuation_token:
                            _complete_continuation(memory, continuation_token)
                            conv_data["memory"] = memory
                        _persist_and_store(conversation_id, conv_data)
                        if continuation_token:
                            await websocket.send_json({
                                "type": "continuation_completed",
                                "continuation_token": continuation_token,
                                "run_id": continuation_run_id,
                                "duplicate": False,
                            })
                        await websocket.send_json({"type": "token", "content": summary_msg})
                        await websocket.send_json({"type": "done"})
                    except _PlanLoopSkip:
                        # User message is not a creation intent — fall through
                        # to the normal single-call LLM path below.
                        pass
                    except Exception as exc:
                        if continuation_token:
                            _active_continuation_runs.discard(continuation_token)
                        import logging as _log
                        _log.getLogger(__name__).exception("Plan+Loop turn failed")
                        await websocket.send_json({
                            "type": "error",
                            "message": f"Plan+Loop 执行失败: {exc}",
                        })
                        continue
                    else:
                        continue
                # ── End Plan+Loop path ────────────────────────────────────────

                planner_agent = create_agent(
                    system_prompt=system_prompt,
                    model_name=model_cfg["model_name"],
                    provider=model_cfg["provider"],
                    stream=True,
                    temperature=0.7,
                    max_tokens=4096,
                    base_url=model_cfg.get("base_url"),
                    api_key=model_cfg.get("api_key"),
                )

                full_response = ""
                stripper = _MemoryTagStripper()
                a2ui_stripper = _A2UITagStripper()
                proposal_stripper = _ProposalJsonStripper()

                # Open a Langfuse trace for this planner turn and set it as the
                # active span so run_conversation's generation nests under it.
                # No-op when Langfuse is unconfigured.
                _lf = _get_langfuse()
                planner_span = None
                span_token = None
                if _lf:
                    try:
                        _trace_id = _lf.create_trace_id()
                        planner_span = _lf.start_observation(
                            trace_context={"trace_id": _trace_id},
                            name=f"planner-turn-{conversation_id}",
                            input={"user_input": user_content},
                        )
                        span_token = set_active_span(planner_span)
                    except Exception:
                        planner_span = None
                try:
                    # Execution path was decided up front (_use_agentic). The
                    # agentic turn streams its own token / thinking / tool run_events
                    # and returns the full_response, which then flows through the
                    # SAME done-branch extraction below (memory_update / a2ui /
                    # proposal) — unchanged.
                    _agentic_turn_state: dict = {}
                    async def _event_source():
                        nonlocal _agentic_turn_state
                        if _use_agentic:
                            agentic_attempt = _reserve_model_attempt("agentic")
                            try:
                                from app.core.planner_agent_loop import run_agentic_turn
                                result = await run_agentic_turn(
                                    system_prompt=system_prompt,
                                    model_cfg=model_cfg,
                                    user_content=user_content,
                                    conversation_id=conversation_id,
                                    planning_context=conv_data.get("planning_context"),
                                    websocket=websocket,
                                    run_emitter=run_emitter,
                                    memory=memory,
                                )
                                # Agentic already streamed tokens/thinking/tool
                                # events; hand the assembled text to the done-branch
                                # via a single synthetic done event.
                                _agentic_turn_state = result.get("turn_state") or {}
                                _agentic_text = str(result.get("full_response") or "")
                                if not _agentic_text.strip() and not any(
                                    _agentic_turn_state.get(key)
                                    for key in ("a2ui", "proposal")
                                ):
                                    raise EmptyModelResponse(
                                        model_name=str(model_cfg.get("model_name") or ""),
                                        response_mode="agentic",
                                        reason="empty_content",
                                    )
                                await run_emitter.emit(
                                    "model_response",
                                    "completed",
                                    message="模型响应完成",
                                    details={
                                        "attempt": agentic_attempt,
                                        "path": "agentic",
                                    },
                                )
                                yield ("token", _agentic_text)
                                yield ("done", "")
                                return
                            except EmptyModelResponse as exc:
                                retrying = await _emit_model_failure(
                                    exc,
                                    attempt=agentic_attempt,
                                    path="agentic",
                                )
                                _agent_log = __import__("logging").getLogger(__name__)
                                _agent_log.warning(
                                    "agentic turn returned empty (%s); falling back to single-call",
                                    exc,
                                )
                                if not retrying:
                                    raise
                            except Exception as exc:
                                attributed = EmptyModelResponse(
                                    model_name=str(model_cfg.get("model_name") or ""),
                                    response_mode="agentic",
                                    reason="agentic_error",
                                )
                                retrying = await _emit_model_failure(
                                    attributed,
                                    attempt=agentic_attempt,
                                    path="agentic",
                                )
                                _agent_log = __import__("logging").getLogger(__name__)
                                _agent_log.warning(
                                    "agentic turn failed (%s); falling back to single-call",
                                    exc,
                                )
                                if not retrying:
                                    raise
                        # Single-call fallback (existing behaviour).
                        fallback_path = "agentic_fallback" if _use_agentic else "single_call"
                        async for ev in _budgeted_run_conversation(
                            planner_agent,
                            user_content,
                            attachments=attachments,
                            path=fallback_path,
                        ):
                            yield ev

                    async for event_type, content in _event_source():
                        if event_type == "thinking_content":
                            await websocket.send_json({"type": "thinking_content", "content": content})
                        elif event_type == "token":
                            full_response += content
                            visible = stripper.feed(content)
                            if visible:
                                visible = a2ui_stripper.feed(visible)
                                if visible:
                                    visible = proposal_stripper.feed(visible)
                                    if visible:
                                        # First visible token: the model is past
                                        # the gathering/analysis phase and is now
                                        # producing the reply.
                                        if not drafting_started:
                                            drafting_started = True
                                            if _use_agentic:
                                                # Close the dynamic reasoning opener.
                                                await run_emitter.emit("reasoning", "completed", message="分析完成")
                                            await _activity(websocket, act_processing, "processing", "done")
                                            await _activity(websocket, act_drafting, "drafting", "start")
                                        # In agentic mode the per-token stream was
                                        # already sent live by the loop; avoid
                                        # double-emitting the assembled text.
                                        if not _use_agentic:
                                            await websocket.send_json({"type": "token", "content": visible})
                        elif event_type == "done":
                            tail = stripper.flush()
                            tail2 = a2ui_stripper.feed(tail) + a2ui_stripper.flush()
                            tail3 = proposal_stripper.feed(tail2)
                            tail3 += proposal_stripper.flush()
                            if tail3:
                                await websocket.send_json({"type": "token", "content": tail3})
                            # Extract memory_update before sending done
                            clean_text, memory_update = _extract_memory_update(full_response)
                            # Also extract a2ui_request and remove it from the visible text
                            a2ui_payload = _extract_a2ui_request(clean_text)
                            # Agentic fallback: if text extraction failed but the agentic
                            # turn_state carries a2ui data (tool was actually called),
                            # use it directly — bypasses the fragile regex chain.
                            if a2ui_payload is None and _agentic_turn_state.get("a2ui"):
                                a2ui_payload = _agentic_turn_state["a2ui"]
                            if a2ui_payload is not None:
                                clean_text = re.sub(
                                    r"<a2ui_request>[\s\S]*?</a2ui_request>",
                                    "",
                                    clean_text,
                                ).rstrip()
                            if memory_update:
                                _candidate_proposal = _try_extract_proposal(clean_text)
                                if (
                                    _candidate_proposal is None
                                    and _agentic_turn_state.get("proposal")
                                ):
                                    _candidate_proposal = _agentic_turn_state["proposal"]
                                _, _candidate_proposal_ok = _validate_proposal_payload(
                                    _candidate_proposal
                                )
                                _, _existing_proposal_ok = _validate_proposal_payload(
                                    conv_data.get("proposal")
                                )
                                # Tolerant per-field merge (design D4): partial /
                                # unknown fields never wipe accumulated state, and
                                # a malformed field is skipped, not fatal.
                                _merge_memory_update(
                                    memory,
                                    memory_update,
                                    recent_user_text=user_content,
                                    source_turn=turn_seq,
                                    has_validated_proposal=(
                                        _candidate_proposal_ok or _existing_proposal_ok
                                    ),
                                )
                                conv_data["memory"] = memory
                                await websocket.send_json({"type": "memory_update", "memory": memory})
                            # Plan-with-file: requirements.md follows memory updates,
                            # and also records this turn's attachments (design Q2).
                            if memory_update or attachments:
                                try:
                                    art = planner_files.write_requirements(
                                        conversation_id, memory, attachments=attachments
                                    )
                                    conv_data["file_artifacts"] = planner_files.merge_artifacts(
                                        conv_data.get("file_artifacts", []), art
                                    )
                                    await websocket.send_json({"type": "plan_file_updated", **art})
                                except Exception:
                                    pass
                            # Append assistant response to the message log. When
                            # this turn delivered a structured proposal, the raw
                            # JSON is NOT persisted — only the prose plus a file
                            # marker the frontend renders as a proposal.json chip.
                            structured = _try_extract_proposal(clean_text)
                            # Agentic fallback: if text extraction missed the proposal
                            # but the tool was actually called, use turn_state directly.
                            if structured is None and _agentic_turn_state.get("proposal"):
                                structured = _agentic_turn_state["proposal"]

                            # ── Agentic auto-retry: model narrated completion
                            # without calling emit_proposal tool ──────────
                            # gpt-5.4 and similar models sometimes reply with
                            # "已创建为草案" text but zero tool_calls. Detect
                            # this and inject a synthetic user message + re-run
                            # the agentic turn once so the model is explicitly
                            # told to use the tool.
                            _NARRATED_COMPLETION = (
                                "已创建", "已按方案", "已提交", "创建为草案",
                                "方案已保存", "创建成功", "已生成", "已落地",
                            )
                            if (
                                _use_agentic
                                and structured is None
                                and a2ui_payload is None
                                and any(p in clean_text for p in _NARRATED_COMPLETION)
                                and not conv_data.get("_agentic_retry_done")
                                and _model_attempt_count < _MODEL_ATTEMPT_LIMIT
                            ):
                                conv_data["_agentic_retry_done"] = True
                                # Persist the fake-completion as context, then
                                # inject a system nudge and re-run ONE more turn.
                                conv_data["messages"].append({"role": "assistant", "content": clean_text})
                                _retry_nudge = (
                                    '你上一步只用文字说了"已创建"但没有调用 emit_proposal 工具，'
                                    "方案实际未保存。请立即调用 emit_proposal(proposal) 提交。"
                                )
                                conv_data["messages"].append({"role": "user", "content": _retry_nudge})
                                await websocket.send_json({
                                    "type": "token",
                                    "content": "\n\n⏳ 正在重试提交方案...\n",
                                })
                                # Re-run agentic turn with the nudge
                                await run_emitter.emit(
                                    "model_response_retry",
                                    "running",
                                    message="正在重试提交结构化方案",
                                    details={
                                        "attempt": _model_attempt_count + 1,
                                        "cause": "missing_emit_proposal",
                                        "from_path": "agentic",
                                    },
                                )
                                retry_attempt = _reserve_model_attempt(
                                    "agentic_emit_proposal_retry"
                                )
                                try:
                                    from app.core.planner_agent_loop import run_agentic_turn
                                    retry_result = await run_agentic_turn(
                                        system_prompt=system_prompt,
                                        model_cfg=model_cfg,
                                        user_content=_retry_nudge,
                                        conversation_id=conversation_id,
                                        planning_context=conv_data.get("planning_context"),
                                        websocket=websocket,
                                        run_emitter=run_emitter,
                                        memory=memory,
                                    )
                                    retry_state = retry_result.get("turn_state") or {}
                                    retry_text = str(
                                        retry_result.get("full_response") or ""
                                    )
                                    retry_structured = _try_extract_proposal(retry_text)
                                    if (
                                        retry_structured is None
                                        and retry_state.get("proposal")
                                    ):
                                        retry_structured = retry_state["proposal"]
                                    if not retry_text.strip() and retry_structured is None:
                                        raise EmptyModelResponse(
                                            model_name=str(
                                                model_cfg.get("model_name") or ""
                                            ),
                                            response_mode="agentic",
                                            reason="empty_content",
                                        )
                                    if retry_structured is not None:
                                        structured = retry_structured
                                        clean_text = retry_text
                                        full_response = retry_text
                                        _agentic_turn_state = retry_state
                                        await run_emitter.emit(
                                            "model_response",
                                            "completed",
                                            message="模型响应完成",
                                            details={
                                                "attempt": retry_attempt,
                                                "path": "agentic_emit_proposal_retry",
                                            },
                                        )
                                        await run_emitter.emit(
                                            "model_response_retry",
                                            "completed",
                                            message="结构化方案重试成功",
                                            details={"attempt": retry_attempt},
                                        )
                                    else:
                                        await run_emitter.emit(
                                            "model_response_retry",
                                            "failed",
                                            message="重试后仍未提交结构化方案",
                                            details={
                                                "attempt": retry_attempt,
                                                "reason": "missing_emit_proposal",
                                            },
                                        )
                                except EmptyModelResponse as exc:
                                    retrying = await _emit_model_failure(
                                        exc,
                                        attempt=retry_attempt,
                                        path="agentic_emit_proposal_retry",
                                    )
                                    if not retrying:
                                        raise
                                except Exception:
                                    attributed = EmptyModelResponse(
                                        model_name=str(
                                            model_cfg.get("model_name") or ""
                                        ),
                                        response_mode="agentic",
                                        reason="agentic_error",
                                    )
                                    retrying = await _emit_model_failure(
                                        attributed,
                                        attempt=retry_attempt,
                                        path="agentic_emit_proposal_retry",
                                    )
                                    if not retrying:
                                        raise attributed

                            # Normalize shape so downstream consumers (final_summary,
                            # proposal.json, PlannerPanel) never see missing list
                            # fields. Agentic emit_proposal may submit a partial
                            # proposal (e.g. architecture_summary without a DAG
                            # projection); ProposalPayload allows extra/missing keys,
                            # so we backfill the list fields the UI/apply rely on.
                            if isinstance(structured, dict):
                                for _lk in ("nodes", "edges", "tuning_hints"):
                                    if not isinstance(structured.get(_lk), list):
                                        structured[_lk] = []
                                # DAG projection guard: if nodes is still empty but
                                # agent_spec is present, auto-derive a minimal DAG so
                                # the canvas is never blank after apply.
                                if not structured.get("nodes"):
                                    _derive_dag_from_agent_spec(structured)
                            # When a structured proposal is present, validate it
                            # against the Proposal Schema, derive capability
                            # candidates from this turn's planning_context, and
                            # mirror the validated payload into planner memory
                            # (persisted in planner_sessions — no new table).
                            # Validation failure degrades: keep the reply, log,
                            # don't abort the turn (spec "校验失败降级不阻断回复").
                            if structured is not None:
                                _payload, _ok = _validate_proposal_payload(structured)
                                if _ok:
                                    # Expert template retrieval (T3, design D4):
                                    # fill expert_candidates from this turn's
                                    # planning_context; back-fill the proposal's
                                    # primary_expert_template_id + a hit note in
                                    # context_used_explanation. Best-effort — a
                                    # retrieval hiccup must not abort the turn.
                                    expert_tags: list = []
                                    expert_modes: list = []
                                    await run_emitter.emit("expert_retrieval", "running",
                                                           message="正在检索专家模板")
                                    try:
                                        from app.core.expert_retrieval import retrieve_expert_candidates
                                        experts = retrieve_expert_candidates(
                                            goal_text=user_content,
                                            planning_context=conv_data.get("planning_context"),
                                        )
                                        if experts:
                                            structured["expert_candidates"] = experts
                                            top = experts[0]
                                            expert_tags = top.get("recommended_capability_tags") or []
                                            expert_modes = top.get("recommended_modes") or []
                                            if not structured.get("primary_expert_template_id"):
                                                structured["primary_expert_template_id"] = top.get("id")
                                            cue = structured.get("context_used_explanation")
                                            if not isinstance(cue, list):
                                                cue = []
                                            cue.append(f"命中专家模板：{top.get('name')}")
                                            structured["context_used_explanation"] = cue
                                        await run_emitter.emit(
                                            "expert_retrieval", "completed",
                                            message=f"命中 {len(experts)} 个专家模板" if experts else "无匹配专家模板",
                                            details={
                                                "expert_count": len(experts),
                                                "template_name": experts[0].get("name") if experts else None,
                                            },
                                        )
                                    except Exception as exc:
                                        await run_emitter.emit("expert_retrieval", "failed",
                                                               message=f"专家检索失败：{exc}")

                                    # Capability match + runtime mode (T4). Re-run
                                    # the upgraded matcher with the matched expert
                                    # template's tags + a DB session for tag lookup;
                                    # back-fill recommended_capabilities (only when
                                    # the planner didn't give them) and recommend a
                                    # runtime_mode (planner's explicit value wins).
                                    # Best-effort — never aborts the turn.
                                    await run_emitter.emit("capability_match", "running",
                                                           message="正在匹配可用能力")
                                    try:
                                        from sqlmodel import Session
                                        from app.core.database import engine
                                        with Session(engine) as _s:
                                            cands = _match_capability_candidates(
                                                conv_data.get("planning_context"), structured,
                                                expert_tags=expert_tags, session=_s,
                                            )
                                        if cands:
                                            structured["capability_candidates"] = cands
                                            # Back-fill recommended_capabilities only
                                            # when the planner didn't give them (D5):
                                            # take the positive-signal top-3.
                                            existing_rc = structured.get("recommended_capabilities")
                                            if not existing_rc:
                                                structured["recommended_capabilities"] = cands[:3]
                                        rec_mode = recommend_runtime_mode(
                                            goal_text=user_content,
                                            task_classification=str(memory.get("task_classification") or ""),
                                            expert_modes=expert_modes,
                                        )
                                        structured["recommended_runtime_mode"] = rec_mode
                                        # planner-first: only fill runtime_mode when
                                        # the proposal left it empty (D5).
                                        if not structured.get("runtime_mode"):
                                            structured["runtime_mode"] = rec_mode
                                        await run_emitter.emit(
                                            "capability_match", "completed",
                                            message=f"匹配到 {len(cands)} 个候选能力，推荐运行模式 {rec_mode}",
                                            details={
                                                "candidate_count": len(cands), "runtime_mode": rec_mode,
                                                "matched_count": len(cands),
                                                "top_names": [c.get("name", "") for c in cands[:5]],
                                            },
                                        )
                                    except Exception as exc:
                                        await run_emitter.emit("capability_match", "failed",
                                                               message=f"能力匹配失败：{exc}")

                                    # Mark recommended capabilities that are NOT in
                                    # the library as ``status: to_create`` (user
                                    # decision: 标注待创建并照常出方案). The model often
                                    # recommends tools/skills that don't exist yet
                                    # (e.g. prometheus_query, http_request). Rather
                                    # than silently implying they exist, flag them so
                                    # the UI/apply can show "待创建". Match by id when
                                    # present, else by name, against internal_context.
                                    try:
                                        _ctx = conv_data.get("planning_context") or {}
                                        _lib = ((_ctx.get("internal_context") or {}).get("capabilities")) or []
                                        _lib_ids = {str(c.get("id")) for c in _lib if isinstance(c, dict) and c.get("id") is not None}
                                        _lib_names = {str(c.get("name")) for c in _lib if isinstance(c, dict) and c.get("name")}
                                        _rc = structured.get("recommended_capabilities")
                                        _to_create = []
                                        if isinstance(_rc, list):
                                            for _cap in _rc:
                                                if not isinstance(_cap, dict):
                                                    continue
                                                _has_id = _cap.get("id") is not None and str(_cap.get("id")) in _lib_ids
                                                _has_name = str(_cap.get("name") or "") in _lib_names
                                                if _has_id or _has_name:
                                                    _cap["status"] = "available"
                                                else:
                                                    _cap["status"] = "to_create"
                                                    if _cap.get("name"):
                                                        _to_create.append(str(_cap["name"]))
                                        if _to_create:
                                            structured["capabilities_to_create"] = _to_create
                                            await run_emitter.emit(
                                                "capability_match", "completed",
                                                message=f"其中 {len(_to_create)} 项能力库中暂无，需新建：{('、'.join(_to_create[:5]))}",
                                                details={"to_create": _to_create},
                                            )
                                    except Exception:
                                        pass

                                    # Surface which memories fed the turn (T9):
                                    # alongside context_used_explanation, not a
                                    # duplicate store.
                                    _mem_used = conv_data.get("memory_used_explanation") or []
                                    if _mem_used:
                                        structured["memory_used_explanation"] = _mem_used

                                    _persist_proposal_to_memory(
                                        memory, _payload, conv_data.get("planning_context")
                                    )
                                    # Keep the validated, executable proposal
                                    # shape as the durable session mirror. The
                                    # ProposalPayload view is intentionally
                                    # smaller and model_dump() may omit DAG/UI
                                    # fields such as architecture_summary.
                                    memory["proposal_payload"] = structured
                                    proposal_readiness = structured.get("apply_readiness")
                                    if isinstance(proposal_readiness, dict):
                                        _merge_memory_update(
                                            memory,
                                            {"apply_readiness": proposal_readiness},
                                            recent_user_text=user_content,
                                            source_turn=turn_seq,
                                            has_validated_proposal=True,
                                        )
                                    conv_data["memory"] = memory

                                    # Persist as a first-class proposal (T5). The
                                    # proposals table is the authoritative source
                                    # of record; planner_sessions keeps only the
                                    # summary (above) for the session list. Best-
                                    # effort — a DB hiccup must not abort the turn.
                                    await run_emitter.emit("proposal_compose", "running",
                                                           message="正在组装并保存方案")
                                    try:
                                        import json as _json
                                        from app.core import proposal_store
                                        _prop = proposal_store.upsert_proposal(
                                            conversation_id=conversation_id,
                                            user_goal=user_content,
                                            inferred_goal=str(memory.get("requirement_summary") or ""),
                                            task_type=str(memory.get("task_classification") or ""),
                                            proposal_json=_json.dumps(structured, ensure_ascii=False),
                                            selected_runtime_mode=str(structured.get("runtime_mode") or ""),
                                            selected_expert_template_id=structured.get("primary_expert_template_id"),
                                        )
                                        # context_used_explanation lives in proposal_json
                                        # (T2/T3); surface it here without duplicating it.
                                        await run_emitter.emit(
                                            "proposal_compose", "completed", message="方案已生成并保存",
                                            proposal_id=_prop.id if _prop else None,
                                            details={"context_used_explanation":
                                                     structured.get("context_used_explanation") or []},
                                        )
                                    except Exception as exc:
                                        await run_emitter.emit("proposal_compose", "failed",
                                                               message=f"方案保存失败：{exc}")
                                else:
                                    import logging
                                    logging.getLogger(__name__).warning(
                                        "planner proposal failed schema validation; "
                                        "degrading (keeping reply, no structured mirror)"
                                    )
                                    # The raw proposal is not authoritative evidence.
                                    # Keep any surrounding prose, but do not let an
                                    # invalid payload create proposal artifacts or a
                                    # ready-to-apply stage.
                                    clean_text = _strip_proposal_text(clean_text)
                                    structured = None

                            # Compute which skills this turn ACTUALLY triggered (an
                            # observable backend action ran for them). This — not
                            # "which skills are enabled" — is what the user wants to
                            # see, and it is recorded on the message so the badge
                            # survives a refresh/restore (plain JSON key, no DB
                            # column). A skill is credited only when its candidate
                            # is in effective_skills this turn (design D5).
                            if structured is not None:
                                _ps, _ = resolve_action_skill("proposal", turn_effective)
                                if _ps and _ps not in turn_triggered:
                                    turn_triggered.append(_ps)
                            if a2ui_payload is not None:
                                _as, _ = resolve_action_skill("a2ui", turn_effective)
                                if _as and _as not in turn_triggered:
                                    turn_triggered.append(_as)

                            if structured is not None:
                                # Capture the parsed proposal on the planner trace
                                # span output (spec: proposal JSON captured on trace).
                                if planner_span is not None:
                                    try:
                                        planner_span.update(output={"proposal": structured})
                                    except Exception:
                                        pass
                                prose = _strip_proposal_text(clean_text)
                                persisted = (prose + "\n\n" + _PROPOSAL_FILE_MARKER).strip() if prose else _PROPOSAL_FILE_MARKER
                                _asst = {"role": "assistant", "content": persisted}
                                if continuation_token:
                                    _asst.update({
                                        "continuation_token": continuation_token,
                                        "continuation_run_id": continuation_run_id,
                                    })
                                if turn_triggered:
                                    _asst["triggered_skills"] = list(turn_triggered)
                                conv_data["messages"].append(_asst)
                            elif clean_text.strip():
                                # No proposal — the JSON stripper may have held a
                                # legitimate (non-proposal) code block; emit it now
                                # so it is not silently lost.
                                held = proposal_stripper.held
                                if held:
                                    await websocket.send_json({"type": "token", "content": held})
                                _asst = {"role": "assistant", "content": clean_text}
                                if continuation_token:
                                    _asst.update({
                                        "continuation_token": continuation_token,
                                        "continuation_run_id": continuation_run_id,
                                    })
                                if turn_triggered:
                                    _asst["triggered_skills"] = list(turn_triggered)
                                conv_data["messages"].append(_asst)
                            elif a2ui_payload is None and not memory_update:
                                # Last-line invariant: a successful turn must carry
                                # visible text or a structured control result.
                                # Normally the adapter catches this before ``done``;
                                # retain the guard for agentic/legacy producers.
                                raise EmptyModelResponse(
                                    model_name=str(model_cfg.get("model_name") or ""),
                                    response_mode="planner_done",
                                    reason="empty_assistant",
                                )

                            # When a proposal is found, write proposal.json + final.md
                            # and send a final_summary event so the frontend renders a
                            # short summary + file chip instead of the long JSON.
                            short_summary: Optional[str] = None
                            files_for_final: List[dict] = []
                            if structured is not None:
                                act_proposal = f"t{turn_seq}-proposal"
                                # Attribute the proposal action to its conventional
                                # skill, but ONLY when that skill is actually in
                                # this turn's effective_skills (design D5). Otherwise
                                # the action is surfaced without a skill label.
                                _p_skill, _p_action = resolve_action_skill(
                                    "proposal", turn_effective
                                )
                                await _activity(
                                    websocket, act_proposal, "composing_proposal",
                                    "start", skill=_p_skill, skill_action=_p_action,
                                )
                                try:
                                    art = planner_files.write_proposal_json(conversation_id, structured)
                                    conv_data["file_artifacts"] = planner_files.merge_artifacts(
                                        conv_data.get("file_artifacts", []), art
                                    )
                                    await websocket.send_json({"type": "plan_file_updated", **art})
                                    files_for_final.append(art)
                                except Exception:
                                    pass
                                arch_summary = str(structured.get("architecture_summary") or "")
                                arch_rationale = str(structured.get("rationale") or "")
                                if arch_summary or arch_rationale:
                                    try:
                                        art2 = planner_files.write_architecture(
                                            conversation_id, arch_summary, arch_rationale
                                        )
                                        conv_data["file_artifacts"] = planner_files.merge_artifacts(
                                            conv_data.get("file_artifacts", []), art2
                                        )
                                        await websocket.send_json({"type": "plan_file_updated", **art2})
                                        files_for_final.append(art2)
                                    except Exception:
                                        pass
                                # Build a short summary (≤ 800 chars) for the chat surface.
                                short_summary = _build_short_summary(structured, memory)
                                try:
                                    art3 = planner_files.write_final_summary(
                                        conversation_id,
                                        short_summary,
                                        files=conv_data.get("file_artifacts", []),
                                    )
                                    conv_data["file_artifacts"] = planner_files.merge_artifacts(
                                        conv_data.get("file_artifacts", []), art3
                                    )
                                    await websocket.send_json({"type": "plan_file_updated", **art3})
                                    files_for_final.append(art3)
                                except Exception:
                                    pass
                                # Carry the structured proposal in the event so
                                # the frontend can drive PlannerPanel + apply
                                # without parsing it from the (now JSON-free) chat.
                                conv_data["proposal"] = structured
                                # Derive richer planning meta for the event
                                # (apply_readiness / architecture_pattern / kind).
                                meta = _proposal_meta(structured, memory)
                                await websocket.send_json({
                                    "type": "final_summary",
                                    "text": short_summary,
                                    "files": conv_data.get("file_artifacts", []),
                                    "proposal": structured,
                                    "apply_readiness": meta["apply_readiness"],
                                    "architecture_pattern": meta["architecture_pattern"],
                                    "kind": meta["kind"],
                                    "triggered_skills": list(turn_triggered),
                                })
                                await _activity(
                                    websocket, act_proposal, "composing_proposal",
                                    "done", skill=_p_skill, skill_action=_p_action,
                                )

                            # If the LLM emitted an a2ui_request tag, surface it as a
                            # structured event for the frontend confirmation card.
                            if a2ui_payload is not None:
                                conv_data["pending_a2ui"] = _normalize_pending_a2ui(a2ui_payload)
                                import logging as _ws_log
                                _ws_log.getLogger("planner.ws").info(
                                    ">>> Sending a2ui_request to frontend: id=%s options=%d",
                                    a2ui_payload.get("id", "?"),
                                    len(a2ui_payload.get("options", [])),
                                )
                                await websocket.send_json({
                                    "type": "a2ui_request",
                                    **a2ui_payload,
                                })
                                act_confirm = f"t{turn_seq}-confirm"
                                # Confirmation is a discrete waypoint: emit start+done
                                # so the timeline shows it as a completed step (the
                                # actual wait is driven by the separate a2ui card).
                                # a2ui is a system_builtin default, so it is always in
                                # effective_skills — attribution is unconditional here.
                                _a_skill, _a_action = resolve_action_skill(
                                    "a2ui", turn_effective
                                )
                                await _activity(
                                    websocket, act_confirm, "awaiting_confirmation",
                                    "start", skill=_a_skill, skill_action=_a_action,
                                )
                                await _activity(
                                    websocket, act_confirm, "awaiting_confirmation",
                                    "done", skill=_a_skill, skill_action=_a_action,
                                )

                            # Persist this turn so the session survives restart and shows up in /sessions list
                            if continuation_token:
                                _complete_continuation(memory, continuation_token)
                                conv_data["memory"] = memory
                            try:
                                _persist_and_store(conversation_id, conv_data)
                            except Exception:
                                pass
                            if continuation_token:
                                await websocket.send_json({
                                    "type": "continuation_completed",
                                    "continuation_token": continuation_token,
                                    "run_id": continuation_run_id,
                                    "duplicate": False,
                                })
                            # Finalize any phase still open this turn before `done`
                            # so the timeline never leaves a step "in progress":
                            # drafting if tokens were produced, else processing.
                            if _use_agentic and not drafting_started:
                                # A tool-only / no-visible-text agentic turn: close
                                # the dynamic reasoning opener so it doesn't hang.
                                await run_emitter.emit("reasoning", "completed", message="分析完成")
                            if drafting_started:
                                await _activity(websocket, act_drafting, "drafting", "done")
                            else:
                                await _activity(websocket, act_processing, "processing", "done")
                            await websocket.send_json({
                                "type": "done",
                                "triggered_skills": list(turn_triggered),
                            })
                            # Safety net: re-send pending a2ui_request AFTER done so
                            # the frontend definitely renders the card even if the
                            # earlier send was lost in a WebSocket race. The frontend
                            # reducer is idempotent on duplicate a2ui_request events.
                            _pa = conv_data.get("pending_a2ui")
                            if isinstance(_pa, dict) and _pa.get("options"):
                                await websocket.send_json({
                                    "type": "a2ui_request",
                                    **_pa,
                                })
                except Exception as e:
                    import logging as _lg, traceback as _tb
                    _lg.getLogger(__name__).error(
                        "planner turn exception:\n%s", _tb.format_exc(),
                    )
                    is_empty_response = isinstance(e, EmptyModelResponse)
                    if is_empty_response:
                        # Retain the user turn, keep the artifact-derived stage,
                        # and close the coarse activity instead of leaving the UI
                        # spinner open.  Never append/persist an assistant here.
                        try:
                            if continuation_token:
                                _complete_continuation(memory, continuation_token)
                                conv_data["memory"] = memory
                            _persist_and_store(conversation_id, conv_data)
                        except Exception:
                            pass
                        try:
                            if drafting_started:
                                await _activity(websocket, act_drafting, "drafting", "done")
                            else:
                                await _activity(websocket, act_processing, "processing", "done")
                        except Exception:
                            pass
                    try:
                        payload = {"type": "error", "message": str(e)}
                        if is_empty_response:
                            payload.update({
                                "code": "empty_model_response",
                                "recoverable": True,
                            })
                        await websocket.send_json(payload)
                    except Exception:
                        pass
                finally:
                    if continuation_token:
                        _active_continuation_runs.discard(continuation_token)
                    if span_token is not None:
                        reset_active_span(span_token)
                    if planner_span is not None and _lf:
                        try:
                            planner_span.end()
                            _lf.flush()
                        except Exception:
                            pass

            elif msg.get("type") == "a2ui_response":
                # Persist the human decision to decisions.md and inject as next-turn context.
                req_id = str(msg.get("id") or "").strip()
                choice_id = str(msg.get("choice") or "").strip()
                free_text = msg.get("free_text")
                pending = conv_data.get("pending_a2ui") or {}
                prompt_text = str(pending.get("prompt") or "")
                # Resolve the human-readable label of the chosen option.
                choice_label = choice_id
                for opt in pending.get("options") or []:
                    if isinstance(opt, dict) and str(opt.get("id")) == choice_id:
                        choice_label = str(opt.get("label") or choice_id)
                        break
                continuation_token = uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"atlas-planner:{conversation_id}:{req_id}:{choice_id}",
                ).hex

                if not req_id or not choice_id:
                    await websocket.send_json({
                        "type": "error",
                        "code": "invalid_a2ui_response",
                        "message": "确认请求缺少 id 或 choice",
                        "recoverable": True,
                    })
                    continue

                # Durable decision ledger is also the idempotency record.  A
                # duplicate of the exact frame is acknowledged without touching
                # files, memory, plan state, or consuming another model turn.
                existing_decision = next(
                    (
                        item for item in (memory.get("decisions_confirmed") or [])
                        if isinstance(item, dict) and str(item.get("id") or "") == req_id
                    ),
                    None,
                )
                if existing_decision is not None:
                    selected = existing_decision.get("selected_option") or {}
                    existing_choice = str(
                        selected.get("id") if isinstance(selected, dict) else ""
                    )
                    if existing_choice == choice_id:
                        if isinstance(selected, dict):
                            choice_label = str(selected.get("label") or choice_label)
                        _record_pending_continuation(
                            memory,
                            request_id=req_id,
                            choice_id=choice_id,
                            choice_label=choice_label,
                            continuation_token=continuation_token,
                            free_text=(
                                str(existing_decision.get("free_text") or "")
                                if isinstance(existing_decision, dict)
                                else ""
                            ),
                        )
                        conv_data.pop("pending_a2ui", None)
                        pending_items = memory.get("decisions_pending") or []
                        if isinstance(pending_items, list):
                            memory["decisions_pending"] = [
                                item for item in pending_items
                                if not (
                                    isinstance(item, dict)
                                    and str(item.get("id") or "") == req_id
                                )
                            ]
                        conv_data["memory"] = memory
                        try:
                            _persist_and_store(conversation_id, conv_data)
                        except Exception:
                            pass
                        await websocket.send_json({
                            "type": "a2ui_recorded",
                            "id": req_id,
                            "choice": choice_id,
                            "continuation_token": continuation_token,
                            "duplicate": True,
                        })
                    else:
                        await websocket.send_json({
                            "type": "error",
                            "code": "a2ui_decision_conflict",
                            "message": "该确认请求已记录不同选择，不能重复改写",
                            "recoverable": False,
                        })
                    continue

                # ── Plan+Loop: precise A2UI resolution ──
                # If a Plan+Loop session is waiting_user, resolve via the
                # structured protocol (request_id + choice validation).
                _plan_loop_resolved = False
                _plan_loop_detected = False
                try:
                    from app.core.planner_plan import (
                        load_plan, save_plan, PlanStatus, determine_session_mode,
                    )
                    from app.core.planner_step_handlers import (
                        resolve_a2ui_response, A2UIResolutionError,
                    )
                    from app.core.planner_session_repo import (
                        get_or_create_planner_session, commit_session_row,
                    )
                    _pl_mode = determine_session_mode(
                        type("_S", (), {"planning_state_json": conv_data.get("planning_state_json", "{}")})()
                    )
                    if _pl_mode == "plan_loop":
                        _plan_loop_detected = True
                        # Use real DB session for persistence
                        _pl_row = await get_or_create_planner_session(
                            conversation_id,
                            request_scope,
                        )
                        _pl_plan = load_plan(_pl_row)
                        # Backward-compatible duplicate detection for Plan+Loop
                        # rows created before decisions_confirmed became the
                        # shared idempotency ledger.
                        _resolved_from_plan = None
                        if _pl_plan is not None:
                            for _pl_step in _pl_plan.steps:
                                _confirmation = getattr(_pl_step, "confirmation", None)
                                if (
                                    _confirmation is not None
                                    and str(getattr(_confirmation, "request_id", "") or "") == req_id
                                    and getattr(_confirmation, "selected", None)
                                ):
                                    _resolved_from_plan = str(_confirmation.selected)
                                    break
                        if _resolved_from_plan is not None:
                            if _resolved_from_plan == choice_id:
                                _record_pending_continuation(
                                    memory,
                                    request_id=req_id,
                                    choice_id=choice_id,
                                    choice_label=choice_label,
                                    continuation_token=continuation_token,
                                    free_text=(
                                        free_text
                                        if isinstance(free_text, str)
                                        else ""
                                    ),
                                )
                                conv_data["memory"] = memory
                                _persist_and_store(conversation_id, conv_data)
                                await websocket.send_json({
                                    "type": "a2ui_recorded",
                                    "id": req_id,
                                    "choice": choice_id,
                                    "continuation_token": continuation_token,
                                    "duplicate": True,
                                })
                            else:
                                await websocket.send_json({
                                    "type": "error",
                                    "code": "a2ui_decision_conflict",
                                    "message": "该确认请求已记录不同选择，不能重复改写",
                                    "recoverable": False,
                                })
                            _plan_loop_resolved = True
                        if (
                            not _plan_loop_resolved
                            and _pl_plan
                            and _pl_plan.status == PlanStatus.waiting_user
                        ):
                            try:
                                _pl_plan, _resolved_choice = resolve_a2ui_response(
                                    _pl_plan, msg
                                )
                                # Persist resolved plan to REAL DB row
                                save_plan(_pl_row, _pl_plan)
                                commit_session_row(_pl_row)
                                # Also sync conv_data for in-memory consistency
                                conv_data["planning_state_json"] = _pl_row.planning_state_json
                                conv_data.pop("pending_a2ui", None)
                                _decision_pending = pending or {
                                    "id": req_id,
                                    "prompt": prompt_text,
                                    "options": [],
                                }
                                _decision_pending["id"] = req_id
                                _plan_decision = _build_decision_entry(
                                    _decision_pending,
                                    choice_id=choice_id,
                                    choice_label=choice_label,
                                    free_text=free_text if isinstance(free_text, str) else "",
                                )
                                _plan_ledger = list(memory.get("decisions_confirmed") or [])
                                _plan_ledger.append(_plan_decision)
                                memory["decisions_confirmed"] = _plan_ledger[-30:]
                                _record_pending_continuation(
                                    memory,
                                    request_id=req_id,
                                    choice_id=choice_id,
                                    choice_label=choice_label,
                                    continuation_token=continuation_token,
                                    free_text=(
                                        free_text
                                        if isinstance(free_text, str)
                                        else ""
                                    ),
                                )
                                conv_data["memory"] = memory
                                _persist_and_store(conversation_id, conv_data)
                                # Acknowledge
                                await websocket.send_json({
                                    "type": "a2ui_recorded",
                                    "id": req_id,
                                    "choice": choice_id,
                                    "continuation_token": continuation_token,
                                    "duplicate": False,
                                })
                                _plan_loop_resolved = True
                            except A2UIResolutionError as e:
                                # Validation failed — emit error event
                                await websocket.send_json({
                                    "type": "run_event",
                                    "step": "confirm_proposal",
                                    "event_kind": "a2ui_error",
                                    "status": "failed",
                                    "message": str(e),
                                })
                                _plan_loop_resolved = True  # Don't fall through to legacy
                        if not _plan_loop_resolved:
                            await websocket.send_json({
                                "type": "run_event",
                                "step": "confirm_proposal",
                                "event_kind": "a2ui_error",
                                "status": "failed",
                                "message": "当前 Plan+Loop 没有可处理的确认步骤",
                            })
                            _plan_loop_resolved = True
                except Exception as exc:
                    if _plan_loop_detected:
                        await websocket.send_json({
                            "type": "run_event",
                            "step": "confirm_proposal",
                            "event_kind": "a2ui_error",
                            "status": "failed",
                            "message": f"Plan+Loop 确认处理失败：{exc}",
                        })
                        _plan_loop_resolved = True

                if _plan_loop_resolved:
                    continue  # Skip legacy a2ui handling

                try:
                    art = planner_files.append_decision(
                        conversation_id,
                        prompt=prompt_text or "(unknown prompt)",
                        choice=choice_label,
                        free_text=free_text if isinstance(free_text, str) else None,
                        request_id=req_id or None,
                    )
                    conv_data["file_artifacts"] = planner_files.merge_artifacts(
                        conv_data.get("file_artifacts", []), art
                    )
                    await websocket.send_json({"type": "plan_file_updated", **art})
                except Exception:
                    pass

                # Append a structured decision to the ledger (decisions_confirmed)
                # — the choice lives here authoritatively (design D6). Only the
                # free-text correction is mirrored into user_feedback, so the same
                # fact is never written to both fields without precedence.
                decision = _build_decision_entry(
                    pending,
                    choice_id=choice_id,
                    choice_label=choice_label,
                    free_text=free_text if isinstance(free_text, str) else "",
                )
                ledger = list(memory.get("decisions_confirmed") or [])
                # De-dup by decision id: a re-answer of the same card replaces the
                # prior entry rather than stacking duplicates.
                ledger = [d for d in ledger if not (isinstance(d, dict) and d.get("id") == decision["id"])]
                ledger.append(decision)
                memory["decisions_confirmed"] = ledger[-30:]

                # Drop the topic from decisions_pending now that it's confirmed.
                pending_list = memory.get("decisions_pending") or []
                if isinstance(pending_list, list) and req_id:
                    memory["decisions_pending"] = [
                        d for d in pending_list
                        if not (isinstance(d, dict) and str(d.get("id")) == req_id)
                    ]

                # Mirror ONLY the free-text correction into user_feedback (the
                # choice itself stays in the ledger, not duplicated here).
                if isinstance(free_text, str) and free_text.strip():
                    feedback = list(memory.get("user_feedback") or [])
                    feedback.append(free_text.strip())
                    memory["user_feedback"] = feedback[-10:]
                _record_pending_continuation(
                    memory,
                    request_id=req_id,
                    choice_id=choice_id,
                    choice_label=choice_label,
                    continuation_token=continuation_token,
                    free_text=free_text if isinstance(free_text, str) else "",
                )
                conv_data["memory"] = memory
                conv_data.pop("pending_a2ui", None)
                try:
                    _persist_and_store(conversation_id, conv_data)
                except Exception:
                    pass
                # Echo the updated memory so the frontend ledger reflects the new
                # decision without waiting for the next turn.
                await websocket.send_json({"type": "memory_update", "memory": memory})

                # Persist the decision and acknowledge it immediately. The
                # planner continuation itself is user-driven: the frontend can
                # now choose to send a follow-up message, but the backend should
                # not secretly consume extra model turns on card submit.
                await websocket.send_json({
                    "type": "a2ui_recorded",
                    "id": req_id,
                    "choice": choice_id,
                    "continuation_token": continuation_token,
                    "duplicate": False,
                })
            elif msg.get("type") in ("skill_attached", "skill_detached"):
                # Mount / unmount a skill onto this session. Updates
                # memory.selected_skills (dedup / remove) so the next turn's
                # system prompt picks it up via _build_system_prompt_with_memory.
                skill_name = str(msg.get("skill_name") or "").strip()
                selected = list(memory.get("selected_skills") or [])
                if _is_default_skill(skill_name):
                    # Default skills (system_builtin / workspace_default) are
                    # always active via the resolver; never write them into
                    # selected_skills, and attach/detach is a no-op. Ack with the
                    # current set unchanged so the client stays consistent.
                    await websocket.send_json({
                        "type": "skill_attached_ok" if msg["type"] == "skill_attached" else "skill_detached_ok",
                        "skill_name": skill_name,
                        "selected_skills": selected,
                    })
                else:
                    if msg["type"] == "skill_attached":
                        if skill_name and skill_name not in selected:
                            selected.append(skill_name)
                        ack_type = "skill_attached_ok"
                    else:
                        selected = [s for s in selected if s != skill_name]
                        ack_type = "skill_detached_ok"
                    memory["selected_skills"] = selected
                    conv_data["memory"] = memory
                    try:
                        _persist_and_store(conversation_id, conv_data)
                    except Exception:
                        pass
                    await websocket.send_json({
                        "type": ack_type,
                        "skill_name": skill_name,
                        "selected_skills": selected,
                    })
            elif msg.get("type") == "set_model":
                # Switch the model for THIS session only. The change applies to
                # the next turn (the message loop re-reads conv_data["model"]).
                # Credentials resolved here stay backend-only — only the public
                # model_name/provider are echoed back.
                model_id = str(msg.get("model_id") or "").strip()
                if model_id:
                    resolved = _resolve_model(model_id)
                    conv_data["model"] = resolved
                    try:
                        _persist_and_store(conversation_id, conv_data)
                    except Exception:
                        pass
                    # Visible attribution: if the resolved provider has no usable
                    # credentials (no capability-carried endpoint AND empty env
                    # creds), tell the frontend which provider is missing what —
                    # so a switch to such a model surfaces a readable reason
                    # instead of a bare 401 on the next turn. Never leaks key values.
                    from app.core.config import credential_gap
                    endpoint = {k: resolved[k] for k in ("base_url", "api_key") if k in resolved}
                    gap = credential_gap(resolved["provider"], endpoint)
                    payload = {
                        "type": "model_resolved",
                        "model": resolved["model_name"],
                        "provider": resolved["provider"],
                    }
                    if gap is not None:
                        payload["credential_warning"] = gap
                    await websocket.send_json(payload)
            elif msg.get("type") == "ping":
                await websocket.send_json({"type": "pong"})

    except WebSocketDisconnect:
        pass
    except Exception as _outer_exc:
        import logging as _lg, traceback as _tb
        _lg.getLogger(__name__).error(
            "planner_websocket unhandled exception:\n%s",
            _tb.format_exc(),
        )
    finally:
        token = locals().get("continuation_token")
        if isinstance(token, str) and token:
            _active_continuation_runs.discard(token)
        # Only clear if we're still the registered socket — a concurrent DELETE
        # may have already popped (and closed) us.
        if state._active_websockets.get(conversation_id) is websocket:
            state._active_websockets.pop(conversation_id, None)
