from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session

from .auth import current_user, current_workspace_id
from .config import settings
from .database import get_db
from .model_gateway import embed_query
from .models import Agent, AgentMemory, User
from .memory_ledger import (
    MemoryScope,
    MemoryRetrievalPolicy,
    WorkingStateConflict,
    checkpoint_state,
    explain_memory_retrieval,
    load_profile_card,
    load_checkpoint,
    query_active_facts,
    record_fact,
    tombstone_fact,
    tombstone_profile_card,
    update_profile_card,
)
from .governance_models import MemoryCheckpoint, SemanticFact
from .state_store import state_store


router = APIRouter(prefix="/api/agents/{agent_id}/memory", tags=["agent-memory"])


class ShortMemoryRequest(BaseModel):
    conversation_id: str = Field(min_length=1, max_length=64)
    messages: list[dict] = Field(max_length=50)
    expected_version: int | None = Field(default=None, ge=0)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=160)


class ShortMemoryDeleteRequest(BaseModel):
    conversation_id: str = Field(min_length=1, max_length=64)
    expected_version: int | None = Field(default=None, ge=0)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=160)


class LongMemoryRequest(BaseModel):
    content: str = Field(min_length=1, max_length=4000)
    category: str = Field(default="preference", max_length=40)
    ttl_days: int | None = Field(default=None, ge=1, le=3650)


class GovernedFactRequest(BaseModel):
    subject: str = Field(min_length=1, max_length=240)
    predicate: str = Field(min_length=1, max_length=160)
    object_value: str = Field(min_length=1, max_length=4000)
    idempotency_key: str = Field(min_length=1, max_length=160)
    evidence: str = Field(default="", max_length=4000)
    confidence: float = Field(default=0.5, ge=0, le=1)
    sensitivity: Literal["public", "internal", "confidential", "restricted"] = "internal"
    consent_status: Literal["unknown", "granted", "denied", "revoked"] = "unknown"
    is_sensitive: bool = False
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    expires_at: datetime | None = None
    supersedes_fact_id: str | None = Field(default=None, max_length=36)


class GovernedFactDeleteRequest(BaseModel):
    idempotency_key: str = Field(min_length=1, max_length=160)
    reason: str = Field(default="", max_length=1000)


class ProfileCardRequest(BaseModel):
    fields: dict[str, Any]
    expected_version: int = Field(ge=0)
    idempotency_key: str = Field(min_length=1, max_length=160)


class ProfileCardDeleteRequest(BaseModel):
    expected_version: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=160)


def _agent(agent_id: str, workspace_id: str, db: Session) -> Agent:
    agent = db.scalar(select(Agent).where(Agent.id == agent_id, Agent.workspace_id == workspace_id))
    if not agent:
        raise HTTPException(404, "Agent 不存在")
    return agent


def _short_key(workspace_id: str, user_id: str, agent_id: str, conversation_id: str) -> str:
    return f"{workspace_id}:{user_id}:{agent_id}:{conversation_id}"


def _scope(workspace_id: str, user_id: str, agent_id: str) -> MemoryScope:
    return MemoryScope(workspace_id=workspace_id, user_id=user_id, agent_id=agent_id)


def _session_idempotency_key(
    *, workspace_id: str, user_id: str, agent_id: str, conversation_id: str,
    expected_version: int, snapshot: dict[str, Any], operation: str,
) -> str:
    canonical = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]
    return ":".join((operation, workspace_id, user_id, agent_id, conversation_id,
                     str(expected_version), digest))[:160]


def _write_session_checkpoint(
    db: Session, *, scope: MemoryScope, conversation_id: str,
    snapshot: dict[str, Any], expected_version: int | None,
    idempotency_key: str | None, operation: str,
):
    current = load_checkpoint(
        db, scope=scope, state_kind="session", state_key=conversation_id
    )
    version = current.version if expected_version is None and current is not None else (expected_version or 0)
    key = idempotency_key or _session_idempotency_key(
        workspace_id=scope.workspace_id, user_id=scope.user_id,
        agent_id=scope.agent_id, conversation_id=conversation_id,
        expected_version=version, snapshot=snapshot, operation=operation,
    )
    checkpoint, created = checkpoint_state(
        db, scope=scope, state_kind="session", state_key=conversation_id,
        snapshot=snapshot, expected_version=version, idempotency_key=key,
    )
    db.commit()
    return checkpoint, created


@router.put("/short")
def put_short_memory(
    agent_id: str, payload: ShortMemoryRequest, user: User = Depends(current_user),
    workspace_id: str = Depends(current_workspace_id), db: Session = Depends(get_db),
):
    _agent(agent_id, workspace_id, db)
    scope = _scope(workspace_id, user.id, agent_id)
    try:
        checkpoint, created = _write_session_checkpoint(
            db, scope=scope, conversation_id=payload.conversation_id,
            snapshot={"messages": payload.messages, "deleted": False},
            expected_version=payload.expected_version,
            idempotency_key=payload.idempotency_key, operation="session-put",
        )
    except WorkingStateConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    hot_cached = True
    try:
        state_store.set(
            "short-memory", _short_key(workspace_id, user.id, agent_id, payload.conversation_id),
            payload.messages, settings.short_memory_ttl_seconds,
        )
    except RuntimeError:
        # PostgreSQL is authoritative; Redis loss must not discard the write.
        hot_cached = False
    return {"ok": True, "version": checkpoint.version, "created": created,
            "hot_cached": hot_cached, "ttl_seconds": settings.short_memory_ttl_seconds}


@router.get("/short")
def get_short_memory(
    agent_id: str, conversation_id: str = Query(min_length=1, max_length=64),
    user: User = Depends(current_user), workspace_id: str = Depends(current_workspace_id),
    db: Session = Depends(get_db),
):
    _agent(agent_id, workspace_id, db)
    hot_key = _short_key(workspace_id, user.id, agent_id, conversation_id)
    try:
        cached = state_store.get("short-memory", hot_key)
    except RuntimeError:
        cached = None
    if cached is not None:
        return {"messages": cached, "source": "redis"}
    checkpoint = load_checkpoint(
        db, scope=_scope(workspace_id, user.id, agent_id),
        state_kind="session", state_key=conversation_id,
    )
    snapshot = dict(checkpoint.snapshot) if checkpoint is not None else {}
    messages = [] if snapshot.get("deleted") else list(snapshot.get("messages") or [])
    if checkpoint is not None and not snapshot.get("deleted"):
        try:
            state_store.set("short-memory", hot_key, messages, settings.short_memory_ttl_seconds)
        except RuntimeError:
            pass
    return {"messages": messages, "source": "postgresql" if checkpoint is not None else "empty",
            "version": checkpoint.version if checkpoint is not None else 0}


@router.delete("/short")
def delete_short_memory(
    agent_id: str, payload: ShortMemoryDeleteRequest,
    user: User = Depends(current_user), workspace_id: str = Depends(current_workspace_id),
    db: Session = Depends(get_db),
):
    _agent(agent_id, workspace_id, db)
    try:
        checkpoint, created = _write_session_checkpoint(
            db, scope=_scope(workspace_id, user.id, agent_id),
            conversation_id=payload.conversation_id,
            snapshot={"messages": [], "deleted": True},
            expected_version=payload.expected_version,
            idempotency_key=payload.idempotency_key, operation="session-delete",
        )
    except WorkingStateConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    try:
        state_store.delete(
            "short-memory", _short_key(workspace_id, user.id, agent_id, payload.conversation_id)
        )
    except RuntimeError:
        pass
    return {"ok": True, "version": checkpoint.version, "deleted": created}


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


@router.post("/facts", status_code=201)
async def create_governed_fact(
    agent_id: str, payload: GovernedFactRequest, user: User = Depends(current_user),
    workspace_id: str = Depends(current_workspace_id), db: Session = Depends(get_db),
):
    _agent(agent_id, workspace_id, db)
    embedding = await embed_query(f"{payload.subject} {payload.predicate} {payload.object_value}")
    try:
        fact, created = record_fact(
            db, scope=_scope(workspace_id, user.id, agent_id), subject=payload.subject,
            predicate=payload.predicate, object_value=payload.object_value,
            idempotency_key=payload.idempotency_key, evidence=payload.evidence,
            confidence=payload.confidence, sensitivity=payload.sensitivity,
            consent_status=payload.consent_status, valid_from=payload.valid_from,
            valid_to=payload.valid_to, expires_at=payload.expires_at,
            supersedes_fact_id=payload.supersedes_fact_id, is_sensitive=payload.is_sensitive,
            embedding=embedding, embedding_model=settings.embedding_model if embedding else "",
        )
    except LookupError as exc:
        raise HTTPException(404, "被修正的记忆事实不存在") from exc
    db.commit()
    return {"id": fact.fact_id, "created": created, "transaction_from": fact.transaction_from}


@router.get("/facts")
async def list_governed_facts(
    agent_id: str,
    query: str = Query(default="", max_length=1000),
    allowed_sensitivity: list[Literal["public", "internal", "confidential", "restricted"]] = Query(default=["public", "internal"]),
    include_sensitive: bool = Query(default=False),
    limit: int = Query(default=20, ge=1, le=100),
    minimum_similarity: float = Query(default=0.0, ge=0, le=1),
    user: User = Depends(current_user), workspace_id: str = Depends(current_workspace_id),
    db: Session = Depends(get_db),
):
    _agent(agent_id, workspace_id, db)
    query_vector = await embed_query(query) if query else None
    return explain_memory_retrieval(
        db,
        scope=_scope(workspace_id, user.id, agent_id),
        policy=MemoryRetrievalPolicy(
            allowed_sensitivities=frozenset(allowed_sensitivity),
            allow_sensitive=include_sensitive,
        ),
        query_vector=query_vector,
        limit=limit,
        minimum_similarity=minimum_similarity,
    )


@router.get("/profile")
def get_profile_card(
    agent_id: str, user: User = Depends(current_user), workspace_id: str = Depends(current_workspace_id),
    db: Session = Depends(get_db),
):
    _agent(agent_id, workspace_id, db)
    card = load_profile_card(db, scope=_scope(workspace_id, user.id, agent_id))
    if card is None or card.tombstoned_at is not None:
        return {"version": 0, "fields": {}}
    return {"version": card.version, "fields": card.fields, "updated_at": card.updated_at}


@router.put("/profile")
def put_profile_card(
    agent_id: str, payload: ProfileCardRequest, user: User = Depends(current_user),
    workspace_id: str = Depends(current_workspace_id), db: Session = Depends(get_db),
):
    _agent(agent_id, workspace_id, db)
    try:
        card, created = update_profile_card(
            db,
            scope=_scope(workspace_id, user.id, agent_id),
            fields=payload.fields,
            expected_version=payload.expected_version,
            idempotency_key=payload.idempotency_key,
        )
    except WorkingStateConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    db.commit()
    return {"version": card.version, "fields": card.fields, "created": created}


@router.delete("/profile")
def delete_profile_card(
    agent_id: str, payload: ProfileCardDeleteRequest, user: User = Depends(current_user),
    workspace_id: str = Depends(current_workspace_id), db: Session = Depends(get_db),
):
    _agent(agent_id, workspace_id, db)
    try:
        card, created = tombstone_profile_card(
            db,
            scope=_scope(workspace_id, user.id, agent_id),
            expected_version=payload.expected_version,
            idempotency_key=payload.idempotency_key,
        )
    except LookupError as exc:
        raise HTTPException(404, "用户档案卡不存在") from exc
    except WorkingStateConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    db.commit()
    return {"version": card.version, "tombstoned": created}


@router.delete("/facts/{fact_id}")
def delete_governed_fact(
    agent_id: str, fact_id: str, payload: GovernedFactDeleteRequest, user: User = Depends(current_user),
    workspace_id: str = Depends(current_workspace_id), db: Session = Depends(get_db),
):
    _agent(agent_id, workspace_id, db)
    try:
        fact, created = tombstone_fact(
            db, scope=_scope(workspace_id, user.id, agent_id), fact_id=fact_id,
            idempotency_key=payload.idempotency_key, reason=payload.reason,
        )
    except LookupError as exc:
        raise HTTPException(404, "记忆事实不存在") from exc
    db.commit()
    return {"id": fact.fact_id, "tombstoned": created}


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
    scope = _scope(workspace_id, user.id, agent_id)
    for fact in query_active_facts(db, scope=scope):
        tombstone_fact(
            db,
            scope=scope,
            fact_id=fact.fact_id,
            idempotency_key=f"bulk-delete:fact:{fact.fact_id}:{datetime.now(timezone.utc).isoformat()}",
            reason="user requested delete all memory",
        )
    profile = load_profile_card(db, scope=scope)
    if profile is not None and profile.tombstoned_at is None:
        tombstone_profile_card(
            db,
            scope=scope,
            expected_version=profile.version,
            idempotency_key=f"bulk-delete:profile:{profile.card_id}:{datetime.now(timezone.utc).isoformat()}",
        )
    session_query = select(MemoryCheckpoint).where(
        MemoryCheckpoint.workspace_id == workspace_id,
        MemoryCheckpoint.user_id == user.id,
        MemoryCheckpoint.agent_id == agent_id,
        MemoryCheckpoint.scope_key == "-:-",
        MemoryCheckpoint.state_kind == "session",
    )
    if conversation_id:
        session_query = session_query.where(MemoryCheckpoint.state_key == conversation_id)
    for checkpoint in db.scalars(session_query).all():
        if checkpoint.snapshot.get("deleted"):
            continue
        checkpoint_state(
            db, scope=scope, state_kind="session", state_key=checkpoint.state_key,
            snapshot={"messages": [], "deleted": True},
            expected_version=checkpoint.version,
            idempotency_key=(
                f"bulk-delete:session:{checkpoint.checkpoint_id}:"
                f"{datetime.now(timezone.utc).isoformat()}"
            )[:160],
        )
    db.commit()
    try:
        if conversation_id:
            state_store.delete("short-memory", _short_key(workspace_id, user.id, agent_id, conversation_id))
        else:
            state_store.delete_prefix("short-memory", f"{workspace_id}:{user.id}:{agent_id}:")
    except RuntimeError:
        pass
