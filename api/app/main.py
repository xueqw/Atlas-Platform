import json
from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload
from .database import Base, SessionLocal, engine, get_db
from .model_gateway import stream_model
from .models import Conversation, Message
from .schemas import ChatRequest, ConversationCreate, ConversationDetail, ConversationOut

app = FastAPI(title="Atlas Agent Platform API", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])


@app.on_event("startup")
def startup():
    Base.metadata.create_all(engine)


@app.get("/api/health")
def health():
    return {"status": "ok", "service": "atlas-api"}


@app.get("/api/conversations", response_model=list[ConversationOut])
def list_conversations(db: Session = Depends(get_db)):
    return db.scalars(select(Conversation).order_by(Conversation.updated_at.desc())).all()


@app.post("/api/conversations", response_model=ConversationOut, status_code=201)
def create_conversation(payload: ConversationCreate, db: Session = Depends(get_db)):
    item = Conversation(title=payload.title.strip() or "新任务")
    db.add(item); db.commit(); db.refresh(item)
    return item


@app.get("/api/conversations/{conversation_id}", response_model=ConversationDetail)
def get_conversation(conversation_id: str, db: Session = Depends(get_db)):
    item = db.scalar(select(Conversation).options(selectinload(Conversation.messages)).where(Conversation.id == conversation_id))
    if not item:
        raise HTTPException(404, "任务不存在")
    return item


@app.delete("/api/conversations/{conversation_id}", status_code=204)
def delete_conversation(conversation_id: str, db: Session = Depends(get_db)):
    item = db.get(Conversation, conversation_id)
    if not item:
        raise HTTPException(404, "任务不存在")
    db.delete(item); db.commit()


@app.post("/api/conversations/{conversation_id}/messages/stream")
async def send_message(conversation_id: str, payload: ChatRequest, db: Session = Depends(get_db)):
    conversation = db.scalar(select(Conversation).options(selectinload(Conversation.messages)).where(Conversation.id == conversation_id))
    if not conversation:
        raise HTTPException(404, "任务不存在")
    user_message = Message(conversation_id=conversation_id, role="user", content=payload.content)
    if not conversation.messages:
        conversation.title = payload.content[:28]
    db.add(user_message); db.commit()
    history = [{"role": m.role, "content": m.content} for m in conversation.messages] + [{"role": "user", "content": payload.content}]

    async def events():
        parts: list[str] = []
        try:
            async for token in stream_model(history):
                parts.append(token)
                yield f"data: {json.dumps({'type': 'token', 'content': token}, ensure_ascii=False)}\n\n"
            answer = "".join(parts)
            with SessionLocal() as write_db:
                write_db.add(Message(conversation_id=conversation_id, role="assistant", content=answer))
                saved = write_db.get(Conversation, conversation_id)
                if saved:
                    from datetime import datetime, timezone
                    saved.updated_at = datetime.now(timezone.utc)
                write_db.commit()
            yield f"data: {json.dumps({'type': 'done'}, ensure_ascii=False)}\n\n"
        except Exception as exc:
            yield f"data: {json.dumps({'type': 'error', 'message': str(exc)}, ensure_ascii=False)}\n\n"

    return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
