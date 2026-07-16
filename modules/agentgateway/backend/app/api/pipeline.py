"""Pipeline status and trace endpoints."""

from datetime import datetime
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from app.core.database import get_session
from app.models.db import PipelineNodeStatusModel, _utcnow
from app.models.schemas import (
    PipelineNodeStatusSchema,
    PipelineStatusResponse,
    TraceItem,
    LatestTracesResponse,
)

router = APIRouter(prefix="/agents", tags=["pipeline"])

NODE_NAMES = ["p", "m", "k", "t"]


def _ensure_nodes(session: Session, agent_id: int):
    existing = session.exec(
        select(PipelineNodeStatusModel).where(PipelineNodeStatusModel.agent_id == agent_id)
    ).all()
    existing_names = {n.node_name for n in existing}
    for name in NODE_NAMES:
        if name not in existing_names:
            default_status = "optional" if name in ("k", "t") else "pending"
            session.add(PipelineNodeStatusModel(
                agent_id=agent_id,
                node_name=name,
                status=default_status,
            ))
    session.commit()


def update_node_status(
    session: Session,
    agent_id: int,
    node_name: str,
    status: str,
    quality_score: float | None = None,
    trace_url: str | None = None,
):
    _ensure_nodes(session, agent_id)
    node = session.exec(
        select(PipelineNodeStatusModel).where(
            PipelineNodeStatusModel.agent_id == agent_id,
            PipelineNodeStatusModel.node_name == node_name,
        )
    ).first()
    if node:
        node.status = status
        if quality_score is not None:
            node.quality_score = quality_score
        if trace_url is not None:
            node.trace_url = trace_url
        if status in ("in_progress",) and node.started_at is None:
            node.started_at = _utcnow()
        if status in ("complete", "failed"):
            node.completed_at = _utcnow()
        session.add(node)
        session.commit()


def update_tool_node_status(session: Session, agent_id: int, has_tools: bool):
    """Update T node status based on whether tools are configured."""
    _ensure_nodes(session, agent_id)
    status = "complete" if has_tools else "optional"
    node = session.exec(
        select(PipelineNodeStatusModel).where(
            PipelineNodeStatusModel.agent_id == agent_id,
            PipelineNodeStatusModel.node_name == "t",
        )
    ).first()
    if node:
        node.status = status
        if has_tools:
            node.completed_at = _utcnow()
        session.add(node)
        session.commit()


@router.get("/{agent_id}/pipeline-status", response_model=PipelineStatusResponse)
def get_pipeline_status(agent_id: int, session: Session = Depends(get_session)):
    _ensure_nodes(session, agent_id)
    nodes = session.exec(
        select(PipelineNodeStatusModel).where(PipelineNodeStatusModel.agent_id == agent_id)
    ).all()
    result = [
        PipelineNodeStatusSchema(
            node_name=n.node_name,
            status=n.status,
            quality_score=n.quality_score,
            started_at=n.started_at.isoformat() if n.started_at else None,
            completed_at=n.completed_at.isoformat() if n.completed_at else None,
            trace_url=n.trace_url,
        )
        for n in nodes
    ]
    return PipelineStatusResponse(agent_id=agent_id, nodes=result)


@router.get("/{agent_id}/traces/latest", response_model=LatestTracesResponse)
def get_latest_traces(agent_id: int, session: Session = Depends(get_session)):
    _ensure_nodes(session, agent_id)
    nodes = session.exec(
        select(PipelineNodeStatusModel).where(
            PipelineNodeStatusModel.agent_id == agent_id,
            PipelineNodeStatusModel.trace_url != None,  # noqa: E711
        )
    ).all()
    traces = [
        TraceItem(
            trace_id=n.trace_url.split("/trace/")[-1] if "/trace/" in (n.trace_url or "") else "",
            trace_url=n.trace_url or "",
            created_at=n.completed_at.isoformat() if n.completed_at else "",
        )
        for n in nodes if n.trace_url
    ]
    return LatestTracesResponse(agent_id=agent_id, traces=traces)