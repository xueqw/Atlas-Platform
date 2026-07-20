"""Planner Step Handlers — business logic for Plan+Loop steps.

Extracted from ws.py so handlers are independently testable (no WebSocket dep).
Each handler is an async callable(step, deps) -> dict | StepResult.

Also provides:
- PlannerLoopContext: dataclass encapsulating loop dependencies
- resolve_a2ui_response(): precise A2UI confirmation resolution
- resolve_missing_info_text(): text supplement for missing_info confirmations
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional, Protocol

from .planner_loop import StepResult
from .planner_plan import (
    ArtifactRef,
    ConfirmationRequest,
    Plan,
    PlanStatus,
    PlanStep,
    StepStatus,
    StopReason,
)

logger = logging.getLogger(__name__)


# ─── PlannerLoopContext ───────────────────────────────────────────────────────

@dataclass
class PlannerLoopContext:
    """Encapsulates all dependencies for a Plan+Loop execution turn."""

    conversation_id: str
    session_row: Any  # Real PlannerSession ORM row
    conv_data: dict
    user_message: str
    websocket: Any  # WebSocket or None (for testing)
    run_id: str
    model_cfg: dict = field(default_factory=dict)
    memory: dict = field(default_factory=dict)


# ─── StepHandler Protocol ─────────────────────────────────────────────────────

class StepHandler(Protocol):
    """Protocol for Plan+Loop step handlers."""

    async def __call__(self, step: PlanStep, ctx: Any) -> dict | StepResult:
        ...


# ─── PlannerStepDeps ──────────────────────────────────────────────────────────

@dataclass
class PlannerStepDeps:
    """Dependencies injected into step handlers from ws.py.

    This replaces closure-captured variables — handlers receive
    an explicit deps object rather than closing over ws.py locals.
    """

    conversation_id: str
    run_id: str
    user_content: str
    conv_data: dict
    memory: dict
    model_cfg: dict
    websocket: Any  # For streaming events only (not chat tokens)
    # Service callables — injected by ws.py at build time
    build_planning_context: Optional[Callable] = None
    render_context_block: Optional[Callable] = None
    build_system_prompt_with_memory: Optional[Callable] = None
    match_capability_candidates: Optional[Callable] = None
    try_extract_proposal: Optional[Callable] = None
    extract_a2ui_request: Optional[Callable] = None
    extract_memory_update: Optional[Callable] = None
    merge_memory_update: Optional[Callable] = None
    normalize_pending_a2ui: Optional[Callable] = None
    validate_proposal_payload: Optional[Callable] = None
    write_proposal_json: Optional[Callable] = None
    merge_artifacts: Optional[Callable] = None
    # LLM agent creation
    create_agent: Optional[Callable] = None
    run_conversation: Optional[Callable] = None
    # Tag strippers factory
    create_strippers: Optional[Callable] = None
    # Event emitter for streaming run_events
    emitter: Any = None


def _apply_memory_candidate(
    deps: PlannerStepDeps,
    step: PlanStep,
    memory_update: Optional[dict],
    *,
    has_validated_proposal: bool,
) -> None:
    """Apply one Plan+Loop memory candidate with turn/proposal evidence.

    The fallback preserves compatibility with older injected test doubles and
    external adapters that still implement the historical two-argument merge
    callable. The platform merge policy accepts the keyword evidence.
    """
    if not memory_update or not deps.merge_memory_update:
        return
    user_messages = [
        message
        for message in deps.conv_data.get("messages", [])
        if isinstance(message, dict) and message.get("role") == "user"
    ]
    latest_user = user_messages[-1] if user_messages else {}
    source_turn = {
        "run_id": deps.run_id,
        "step_id": step.id,
        "message_index": max(0, len(deps.conv_data.get("messages", [])) - 1),
    }
    if latest_user.get("id") not in (None, ""):
        source_turn["message_id"] = latest_user["id"]
    try:
        deps.merge_memory_update(
            deps.memory,
            memory_update,
            recent_user_text=deps.user_content,
            source_turn=source_turn,
            has_validated_proposal=has_validated_proposal,
        )
    except TypeError:
        deps.merge_memory_update(deps.memory, memory_update)
    deps.conv_data["memory"] = deps.memory


# ─── A2UI Confirmation Resolution ─────────────────────────────────────────────

class A2UIResolutionError(Exception):
    """Raised when a2ui_response cannot be resolved."""

    pass


def resolve_a2ui_response(plan: Plan, msg: dict) -> tuple[Plan, str]:
    """Resolve a waiting_user step from a structured a2ui_response message.

    Validates:
    - msg.id matches the waiting step's confirmation.request_id
    - msg.choice is in confirmation.options

    Returns (updated_plan, choice) on success.
    Raises A2UIResolutionError on validation failure.
    """
    req_id = str(msg.get("id") or "").strip()
    choice = str(msg.get("choice") or "").strip()
    free_text = msg.get("free_text")

    if not req_id:
        raise A2UIResolutionError("a2ui_response missing 'id' field")
    if not choice:
        raise A2UIResolutionError("a2ui_response missing 'choice' field")

    # Find the waiting step
    waiting_step: Optional[PlanStep] = None
    for step in plan.steps:
        if step.status == StepStatus.waiting_user and step.confirmation:
            waiting_step = step
            break

    if waiting_step is None:
        raise A2UIResolutionError("No step in waiting_user status with confirmation")

    confirmation = waiting_step.confirmation

    # Validate request_id match
    if confirmation.request_id != req_id:
        raise A2UIResolutionError(
            f"request_id mismatch: expected '{confirmation.request_id}', got '{req_id}'"
        )

    # Validate choice is a valid option
    valid_choices = {opt["id"] for opt in confirmation.options if isinstance(opt, dict)}
    if choice not in valid_choices:
        raise A2UIResolutionError(
            f"Invalid choice '{choice}'. Valid: {valid_choices}"
        )

    # Apply choice semantics
    now = datetime.now(timezone.utc)

    if choice == "confirm":
        # Mark step done, plan continues to next step
        waiting_step.confirmation.selected = choice
        waiting_step.confirmation.resolved_at = now
        if free_text:
            waiting_step.confirmation.payload["free_text"] = free_text
        waiting_step.status = StepStatus.done
        waiting_step.completed_at = now
        waiting_step.outputs["confirmation_selected"] = choice
        plan.status = PlanStatus.running
        plan.updated_at = now

    elif choice == "revise":
        # Reset design_architecture + confirm_proposal to pending
        waiting_step.confirmation.selected = choice
        waiting_step.confirmation.resolved_at = now
        if free_text:
            waiting_step.confirmation.payload["free_text"] = free_text
        # Reset the waiting step itself
        waiting_step.status = StepStatus.pending
        waiting_step.confirmation = ConfirmationRequest(
            kind="proposal_confirm",
            prompt=confirmation.prompt,
            options=confirmation.options,
        )
        # Reset design_architecture
        for s in plan.steps:
            if s.id == "design_architecture" and s.status == StepStatus.done:
                s.status = StepStatus.pending
                s.attempt = 0
                s.outputs = {}
                s.completed_at = None
                # Attach user feedback for revision
                s.inputs["user_feedback"] = free_text or "(用户要求修改)"
                break
        plan.status = PlanStatus.running
        plan.revision += 1
        plan.updated_at = now
        plan.current_step_id = "design_architecture"

    elif choice == "reject":
        # Plan failed — user rejected
        waiting_step.confirmation.selected = choice
        waiting_step.confirmation.resolved_at = now
        waiting_step.status = StepStatus.failed
        waiting_step.completed_at = now
        waiting_step.error = "用户拒绝方案"
        plan.status = PlanStatus.failed
        plan.updated_at = now

    else:
        # Generic confirm for other kinds (missing_info answered via button)
        waiting_step.confirmation.selected = choice
        waiting_step.confirmation.resolved_at = now
        if free_text:
            waiting_step.confirmation.payload["free_text"] = free_text
        waiting_step.status = StepStatus.done
        waiting_step.completed_at = now
        waiting_step.outputs["confirmation_selected"] = choice
        plan.status = PlanStatus.running
        plan.updated_at = now

    # Advance current_step_id
    from .planner_plan import next_runnable_step

    next_step, _ = next_runnable_step(plan)
    plan.current_step_id = next_step.id if next_step else None

    return plan, choice


def resolve_missing_info_text(plan: Plan, text: str) -> Optional[Plan]:
    """Resolve a missing_info waiting step with plain text supplement.

    Only resolves if the waiting step's confirmation.kind == "missing_info".
    Returns updated plan or None if not applicable.
    """
    for step in plan.steps:
        if step.status != StepStatus.waiting_user:
            continue
        if not step.confirmation:
            continue
        if step.confirmation.kind != "missing_info":
            return None  # Not a missing_info wait — plain text cannot resolve

        # Store the text and resolve
        now = datetime.now(timezone.utc)
        step.inputs["user_supplement"] = text
        step.confirmation.selected = "text_supplement"
        step.confirmation.resolved_at = now
        step.confirmation.payload["user_text"] = text
        step.status = StepStatus.pending  # Re-run with supplement
        step.attempt = 0
        step.outputs = {}
        step.completed_at = None

        plan.status = PlanStatus.running
        plan.revision += 1
        plan.updated_at = now
        plan.current_step_id = step.id  # Re-run this step

        return plan

    return None  # No waiting step found


def is_plan_waiting_user(plan: Plan) -> bool:
    """Check if the plan is currently waiting for user input."""
    return plan.status == PlanStatus.waiting_user


def get_waiting_step(plan: Plan) -> Optional[PlanStep]:
    """Get the step currently waiting for user input, if any."""
    for step in plan.steps:
        if step.status == StepStatus.waiting_user and step.confirmation:
            return step
    return None


# ─── Step Handler Implementations ─────────────────────────────────────────────


async def handle_understand_requirement(step: PlanStep, deps: PlannerStepDeps) -> dict:
    """LLM: extract requirement summary from user input."""
    all_user_msgs = [
        m["content"]
        for m in deps.conv_data.get("messages", [])
        if m.get("role") == "user"
    ]
    combined_input = "\n".join(all_user_msgs) if all_user_msgs else deps.user_content
    return {
        "requirement_summary": combined_input[:500],
        "task_classification": deps.memory.get("task_classification", ""),
        "user_input": deps.user_content,
    }


async def handle_collect_context(step: PlanStep, deps: PlannerStepDeps) -> dict:
    """Deterministic: call build_planning_context."""
    if not deps.build_planning_context:
        return {"planning_context_keys": [], "context_block_len": 0}

    planning_ctx = deps.build_planning_context(
        conversation_id=deps.conversation_id,
        user_input={"goal_text": deps.user_content},
    )
    deps.conv_data["planning_context"] = planning_ctx
    ctx_block = deps.render_context_block(planning_ctx) if deps.render_context_block else ""
    return {
        "planning_context_keys": list(planning_ctx.keys()),
        "context_block_len": len(ctx_block) if ctx_block else 0,
    }


async def handle_recall_memory(step: PlanStep, deps: PlannerStepDeps) -> dict:
    """Deterministic: assemble layered memory context."""
    try:
        from app.core import memory_service

        is_replan = (deps.conv_data.get("mode") or "create") == "replan"
        mem_ctx = memory_service.assemble_layered_context(
            memory_service.SCENARIO_REPLAN if is_replan else memory_service.SCENARIO_PLANNER,
            agent_id=deps.conv_data.get("linked_agent_id"),
            planner_conversation_id=deps.conversation_id,
            query=deps.user_content,
        )
        mem_block = memory_service.render_layered_context_block(mem_ctx)
        recalled = sum(
            len(getattr(mem_ctx, attr, None) or [])
            for attr in ("profile", "semantic", "episodic", "procedural")
        )
        return {
            "recalled_count": recalled,
            "mem_block_len": len(mem_block) if mem_block else 0,
        }
    except Exception as exc:
        return {"recalled_count": 0, "error": str(exc)}


async def handle_match_capabilities(step: PlanStep, deps: PlannerStepDeps) -> dict:
    """Deterministic: match capabilities from planning context."""
    if not deps.match_capability_candidates:
        return {"matched_count": 0, "top_names": []}

    planning_ctx = deps.conv_data.get("planning_context")
    candidates = deps.match_capability_candidates(planning_ctx, None)
    return {
        "matched_count": len(candidates),
        "top_names": [c.get("name", "") for c in candidates[:5]],
    }


async def handle_design_architecture(step: PlanStep, deps: PlannerStepDeps) -> dict | StepResult:
    """LLM: generate proposal via the planner model.

    Three-way output classification:
    1. Has structured proposal → done + artifact_ref
    2. Has <a2ui_request kind="missing_info"> → waiting_user(missing_info)
    3. Neither → failed (parse error)
    """
    if not deps.create_agent or not deps.run_conversation:
        return StepResult(status="failed", error="LLM service not configured")

    # Build enriched system prompt
    sys_prompt = ""
    if deps.build_system_prompt_with_memory:
        sys_prompt = deps.build_system_prompt_with_memory(
            deps.memory,
            mode=deps.conv_data.get("mode") or "create",
            replan_context=deps.conv_data.get("replan_context") or "",
        )

    # Append planning context
    planning_ctx = deps.conv_data.get("planning_context")
    if planning_ctx and deps.render_context_block:
        ctx_block = deps.render_context_block(planning_ctx)
        if ctx_block:
            sys_prompt += ctx_block

    # Append memory block
    try:
        from app.core import memory_service

        is_replan = (deps.conv_data.get("mode") or "create") == "replan"
        mem_ctx = memory_service.assemble_layered_context(
            memory_service.SCENARIO_REPLAN if is_replan else memory_service.SCENARIO_PLANNER,
            agent_id=deps.conv_data.get("linked_agent_id"),
            planner_conversation_id=deps.conversation_id,
            query=deps.user_content,
        )
        mem_block = memory_service.render_layered_context_block(mem_ctx)
        if mem_block:
            sys_prompt += mem_block
    except Exception:
        pass

    agent = deps.create_agent(
        system_prompt=sys_prompt,
        model_name=deps.model_cfg["model_name"],
        provider=deps.model_cfg["provider"],
        stream=True,
        temperature=0.7,
        max_tokens=4096,
        base_url=deps.model_cfg.get("base_url"),
        api_key=deps.model_cfg.get("api_key"),
    )

    # Build conversation input with full history for multi-turn
    _all_msgs = deps.conv_data.get("messages", [])
    _conv_input = deps.user_content
    if len(_all_msgs) > 1:
        _conv_input = "\n".join(
            f"{'用户' if m['role'] == 'user' else '助手'}: {m['content'][:300]}"
            for m in _all_msgs[-6:]
        ) + f"\n用户: {deps.user_content}"

    # Include user_feedback from revision
    user_feedback = step.inputs.get("user_feedback")
    if user_feedback:
        _conv_input += f"\n\n[用户修改意见]: {user_feedback}"

    # Stream the response — send visible tokens to frontend via WebSocket
    # so the user can see LLM output in real time. Use strippers to hide
    # <memory_update>, <a2ui_request>, and proposal JSON from the chat.
    full_response = ""
    strippers = deps.create_strippers() if deps.create_strippers else None
    _mem_strip = strippers[0] if strippers else None
    _a2ui_strip = strippers[1] if strippers else None
    _prop_strip = strippers[2] if strippers else None

    async for event_type, content in deps.run_conversation(agent, _conv_input):
        if event_type == "thinking_content":
            # Push thinking content directly to frontend
            if deps.websocket:
                try:
                    await deps.websocket.send_json({"type": "thinking_content", "content": content})
                except Exception:
                    pass
        elif event_type == "token":
            full_response += content
            # Strip memory/a2ui/proposal tags before sending visible token
            if _mem_strip and _a2ui_strip and _prop_strip:
                visible = _mem_strip.feed(content)
                if visible:
                    visible = _a2ui_strip.feed(visible)
                    if visible:
                        visible = _prop_strip.feed(visible)
                        if visible and deps.websocket:
                            try:
                                await deps.websocket.send_json({"type": "token", "content": visible})
                            except Exception:
                                pass
        elif event_type == "done":
            # Flush remaining buffered content
            if _mem_strip and _a2ui_strip and _prop_strip:
                tail = _mem_strip.flush()
                tail = _a2ui_strip.feed(tail) + _a2ui_strip.flush()
                tail = _prop_strip.feed(tail) + _prop_strip.flush()
                if tail and deps.websocket:
                    try:
                        await deps.websocket.send_json({"type": "token", "content": tail})
                    except Exception:
                        pass
            break

    # ── Three-way classification ──
    memory_update = None
    if deps.extract_memory_update:
        _, memory_update = deps.extract_memory_update(full_response)

    # 1. Try to extract structured proposal
    if deps.try_extract_proposal:
        structured = deps.try_extract_proposal(full_response)
    else:
        structured = None

    if structured and isinstance(structured, dict):
        # Normalize required lists
        for _lk in ("nodes", "edges", "tuning_hints"):
            if not isinstance(structured.get(_lk), list):
                structured[_lk] = []
        deps.conv_data["proposal"] = structured
        proposal_validated = False
        if deps.validate_proposal_payload:
            try:
                _validated_payload, proposal_validated = deps.validate_proposal_payload(structured)
                proposal_validated = bool(proposal_validated)
            except Exception:
                proposal_validated = False
        _apply_memory_candidate(
            deps,
            step,
            memory_update,
            has_validated_proposal=proposal_validated,
        )

        # Write proposal as file artifact
        artifact_ref = None
        if deps.write_proposal_json and deps.merge_artifacts:
            try:
                art = deps.write_proposal_json(deps.conversation_id, structured)
                deps.conv_data["file_artifacts"] = deps.merge_artifacts(
                    deps.conv_data.get("file_artifacts", []), art
                )
                artifact_ref = ArtifactRef(type="proposal", id=art.get("path", ""))
                # Notify frontend of file
                if deps.emitter:
                    await deps.emitter._safe_send({"type": "plan_file_updated", **art})
                    await deps.emitter._safe_send({
                        "type": "final_summary",
                        "proposal": structured,
                    })
            except Exception:
                pass

        # Proposal generated successfully — skip the confirm_proposal step
        # because the user already confirmed intent via the LLM's multi-turn
        # a2ui interactions (missing_info cards). No need to ask again.
        deps.conv_data["_skip_confirm_proposal"] = True

        result = StepResult(
            status="success",
            outputs={
                "proposal_generated": True,
                "summary": (structured.get("architecture_summary") or "")[:200],
            },
            artifact_refs=[artifact_ref] if artifact_ref else [],
        )
        return result

    # 2. Check for missing_info a2ui request
    if deps.extract_a2ui_request:
        a2ui_payload = deps.extract_a2ui_request(full_response)
    else:
        a2ui_payload = None

    if a2ui_payload and isinstance(a2ui_payload, dict):
        kind = a2ui_payload.get("kind", "")
        if kind == "missing_info" or not structured:
            # LLM is asking for more info — step enters waiting_user
            # with kind=missing_info
            step.confirmation = ConfirmationRequest(
                kind="missing_info",
                prompt=a2ui_payload.get("prompt", "请补充信息"),
                options=a2ui_payload.get("options", [
                    {"id": "provide", "label": "补充信息"},
                ]),
            )
            # Push a2ui card to frontend
            if deps.normalize_pending_a2ui and deps.emitter:
                a2ui_data = deps.normalize_pending_a2ui(a2ui_payload)
                if a2ui_data:
                    a2ui_data["id"] = step.confirmation.request_id
                    await deps.emitter._safe_send({
                        "type": "a2ui_request",
                        **a2ui_data,
                    })
                    deps.conv_data["pending_a2ui"] = a2ui_data

            _apply_memory_candidate(
                deps,
                step,
                memory_update,
                has_validated_proposal=False,
            )
            return StepResult(status="waiting_user", outputs={"missing_info": True})

    # 3. Extract and apply memory_update regardless of proposal outcome
    _apply_memory_candidate(
        deps,
        step,
        memory_update,
        has_validated_proposal=False,
    )

    # 4. No proposal and no missing_info → failed (or store text response)
    # If there's meaningful text but no structured output, it's a parse failure
    if len(full_response.strip()) > 50:
        # Store as assistant text for context but mark step failed
        deps.conv_data.setdefault("_design_text", full_response)
        return StepResult(
            status="failed",
            error="LLM 未产出结构化 proposal，输出仅为文本回复",
            can_auto_repair=True,
            repair_config={
                "title": "重试生成方案",
                "executor_type": "llm",
                "inputs": {"retry_reason": "no_structured_proposal"},
            },
        )

    return StepResult(
        status="failed",
        error="LLM 输出为空或过短",
    )


async def handle_compile_draft(step: PlanStep, deps: PlannerStepDeps) -> dict | StepResult:
    """Validate proposal schema (pre-compile check).

    Reads proposal from design_architecture's artifact_refs first,
    falling back to conv_data["proposal"] for compatibility.
    """
    # Try to read proposal from artifact_refs
    proposal = None
    for s in deps.conv_data.get("_plan_steps", []):
        if isinstance(s, dict) and s.get("id") == "design_architecture":
            for ref in s.get("artifact_refs", []):
                if isinstance(ref, dict) and ref.get("type") == "proposal":
                    # artifact ref exists — read from conv_data (canonical source)
                    proposal = deps.conv_data.get("proposal")
                    break

    # Fallback to conv_data["proposal"] directly
    if not proposal:
        proposal = deps.conv_data.get("proposal")

    if not proposal:
        return {
            "compile_success": False,
            "skipped": True,
            "reason": "尚未生成结构化方案（仍在澄清阶段），编译跳过",
        }

    if not deps.validate_proposal_payload:
        return {"compile_success": True, "validated": False, "reason": "No validator available"}

    try:
        _payload, _ok = deps.validate_proposal_payload(proposal)
        if _ok:
            return {
                "compile_success": True,
                "validated": True,
                "node_count": len(proposal.get("nodes") or []),
                "edge_count": len(proposal.get("edges") or []),
            }
        else:
            return {
                "compile_success": False,
                "validated": False,
                "reason": "Proposal 结构校验未通过",
            }
    except Exception as exc:
        return StepResult(
            status="failed",
            error=str(exc),
            can_auto_repair=True,
            repair_config={"title": "修复方案结构", "inputs": {"error": str(exc)}},
        )


# ─── Handler Registry Builder ─────────────────────────────────────────────────


def build_step_handler_registry(
    deps: PlannerStepDeps,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build handler registries for deterministic and LLM executors.

    Returns (deterministic_handlers, llm_handlers) dicts mapping step_id → handler.
    Each handler is already curried with deps via closure.
    """

    async def _det_collect(step, ctx):
        return await handle_collect_context(step, deps)

    async def _det_recall(step, ctx):
        return await handle_recall_memory(step, deps)

    async def _det_match(step, ctx):
        return await handle_match_capabilities(step, deps)

    async def _det_compile(step, ctx):
        return await handle_compile_draft(step, deps)

    async def _llm_understand(step, ctx):
        return await handle_understand_requirement(step, deps)

    async def _llm_design(step, ctx):
        return await handle_design_architecture(step, deps)

    det_services = {
        "collect_context": _det_collect,
        "recall_memory": _det_recall,
        "match_capabilities": _det_match,
        "compile_draft": _det_compile,
        "generate_draft": _det_compile,
    }
    llm_handlers = {
        "understand_requirement": _llm_understand,
        "design_architecture": _llm_design,
    }
    return det_services, llm_handlers

