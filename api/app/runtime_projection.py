"""Product projections derived from the immutable Runtime Event ledger.

These rows exist for the current Atlas UI and reporting APIs.  They are never
consulted to resume a graph; RuntimeRun plus the LangGraph checkpoint remain
the execution authority.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import RuntimeRun, WorkflowRun, WorkflowStep
from .runtime_contract import RuntimeStartRequest


def ensure_workflow_run(db: Session, runtime_run: RuntimeRun, request: RuntimeStartRequest) -> WorkflowRun:
    if runtime_run.legacy_workflow_run_id:
        existing = db.get(WorkflowRun, runtime_run.legacy_workflow_run_id)
        if existing is not None:
            return existing
    projection = WorkflowRun(
        workspace_id=request.workspace_id,
        conversation_id=request.conversation_id,
        agent_id=request.agent_id,
        user_id=request.user_id,
        source=request.source.value,
        input_text=request.input,
        status="created",
        started_at=datetime.now(timezone.utc),
    )
    db.add(projection)
    db.flush()
    runtime_run.legacy_workflow_run_id = projection.id
    return projection


def project_runtime_event(
    db: Session,
    *,
    runtime_run: RuntimeRun,
    event_type: str,
    sequence: int,
    payload: dict[str, Any],
    timestamp: datetime,
) -> None:
    """Apply one already-persisted event to the legacy product projection."""

    request = RuntimeStartRequest.model_validate_json(runtime_run.input_json)
    workflow = ensure_workflow_run(db, runtime_run, request)

    if event_type in {"run.started", "run.resumed", "escalation.resolved"}:
        workflow.status = "running"
        workflow.started_at = workflow.started_at or timestamp
    elif event_type in {"interrupt.requested", "run.paused", "run.escalated"}:
        workflow.status = "waiting_confirmation"
    elif event_type == "strategy.selected":
        workflow.plan_json = _json({"strategy": payload})
    elif event_type in {"plan.created", "plan.updated"}:
        workflow.plan_json = _json(payload)
    elif event_type == "node.started":
        node = str(payload.get("node") or payload.get("name") or "runtime")
        db.add(WorkflowStep(
            run_id=workflow.id,
            index=sequence,
            type=_step_type(node),
            title=node,
            executor=_step_executor(node),
            status="running",
            input_json=_json(payload),
            started_at=timestamp,
        ))
    elif event_type in {"node.completed", "node.failed"}:
        node = str(payload.get("node") or payload.get("name") or "runtime")
        step = db.scalar(
            select(WorkflowStep).where(
                WorkflowStep.run_id == workflow.id,
                WorkflowStep.title == node,
                WorkflowStep.status == "running",
            ).order_by(WorkflowStep.index.desc())
        )
        if step is not None:
            failed = event_type == "node.failed" or payload.get("status") == "failed"
            step.status = "failed" if failed else "succeeded"
            step.output_json = _json(payload)
            step.error = str(payload.get("error") or payload.get("message") or "") if failed else ""
            step.ended_at = timestamp
    elif event_type == "run.completed":
        workflow.status = "succeeded"
        workflow.output_json = _json({"answer": payload.get("output", ""), "runtime": payload})
        workflow.ended_at = timestamp
    elif event_type == "run.failed":
        workflow.status = "failed"
        workflow.error = str(payload.get("message") or payload.get("error") or payload.get("category") or "runtime failed")
        workflow.ended_at = timestamp
    elif event_type == "run.cancelled":
        workflow.status = "cancelled"
        workflow.ended_at = timestamp


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _step_type(node: str) -> str:
    if node == "load_memory":
        return "retrieve"
    if node == "route_skills":
        return "skill"
    if node in {"react", "dispatch_tools"}:
        return "tool"
    if node == "review":
        return "evaluate"
    if node in {"execute", "create_reviewer"}:
        return "agent"
    return "respond"


def _step_executor(node: str) -> str:
    if node == "load_memory":
        return "rag"
    if node == "route_skills":
        return "skill_agent"
    if node == "dispatch_tools":
        return "mcp_tool"
    if node == "review":
        return "reviewer_subagent"
    if node == "execute":
        return "multi_agent_orchestrator"
    return "langgraph"
