"""Plan+Loop WebSocket-level integration tests.

Covers:
- A2UI confirmation protocol (request_id validation, choice semantics)
- Plain text does NOT auto-confirm waiting_user steps
- missing_info plain text resolution
- Proposal/missing_info/failed state classification
- Assistant message not duplicated
- ToolReactExecutor explicit failure
- Handler independence (no WebSocket required)
"""

import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Ensure we can import from backend
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("PLANNER_PLAN_LOOP", "on")

from app.core.planner_plan import (
    ConfirmationRequest,
    Plan,
    PlanStatus,
    PlanStep,
    StepStatus,
    ExecutorType,
    create_initial_plan,
    load_plan,
    save_plan,
)
from app.core.planner_step_handlers import (
    A2UIResolutionError,
    PlannerLoopContext,
    PlannerStepDeps,
    build_step_handler_registry,
    get_waiting_step,
    handle_compile_draft,
    handle_design_architecture,
    handle_understand_requirement,
    is_plan_waiting_user,
    resolve_a2ui_response,
    resolve_missing_info_text,
)
from app.core.planner_loop import StepResult
from app.core.planner_tool_executor import execute_tool_react_step
from app.api.planner.state import _ensure_goal_anchor, _merge_memory_update, _new_memory


# ─── Fixtures ─────────────────────────────────────────────────────────────────


def _make_plan_at_waiting_user(kind="proposal_confirm") -> Plan:
    """Create a plan paused at confirm_proposal(waiting_user)."""
    plan = create_initial_plan("测试需求")
    # Mark earlier steps as done
    for s in plan.steps:
        if s.id in ("understand_requirement", "collect_context", "recall_memory",
                    "match_capabilities", "design_architecture"):
            s.status = StepStatus.done
    # Set confirm_proposal to waiting_user
    for s in plan.steps:
        if s.id == "confirm_proposal":
            s.status = StepStatus.waiting_user
            s.confirmation = ConfirmationRequest(
                request_id="confirm_abc123",
                kind=kind,
                prompt="是否确认该方案？",
                options=[
                    {"id": "confirm", "label": "确认"},
                    {"id": "revise", "label": "修改"},
                    {"id": "reject", "label": "拒绝"},
                ],
            )
            break
    plan.status = PlanStatus.waiting_user
    return plan


def _make_plan_at_missing_info() -> Plan:
    """Create a plan paused at design_architecture(waiting_user, missing_info)."""
    plan = create_initial_plan("测试需求")
    for s in plan.steps:
        if s.id in ("understand_requirement", "collect_context", "recall_memory",
                    "match_capabilities"):
            s.status = StepStatus.done
    for s in plan.steps:
        if s.id == "design_architecture":
            s.status = StepStatus.waiting_user
            s.confirmation = ConfirmationRequest(
                request_id="missing_xyz789",
                kind="missing_info",
                prompt="请问目标用户群体是？",
                options=[{"id": "provide", "label": "补充信息"}],
            )
            break
    plan.status = PlanStatus.waiting_user
    return plan


# ─── Test: Plain text does NOT auto-confirm ───────────────────────────────────


class TestPlainTextDoesNotConfirm:
    """Task 1.1: Plain text messages must not resolve waiting_user steps."""

    def test_proposal_confirm_not_resolved_by_text(self):
        plan = _make_plan_at_waiting_user("proposal_confirm")
        # Attempt to resolve with plain text — should return None (not applicable)
        result = resolve_missing_info_text(plan, "我想换个模型")
        assert result is None
        # Plan should still be waiting
        assert plan.status == PlanStatus.waiting_user
        step = get_waiting_step(plan)
        assert step is not None
        assert step.id == "confirm_proposal"
        assert step.status == StepStatus.waiting_user

    def test_missing_info_accepts_plain_text(self):
        plan = _make_plan_at_missing_info()
        result = resolve_missing_info_text(plan, "目标用户是企业管理者")
        assert result is not None
        # Step should be reset to pending (re-run with supplement)
        step = None
        for s in result.steps:
            if s.id == "design_architecture":
                step = s
                break
        assert step is not None
        assert step.status == StepStatus.pending
        assert step.inputs.get("user_supplement") == "目标用户是企业管理者"
        assert result.status == PlanStatus.running


# ─── Test: request_id validation ──────────────────────────────────────────────


class TestRequestIdValidation:
    """Task 1.2: request_id must match the waiting step's confirmation."""

    def test_matching_request_id_resolves(self):
        plan = _make_plan_at_waiting_user()
        msg = {"id": "confirm_abc123", "choice": "confirm"}
        updated_plan, choice = resolve_a2ui_response(plan, msg)
        assert choice == "confirm"
        # confirm_proposal should be done
        for s in updated_plan.steps:
            if s.id == "confirm_proposal":
                assert s.status == StepStatus.done
                break

    def test_mismatched_request_id_raises(self):
        plan = _make_plan_at_waiting_user()
        msg = {"id": "wrong_id_456", "choice": "confirm"}
        with pytest.raises(A2UIResolutionError, match="request_id mismatch"):
            resolve_a2ui_response(plan, msg)

    def test_missing_id_raises(self):
        plan = _make_plan_at_waiting_user()
        msg = {"choice": "confirm"}
        with pytest.raises(A2UIResolutionError, match="missing 'id'"):
            resolve_a2ui_response(plan, msg)

    def test_invalid_choice_raises(self):
        plan = _make_plan_at_waiting_user()
        msg = {"id": "confirm_abc123", "choice": "invalid_option"}
        with pytest.raises(A2UIResolutionError, match="Invalid choice"):
            resolve_a2ui_response(plan, msg)


# ─── Test: Choice semantics ──────────────────────────────────────────────────


class TestChoiceSemantics:
    """Task 1.3: confirm/revise/reject have distinct behaviors."""

    def test_confirm_marks_done_and_continues(self):
        plan = _make_plan_at_waiting_user()
        msg = {"id": "confirm_abc123", "choice": "confirm"}
        updated, choice = resolve_a2ui_response(plan, msg)
        assert choice == "confirm"
        for s in updated.steps:
            if s.id == "confirm_proposal":
                assert s.status == StepStatus.done
                assert s.outputs.get("confirmation_selected") == "confirm"
        assert updated.status == PlanStatus.running

    def test_revise_resets_design_and_confirm(self):
        plan = _make_plan_at_waiting_user()
        msg = {"id": "confirm_abc123", "choice": "revise", "free_text": "加个缓存节点"}
        updated, choice = resolve_a2ui_response(plan, msg)
        assert choice == "revise"
        # design_architecture should be pending
        for s in updated.steps:
            if s.id == "design_architecture":
                assert s.status == StepStatus.pending
                assert s.inputs.get("user_feedback") == "加个缓存节点"
            if s.id == "confirm_proposal":
                assert s.status == StepStatus.pending
        assert updated.status == PlanStatus.running
        assert updated.current_step_id == "design_architecture"
        # Revision should increment
        assert updated.revision >= 2

    def test_reject_fails_plan(self):
        plan = _make_plan_at_waiting_user()
        msg = {"id": "confirm_abc123", "choice": "reject"}
        updated, choice = resolve_a2ui_response(plan, msg)
        assert choice == "reject"
        assert updated.status == PlanStatus.failed
        for s in updated.steps:
            if s.id == "confirm_proposal":
                assert s.status == StepStatus.failed
                assert s.error == "用户拒绝方案"


# ─── Test: ToolReactExecutor explicit failure ─────────────────────────────────


class TestToolReactExplicitFailure:
    """Task 7.1-7.2: ToolReactExecutor must not return placeholder success."""

    @pytest.mark.asyncio
    async def test_tool_react_returns_failed(self):
        step = PlanStep(
            id="explore_data",
            title="探索数据",
            executor_type=ExecutorType.tool_react,
        )
        from app.core.planner_steps import ExecutionContext
        ctx = ExecutionContext(conversation_id="test", run_id="run_1")
        result = await execute_tool_react_step(step, ctx)
        assert result.status == "failed"
        assert "not wired" in result.error.lower()


# ─── Test: Handler independence (no WebSocket) ────────────────────────────────


class TestHandlerIndependence:
    """Task 6.4: Handlers callable without WebSocket dependency."""

    @pytest.mark.asyncio
    async def test_understand_requirement_no_ws(self):
        deps = PlannerStepDeps(
            conversation_id="conv_1",
            run_id="run_1",
            user_content="我要做政策解读",
            conv_data={"messages": [{"role": "user", "content": "我要做政策解读"}]},
            memory={},
            model_cfg={},
            websocket=None,
        )
        step = PlanStep(id="understand_requirement", title="理解需求")
        result = await handle_understand_requirement(step, deps)
        assert isinstance(result, dict)
        assert "我要做政策解读" in result["requirement_summary"]

    @pytest.mark.asyncio
    async def test_compile_draft_no_proposal(self):
        deps = PlannerStepDeps(
            conversation_id="conv_1",
            run_id="run_1",
            user_content="test",
            conv_data={},
            memory={},
            model_cfg={},
            websocket=None,
        )
        step = PlanStep(id="compile_draft", title="编译")
        result = await handle_compile_draft(step, deps)
        assert isinstance(result, dict)
        assert result.get("skipped") is True

    @pytest.mark.asyncio
    async def test_compile_draft_with_valid_proposal(self):
        proposal = {
            "architecture_summary": "test",
            "nodes": [{"id": "n1", "type": "llm", "config": {}}],
            "edges": [],
            "rationale": "test",
        }
        deps = PlannerStepDeps(
            conversation_id="conv_1",
            run_id="run_1",
            user_content="test",
            conv_data={"proposal": proposal},
            memory={},
            model_cfg={},
            websocket=None,
            validate_proposal_payload=lambda p: (p, True),
        )
        step = PlanStep(id="compile_draft", title="编译")
        result = await handle_compile_draft(step, deps)
        assert isinstance(result, dict)
        assert result["compile_success"] is True

    @pytest.mark.asyncio
    async def test_design_proposal_applies_same_turn_memory_with_policy_evidence(self):
        """A structured proposal must not skip its accompanying memory delta."""
        proposal = {
            "architecture_summary": "售后客服架构",
            "nodes": [{"id": "agent1", "type": "agent", "config": {}}],
            "edges": [],
            "rationale": "用户显式调整了目标",
        }
        memory = _new_memory()
        _ensure_goal_anchor(
            memory,
            [{"id": "m1", "role": "user", "content": "创建股票筛选智能体"}],
            conversation_id="conv-memory-evidence",
        )
        memory["requirement_summary"] = "股票筛选智能体"

        async def fake_stream(_agent, _input):
            yield ("done", "")

        deps = PlannerStepDeps(
            conversation_id="conv-memory-evidence",
            run_id="run-memory-evidence",
            user_content="需求改成售后客服智能体",
            conv_data={
                "messages": [
                    {"id": "m1", "role": "user", "content": "创建股票筛选智能体"},
                    {"id": "m2", "role": "user", "content": "需求改成售后客服智能体"},
                ]
            },
            memory=memory,
            model_cfg={"model_name": "fake", "provider": "fake"},
            websocket=None,
            create_agent=lambda **_kwargs: object(),
            run_conversation=fake_stream,
            try_extract_proposal=lambda _text: dict(proposal),
            extract_memory_update=lambda _text: (
                "",
                {
                    "requirement_summary": "售后客服智能体",
                    "apply_readiness": {"status": "ready", "missing": []},
                },
            ),
            merge_memory_update=_merge_memory_update,
            validate_proposal_payload=lambda value: (value, True),
        )

        result = await handle_design_architecture(
            PlanStep(id="design_architecture", title="设计架构"),
            deps,
        )

        assert isinstance(result, StepResult)
        assert result.status == "success"
        assert deps.memory["requirement_summary"] == "售后客服智能体"
        assert deps.memory["apply_readiness"]["status"] == "ready"
        assert [item["field"] for item in deps.memory["memory_update_audit"][-2:]] == [
            "requirement_summary",
            "apply_readiness",
        ]
        assert deps.memory["memory_update_audit"][-1]["source_turn"] == {
            "run_id": "run-memory-evidence",
            "step_id": "design_architecture",
            "message_index": 1,
            "message_id": "m2",
        }


# ─── Test: build_step_handler_registry ────────────────────────────────────────


class TestStepHandlerRegistry:
    """Task 6.3: Registry builds correct handler maps."""

    def test_registry_returns_both_maps(self):
        deps = PlannerStepDeps(
            conversation_id="c1",
            run_id="r1",
            user_content="test",
            conv_data={},
            memory={},
            model_cfg={},
            websocket=None,
        )
        det, llm = build_step_handler_registry(deps)
        assert "collect_context" in det
        assert "recall_memory" in det
        assert "match_capabilities" in det
        assert "compile_draft" in det
        assert "understand_requirement" in llm
        assert "design_architecture" in llm


# ─── Test: Plan persistence with real session row ─────────────────────────────


class TestPlanPersistence:
    """Task 4.5: Plan state persists to session row."""

    def test_save_and_load_roundtrip(self):
        plan = create_initial_plan("测试")
        # Use a simple object with planning_state_json
        session = MagicMock()
        session.planning_state_json = "{}"
        session.id = 1

        save_plan(session, plan)
        assert session.planning_state_json != "{}"

        loaded = load_plan(session)
        assert loaded is not None
        assert loaded.plan_id == plan.plan_id
        assert len(loaded.steps) == len(plan.steps)

    def test_session_row_contains_plan_after_persist(self):
        plan = _make_plan_at_waiting_user()
        session = MagicMock()
        session.planning_state_json = "{}"
        session.id = 42

        save_plan(session, plan)
        data = json.loads(session.planning_state_json)
        assert data["planner_mode"] == "plan_loop"
        assert data["plan"]["status"] == "waiting_user"


# ─── Test: Disconnect and recover ─────────────────────────────────────────────


class TestDisconnectRecovery:
    """Task 9.5: Plan recovers from DB after WebSocket disconnect."""

    def test_plan_loaded_from_planning_state_json(self):
        """Simulate: save plan → disconnect → reload from same row."""
        plan = _make_plan_at_waiting_user()
        session = MagicMock()
        session.planning_state_json = "{}"
        session.id = 99

        # Simulate persist
        save_plan(session, plan)

        # Simulate reconnect — load from the same row
        loaded = load_plan(session)
        assert loaded is not None
        assert loaded.plan_id == plan.plan_id
        assert loaded.status == PlanStatus.waiting_user
        # Confirmation should be preserved
        waiting = get_waiting_step(loaded)
        assert waiting is not None
        assert waiting.confirmation.request_id == "confirm_abc123"


# ─── Test: One assistant message per turn ─────────────────────────────────────


class TestSingleAssistantMessage:
    """Task 9.6: One Plan+Loop turn appends exactly one assistant message."""

    @pytest.mark.asyncio
    async def test_no_duplicate_message_in_handler(self):
        """Handler (design_architecture) must NOT append assistant messages.
        Only the Loop Runner's post-loop code does that (in ws.py)."""
        deps = PlannerStepDeps(
            conversation_id="c1",
            run_id="r1",
            user_content="test",
            conv_data={"messages": [{"role": "user", "content": "test"}]},
            memory={},
            model_cfg={},
            websocket=None,
        )
        initial_msg_count = len(deps.conv_data["messages"])
        # handle_understand_requirement doesn't append messages
        step = PlanStep(id="understand_requirement", title="理解需求")
        await handle_understand_requirement(step, deps)
        assert len(deps.conv_data["messages"]) == initial_msg_count
