from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select
from typing import List

from app.core.database import get_session
from app.models.db import Conversation, Message, Agent
from app.models.schemas import ConversationResponse, MessageResponse

router = APIRouter(prefix="/conversations", tags=["conversations"])


@router.get("", response_model=List[ConversationResponse])
def list_conversations(session: Session = Depends(get_session)):
    rows = session.exec(
        select(Conversation).order_by(Conversation.updated_at.desc()).limit(50)
    ).all()

    results = []
    for c in rows:
        last_msg = session.exec(
            select(Message)
            .where(Message.conversation_id == c.id)
            .order_by(Message.created_at.desc())
            .limit(1)
        ).first()
        results.append(ConversationResponse(
            id=c.id,
            agent_id=c.agent_id,
            title=c.title,
            created_at=c.created_at.isoformat(),
            updated_at=c.updated_at.isoformat(),
            last_message_preview=last_msg.content[:80] if last_msg else "",
        ))
    return results


@router.get("/{conversation_id}/messages", response_model=List[MessageResponse])
def get_messages(conversation_id: int, session: Session = Depends(get_session)):
    conv = session.get(Conversation, conversation_id)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    msgs = session.exec(
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.created_at)
    ).all()
    return [
        MessageResponse(
            id=m.id,
            conversation_id=m.conversation_id,
            role=m.role,
            content=m.content,
            created_at=m.created_at.isoformat(),
        )
        for m in msgs
    ]


@router.delete("/{conversation_id}", response_model=dict)
def delete_conversation(conversation_id: int, session: Session = Depends(get_session)):
    conv = session.get(Conversation, conversation_id)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    session.exec(select(Message).where(Message.conversation_id == conversation_id))
    session.delete(conv)
    session.commit()
    return {"message": "Conversation deleted"}