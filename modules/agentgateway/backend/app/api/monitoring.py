"""Runtime Monitoring API — aggregated metrics and Langfuse trace history."""

from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select, desc

from app.core.database import get_session
from app.core.observability import (
    _get_langfuse,
    create_trace_url,
    agent_monitoring_overview,
    langfuse_health,
)
from app.core.hermes import degradation_detector
from app.models.db import Agent, AgentRunSummary
from app.models.schemas import MonitoringResponse, MonitoringTraceItem, MonitoringTracesResponse, CredentialStatusItem

router = APIRouter(prefix="/agents", tags=["monitoring"])


@router.get("/{agent_id}/monitoring", response_model=MonitoringResponse)
def get_monitoring(agent_id: int, session: Session = Depends(get_session)):
    agent = session.get(Agent, agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    # Degradation status from the rolling-window detector.
    deg_status = degradation_detector.get_status(agent_id)

    # Real metrics aggregated from local AgentRunSummary rows (same caliber as
    # the release gate). No Langfuse dependency — survives Langfuse downtime.
    overview = agent_monitoring_overview(agent_id, session)
    # Langfuse connectivity/health (enabled/auth_ok/base_url; base_url only).
    health = langfuse_health()
    # Credential health: per-provider + langfuse status (no key values).
    from app.core.credential_health import scan_credentials
    cred_status = [CredentialStatusItem(name=c["name"], status=c["status"]) for c in scan_credentials()]

    return MonitoringResponse(
        agent_id=agent_id,
        request_count_24h=overview["request_count_24h"],
        request_count_7d=overview["request_count_7d"],
        request_count_30d=overview["request_count_30d"],
        p50_latency_ms=overview["p50_latency_ms"],
        p95_latency_ms=overview["p95_latency_ms"],
        token_consumption=overview["token_consumption"],
        error_rate=overview["error_rate"],
        status=deg_status["status"],
        langfuse_enabled=health["langfuse_enabled"],
        langfuse_auth_ok=health["langfuse_auth_ok"],
        langfuse_base_url=health["langfuse_base_url"],
        credential_status=cred_status,
    )


@router.get("/{agent_id}/monitoring/traces", response_model=MonitoringTracesResponse)
def get_monitoring_traces(agent_id: int, limit: int = 20, session: Session = Depends(get_session)):
    agent = session.get(Agent, agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    # Prefer locally persisted run summaries: instant, survive Langfuse downtime.
    rows = session.exec(
        select(AgentRunSummary)
        .where(AgentRunSummary.agent_id == agent_id)
        .order_by(desc(AgentRunSummary.created_at))
        .limit(limit)
    ).all()

    if rows:
        traces = [
            MonitoringTraceItem(
                trace_id=row.trace_id,
                trace_url=create_trace_url(row.trace_id) if row.trace_id else "",
                duration_ms=row.total_duration_ms,
                status=row.status,
                node_count=row.node_count,
                created_at=str(row.created_at),
            )
            for row in rows
        ]
        return MonitoringTracesResponse(agent_id=agent_id, traces=traces)

    # Fallback: no local rows — query Langfuse directly. An unreachable
    # Langfuse yields an empty list rather than raising.
    lf = _get_langfuse()
    traces: List[MonitoringTraceItem] = []
    if lf:
        try:
            raw_traces = lf.fetch_traces(
                name=f"dag-execution-agent-{agent_id}",
                limit=limit,
            )
            for t in raw_traces:
                traces.append(MonitoringTraceItem(
                    trace_id=str(t.id),
                    trace_url=create_trace_url(str(t.id)),
                    duration_ms=getattr(t, "duration", 0) or 0,
                    status="completed",
                    node_count=0,
                    created_at=str(getattr(t, "timestamp", "")),
                ))
        except Exception:
            pass

    return MonitoringTracesResponse(agent_id=agent_id, traces=traces)
