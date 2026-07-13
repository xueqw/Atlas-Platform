from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session

from .auth import current_user, current_workspace_id
from .config import settings
from .database import get_db
from .model_gateway import embed_query
from .models import Agent, AgentMemory, User
from .state_store import state_store


router = APIRouter(prefix="/api/agents/{agent_id}/memory", tags=["agent-memory"])


class ShortMemoryRequest(BaseModel):
    conversation_id: str = Field(min_length=1, max_length=64)
    messages: list[dict] = Field(max_length=50)


class LongMemoryRequest(BaseModel):
    content: str = Field(min_length=1, max_length=4000)
    category: str = Field(default="preference", max_length=40)
    ttl_days: int | None = Field(default=None, ge=1, le=3650)


def _agent(agent_id: str, workspace_id: str, db: Session) -> Agent:
    agent = db.scalar(select(Agent).where(Agent.id == agent_id, Agent.workspace_id == workspace_id))
    if not agent:
        raise HTTPException(404, "Agent 不存在")
    return agent


def _short_key(workspace_id: str, user_id: str, agent_id: str, conversation_id: str) -> str:
    return f"{workspace_id}:{user_id}:{agent_id}:{conversation_id}"


@router.put("/short")
def put_short_memory(
    agent_id: str, payload: ShortMemoryRequest, user: User = Depends(current_user),
    workspace_id: str = Depends(current_workspace_id), db: Session = Depends(get_db),
):
    _agent(agent_id, workspace_id, db)
    state_store.set(
        "short-memory", _short_key(workspace_id, user.id, agent_id, payload.conversation_id),
        payload.messages, settings.short_memory_ttl_seconds,
    )
    return {"ok": True, "ttl_seconds": settings.short_memory_ttl_seconds}


@router.get("/short")
def get_short_memory(
    agent_id: str, conversation_id: str = Query(min_length=1, max_length=64),
    user: User = Depends(current_user), workspace_id: str = Depends(current_workspace_id),
    db: Session = Depends(get_db),
):
    _agent(agent_id, workspace_id, db)
    return {"messages": state_store.get("short-memory", _short_key(workspace_id, user.id, agent_id, conversation_id)) or []}


@router.post("/long", status_code=201)
async def create_long_memory(
    agent_id: str, payload: LongMemoryRequest, user: User = Depends(current_user),
    workspace_id: str = Depends(current_workspace_id), db: Session = Depends(get_db),
):
    _agent(agent_id, workspace_id, db)
    ttl = payload.ttl_days or settings.long_memory_default_ttl_days
    expires_at = datetime.now(timezone.utc) + timedelta(days=ttl) if ttl else None
    embedding = await embed_query(payload.content)
    stored_embedding = json.dumps(embedding) if embedding and db.bind and db.bind.dialect.name == "sqlite" else embedding
    item = AgentMemory(
        workspace_id=workspace_id, user_id=user.id, agent_id=agent_id,
        category=payload.category, content=payload.content, embedding=stored_embedding, expires_at=expires_at,
    )
    db.add(item); db.commit(); db.refresh(item)
    return {"id": item.id, "content": item.content, "category": item.category, "expires_at": item.expires_at}


@router.get("/long")
def list_long_memories(
    agent_id: str, user: User = Depends(current_user), workspace_id: str = Depends(current_workspace_id),
    db: Session = Depends(get_db),
):
    _agent(agent_id, workspace_id, db)
    now = datetime.now(timezone.utc)
    rows = db.scalars(select(AgentMemory).where(
        AgentMemory.workspace_id == workspace_id, AgentMemory.user_id == user.id,
        AgentMemory.agent_id == agent_id,
        or_(AgentMemory.expires_at.is_(None), AgentMemory.expires_at > now),
    ).order_by(AgentMemory.updated_at.desc())).all()
    return [{"id": row.id, "content": row.content, "category": row.category, "expires_at": row.expires_at} for row in rows]


@router.delete("/long/{memory_id}", status_code=204)
def delete_long_memory(
    agent_id: str, memory_id: str, user: User = Depends(current_user),
    workspace_id: str = Depends(current_workspace_id), db: Session = Depends(get_db),
):
    _agent(agent_id, workspace_id, db)
    item = db.scalar(select(AgentMemory).where(
        AgentMemory.id == memory_id, AgentMemory.agent_id == agent_id,
        AgentMemory.workspace_id == workspace_id, AgentMemory.user_id == user.id,
    ))
    if not item:
        raise HTTPException(404, "记忆不存在")
    db.delete(item); db.commit()


@router.delete("", status_code=204)
def delete_all_memory(
    agent_id: str, conversation_id: str | None = None, user: User = Depends(current_user),
    workspace_id: str = Depends(current_workspace_id), db: Session = Depends(get_db),
):
    _agent(agent_id, workspace_id, db)
    db.execute(delete(AgentMemory).where(
        AgentMemory.agent_id == agent_id, AgentMemory.workspace_id == workspace_id, AgentMemory.user_id == user.id,
    ))
    db.commit()
    if conversation_id:
        state_store.delete("short-memory", _short_key(workspace_id, user.id, agent_id, conversation_id))
    else:
        state_store.delete_prefix("short-memory", f"{workspace_id}:{user.id}:{agent_id}:")
