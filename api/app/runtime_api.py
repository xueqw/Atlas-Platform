"""Opt-in HTTP boundary for the LangGraph runtime foundation.

This router is deliberately additive.  It resolves every tenant and version
field server-side, so callers cannot start a run for another workspace or
silently change an Agent version after a run has begun.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .auth import current_user, current_workspace_id
from .config import settings
from .database import SessionLocal, get_db
from .model_gateway import complete
from .models import Agent, AgentVersion, User
from .runtime_contract import RuntimeAccessDenied, RuntimeSource, RuntimeStartRequest
from .runtime_checkpoint import open_postgres_checkpointer
from .runtime_graph import AtlasAgentState, RuntimePhaseOneGraph
from .runtime_memory import RuntimeMemoryProvider
from .runtime_persistence import SqlAlchemyRuntimeEventRepository, SqlAlchemyRuntimeRunRepository
from .runtime_registry import RuntimeGraphRegistry, runtime_graph_registry
from .runtime_service import AgentRuntimeService, GraphLegacyAdapter, RuntimeFeatureFlags


router = APIRouter(prefix="/api/runtime", tags=["runtime"])


class RuntimeRunCreate(BaseModel):
    """Client input only. Identity and version ownership are resolved below."""

    model_config = ConfigDict(extra="forbid")

    agent_id: str = Field(min_length=1, max_length=128)
    input: str = Field(min_length=1, max_length=100_000)
    idempotency_key: str = Field(min_length=8, max_length=120)
    source: RuntimeSource = RuntimeSource.WORKBENCH
    version_id: str | None = Field(default=None, max_length=128)
    conversation_id: str | None = Field(default=None, max_length=128)
    requested_resources: tuple[str, ...] = ()


def _snapshot(version: AgentVersion) -> dict[str, Any]:
    try:
        parsed = json.loads(version.snapshot_json)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=409, detail="智能体版本快照损坏") from exc
    if not isinstance(parsed, dict):
        raise HTTPException(status_code=409, detail="智能体版本快照格式无效")
    return parsed


def _resolve_version(db: Session, *, workspace_id: str, agent_id: str, version_id: str | None) -> tuple[Agent, AgentVersion, dict[str, Any]]:
    agent = db.scalar(select(Agent).where(Agent.id == agent_id, Agent.workspace_id == workspace_id))
    if agent is None:
        raise HTTPException(status_code=404, detail="智能体不存在或不属于当前工作区")
    selected_id = version_id or agent.current_version_id or agent.published_version_id
    if not selected_id:
        raise HTTPException(status_code=409, detail="智能体尚未保存可运行版本")
    version = db.scalar(select(AgentVersion).where(AgentVersion.id == selected_id, AgentVersion.agent_id == agent.id))
    if version is None:
        raise HTTPException(status_code=404, detail="智能体版本不存在或不属于该智能体")
    return agent, version, _snapshot(version)


def _model_for_snapshot(snapshot: dict[str, Any]):
    system_prompt = str(snapshot.get("system_prompt") or snapshot.get("prompt") or "你是一名可靠、严谨的企业智能助手。")
    model = str(snapshot.get("model") or "") or None

    async def invoke(state: AtlasAgentState) -> str:
        response = await complete(
            [{"role": "system", "content": system_prompt}, *state.messages[-14:]],
            model=model,
        )
        # A deterministic result makes an unconfigured local development
        # environment observable without silently changing the old chat path.
        return response or "当前运行时模型未配置或没有返回内容，请在模型管理中配置该版本使用的模型。"

    return invoke


def _legacy_for_snapshot(snapshot: dict[str, Any]) -> GraphLegacyAdapter:
    return GraphLegacyAdapter(RuntimePhaseOneGraph(
        _model_for_snapshot(snapshot),
        prefer_langgraph=False,
        memory_loader=RuntimeMemoryProvider(SessionLocal).load,
    ))


def _service_for_snapshot(snapshot: dict[str, Any], *, checkpointer: Any | None = None) -> AgentRuntimeService:
    flags = RuntimeFeatureFlags(
        langgraph_enabled=settings.langgraph_runtime_enabled,
        allow_legacy_fallback=settings.langgraph_runtime_legacy_fallback,
    )
    graph = runtime_graph_registry.create(
        RuntimeGraphRegistry.PHASE_ONE_REACT,
        _model_for_snapshot(snapshot),
        checkpointer=checkpointer,
        memory_loader=RuntimeMemoryProvider(SessionLocal).load,
    )
    return AgentRuntimeService(
        graph,
        event_repository=SqlAlchemyRuntimeEventRepository(SessionLocal),
        run_repository=SqlAlchemyRuntimeRunRepository(SessionLocal),
        legacy_adapter=_legacy_for_snapshot(snapshot),
        flags=flags,
    )


@asynccontextmanager
async def _start_service(snapshot: dict[str, Any]) -> AsyncIterator[AgentRuntimeService]:
    """Create a durable graph only for an enabled LangGraph execution."""
    if not settings.langgraph_runtime_enabled:
        yield _service_for_snapshot(snapshot)
        return
    if not settings.langgraph_checkpoint_database_url:
        raise HTTPException(status_code=503, detail="LangGraph 运行时尚未配置 PostgreSQL Checkpointer")
    async with open_postgres_checkpointer() as checkpointer:
        yield _service_for_snapshot(snapshot, checkpointer=checkpointer)


def _to_start_request(payload: RuntimeRunCreate, *, workspace_id: str, user_id: str, version_id: str) -> RuntimeStartRequest:
    return RuntimeStartRequest(
        workspace_id=workspace_id,
        user_id=user_id,
        agent_id=payload.agent_id,
        agent_version_id=version_id,
        source=payload.source,
        input=payload.input,
        idempotency_key=payload.idempotency_key,
        conversation_id=payload.conversation_id,
        requested_resources=payload.requested_resources,
    )


def _owned_state(service: AgentRuntimeService, *, run_id: str, workspace_id: str, user_id: str):
    try:
        state = service.get_state(run_id=run_id, workspace_id=workspace_id)
    except RuntimeAccessDenied as exc:
        raise HTTPException(status_code=404, detail="运行不存在") from exc
    if state.identity.user_id != user_id:
        raise HTTPException(status_code=404, detail="运行不存在")
    return state


@router.post("/runs")
async def start_run(payload: RuntimeRunCreate, user: User = Depends(current_user), workspace_id: str = Depends(current_workspace_id), db: Session = Depends(get_db)):
    _, version, snapshot = _resolve_version(db, workspace_id=workspace_id, agent_id=payload.agent_id, version_id=payload.version_id)
    try:
        async with _start_service(snapshot) as service:
            return await service.start(_to_start_request(payload, workspace_id=workspace_id, user_id=user.id, version_id=version.id))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/runs/{run_id}")
def get_run(run_id: str, user: User = Depends(current_user), workspace_id: str = Depends(current_workspace_id)):
    return _owned_state(_service_for_snapshot({}), run_id=run_id, workspace_id=workspace_id, user_id=user.id)


@router.get("/runs/{run_id}/events")
def get_events(run_id: str, after_sequence: int = Query(default=0, ge=0), user: User = Depends(current_user), workspace_id: str = Depends(current_workspace_id)):
    service = _service_for_snapshot({})
    _owned_state(service, run_id=run_id, workspace_id=workspace_id, user_id=user.id)
    return service.stream(run_id=run_id, workspace_id=workspace_id, after_sequence=after_sequence)


def _sse_event(event: Any) -> str:
    return f"id: {event.sequence}\nevent: {event.type}\ndata: {event.model_dump_json()}\n\n"


@router.get("/runs/{run_id}/events/stream")
async def stream_events(run_id: str, after_sequence: int = Query(default=0, ge=0), user: User = Depends(current_user), workspace_id: str = Depends(current_workspace_id)):
    service = _service_for_snapshot({})
    _owned_state(service, run_id=run_id, workspace_id=workspace_id, user_id=user.id)

    async def emit() -> AsyncIterator[str]:
        cursor = after_sequence
        while True:
            events = service.stream(run_id=run_id, workspace_id=workspace_id, after_sequence=cursor)
            for event in events:
                cursor = event.sequence
                yield _sse_event(event)
            latest = service.get_state(run_id=run_id, workspace_id=workspace_id)
            if latest.status.value in {"succeeded", "failed", "cancelled"}:
                return
            yield ": keepalive\n\n"
            await asyncio.sleep(0.5)

    return StreamingResponse(emit(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.delete("/runs/{run_id}")
def cancel_run(run_id: str, user: User = Depends(current_user), workspace_id: str = Depends(current_workspace_id)):
    service = _service_for_snapshot({})
    _owned_state(service, run_id=run_id, workspace_id=workspace_id, user_id=user.id)
    return service.cancel(run_id=run_id, workspace_id=workspace_id, user_id=user.id)
