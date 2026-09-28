import json
from fastapi import Depends, FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload
from .database import SessionLocal, ensure_schema, get_db
from .knowledge import extract_pages, search_chunks, split_pages
from .model_gateway import embed_query, embed_texts, list_providers, stream_agent, test_model
from .connectors import github_mcp
from . import tools as agent_tools
from .models import Agent, Conversation, Document, DocumentChunk, KnowledgeBase, Message
from .schemas import AgentCreate, AgentOut, AgentUpdate, ChatRequest, ConversationCreate, ConversationDetail, ConversationOut, GithubConfigRequest, KnowledgeBaseCreate, KnowledgeBaseOut, ModelTestRequest
from .apps import router as apps_router
from .version import __version__

app = FastAPI(title="Atlas Agent Platform API", version=__version__)
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

app.include_router(apps_router)

@app.on_event("startup")
def startup():
    ensure_schema()


@app.get("/api/health")
def health():
    return {"status": "ok", "service": "atlas-api", "version": __version__}


@app.get("/api/conversations", response_model=list[ConversationOut])
def list_conversations(db: Session = Depends(get_db)):
    return db.scalars(select(Conversation).order_by(Conversation.updated_at.desc())).all()


@app.post("/api/conversations", response_model=ConversationOut, status_code=201)
def create_conversation(payload: ConversationCreate, db: Session = Depends(get_db)):
    item = Conversation(title=payload.title.strip() or "New task")
    db.add(item); db.commit(); db.refresh(item)
    return item


@app.get("/api/conversations/{conversation_id}", response_model=ConversationDetail)
def get_conversation(conversation_id: str, db: Session = Depends(get_db)):
    item = db.scalar(select(Conversation).options(selectinload(Conversation.messages)).where(Conversation.id == conversation_id))
    if not item:
        raise HTTPException(404, "Task not found")
    return item


@app.delete("/api/conversations/{conversation_id}", status_code=204)
def delete_conversation(conversation_id: str, db: Session = Depends(get_db)):
    item = db.get(Conversation, conversation_id)
    if not item:
        raise HTTPException(404, "Task not found")
    db.delete(item); db.commit()


@app.get("/api/models")
def get_models():
    return list_providers()


@app.post("/api/models/test")
async def post_model_test(payload: ModelTestRequest):
    return await test_model(payload.model)


@app.get("/api/agents", response_model=list[AgentOut])
def list_agents(db: Session = Depends(get_db)):
    return db.scalars(select(Agent).order_by(Agent.updated_at.desc())).all()


@app.post("/api/agents", response_model=AgentOut, status_code=201)
def create_agent(payload: AgentCreate, db: Session = Depends(get_db)):
    item = Agent(**payload.model_dump())
    db.add(item); db.commit(); db.refresh(item)
    return item


@app.put("/api/agents/{agent_id}", response_model=AgentOut)
def update_agent(agent_id: str, payload: AgentUpdate, db: Session = Depends(get_db)):
    item = db.get(Agent, agent_id)
    if not item:
        raise HTTPException(404, "Agent not found")
    for key, value in payload.model_dump().items():
        setattr(item, key, value)
    db.commit(); db.refresh(item)
    return item


@app.delete("/api/agents/{agent_id}", status_code=204)
def delete_agent(agent_id: str, db: Session = Depends(get_db)):
    item = db.get(Agent, agent_id)
    if not item:
        raise HTTPException(404, "Agent not found")
    db.delete(item); db.commit()


@app.get("/api/knowledge-bases", response_model=list[KnowledgeBaseOut])
def list_knowledge_bases(db: Session = Depends(get_db)):
    return db.scalars(select(KnowledgeBase).options(selectinload(KnowledgeBase.documents)).order_by(KnowledgeBase.created_at.desc())).all()


@app.post("/api/knowledge-bases", response_model=KnowledgeBaseOut, status_code=201)
def create_knowledge_base(payload: KnowledgeBaseCreate, db: Session = Depends(get_db)):
    item = KnowledgeBase(name=payload.name.strip(), description=payload.description.strip())
    db.add(item); db.commit(); db.refresh(item)
    return item


@app.delete("/api/knowledge-bases/{knowledge_base_id}", status_code=204)
def delete_knowledge_base(knowledge_base_id: str, db: Session = Depends(get_db)):
    item = db.get(KnowledgeBase, knowledge_base_id)
    if not item:
        raise HTTPException(404, "Knowledge base not found")
    db.delete(item); db.commit()


@app.post("/api/knowledge-bases/{knowledge_base_id}/documents", status_code=201)
async def upload_document(knowledge_base_id: str, file: UploadFile = File(...), db: Session = Depends(get_db)):
    knowledge_base = db.get(KnowledgeBase, knowledge_base_id)
    if not knowledge_base:
        raise HTTPException(404, "Knowledge base not found")
    raw = await file.read()
    if len(raw) > 10 * 1024 * 1024:
        raise HTTPException(413, "Files must be 10 MB or smaller")
    try:
        pieces = split_pages(extract_pages(file.filename or "document.txt", raw))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not pieces:
        raise HTTPException(400, "The document does not contain indexable text")
    document = Document(knowledge_base_id=knowledge_base_id, name=file.filename or "Untitled document", content_type=file.content_type or "application/octet-stream", size=len(raw), chunk_count=len(pieces))
    db.add(document); db.flush()
    try:
        vectors = await embed_texts([content for _, content in pieces])
    except Exception:
        vectors = []  # Preserve the document and let retrieval fall back to BM25.
    db.add_all([
        DocumentChunk(
            document_id=document.id, chunk_index=index, page=page, content=content,
            embedding=json.dumps(vectors[index]) if index < len(vectors) else None,
        )
        for index, (page, content) in enumerate(pieces)
    ])
    db.commit(); db.refresh(document)
    return {"id": document.id, "name": document.name, "chunk_count": document.chunk_count, "status": document.status}


@app.post("/api/attachments")
async def extract_attachment(file: UploadFile = File(...)):
    raw = await file.read()
    if len(raw) > 10 * 1024 * 1024:
        raise HTTPException(413, "Files must be 10 MB or smaller")
    try:
        text = "\n".join(content for _, content in extract_pages(file.filename or "attachment.txt", raw)).strip()
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not text:
        raise HTTPException(400, "The file does not contain readable text")
    return {"name": file.filename or "Untitled file", "text": text[:40000]}


@app.delete("/api/documents/{document_id}", status_code=204)
def delete_document(document_id: str, db: Session = Depends(get_db)):
    item = db.get(Document, document_id)
    if not item:
        raise HTTPException(404, "Document not found")
    db.delete(item); db.commit()


_pending_actions: dict[str, dict] = {}  # conversation_id -> pending write action {name, args}
_AFFIRM = {"confirm", "yes", "y", "ok", "approve", "approved"}
_DENY = {"cancel", "no", "n", "deny", "denied", "stop"}


def _affirm(text: str) -> bool:
    return text.strip().lower() in _AFFIRM


def _deny(text: str) -> bool:
    return text.strip().lower() in _DENY


@app.get("/api/connectors")
async def list_connectors():
    return {"connectors": [await github_mcp.get_status()]}


@app.post("/api/connectors/github/config")
async def github_config(payload: GithubConfigRequest, db: Session = Depends(get_db)):
    github_mcp.save_pat(db, payload.pat.strip())
    return await github_mcp.get_status()


@app.delete("/api/connectors/github", status_code=204)
def github_disconnect(db: Session = Depends(get_db)):
    github_mcp.clear(db)


@app.post("/api/conversations/{conversation_id}/messages/stream")
async def send_message(conversation_id: str, payload: ChatRequest, db: Session = Depends(get_db)):
    conversation = db.scalar(select(Conversation).options(selectinload(Conversation.messages)).where(Conversation.id == conversation_id))
    if not conversation:
        raise HTTPException(404, "Task not found")

    async def execute_tool(name: str, arguments: str) -> str:
        if name not in agent_tools.TOOLS and github_mcp.is_configured():
            return await github_mcp.call_tool(name, arguments)
        with SessionLocal() as tool_db:
            return await agent_tools.dispatch(tool_db, name, arguments)

    def save_assistant(text: str, sources_json: str = "[]") -> None:
        with SessionLocal() as wdb:
            wdb.add(Message(conversation_id=conversation_id, role="assistant", content=text, sources=sources_json))
            saved = wdb.get(Conversation, conversation_id)
            if saved:
                from datetime import datetime, timezone
                saved.updated_at = datetime.now(timezone.utc)
            wdb.commit()

    # Handle approval or cancellation of a pending write action without invoking the model.
    pending = _pending_actions.pop(conversation_id, None)
    if pending and (_affirm(payload.content) or _deny(payload.content)):
        approved = _affirm(payload.content)
        db.add(Message(conversation_id=conversation_id, role="user", content=payload.content)); db.commit()

        async def confirm_events():
            if approved:
                result = await execute_tool(pending["name"], json.dumps(pending["args"], ensure_ascii=False))
            else:
                result = "Canceled. No action was taken."
            save_assistant(result)
            for ch in result:
                yield f"data: {json.dumps({'type': 'token', 'content': ch}, ensure_ascii=False)}\n\n"
            yield f"data: {json.dumps({'type': 'done'}, ensure_ascii=False)}\n\n"

        return StreamingResponse(confirm_events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    agent = db.get(Agent, payload.agent_id) if payload.agent_id else None
    # Respect the explicit knowledge-base selection made by the client.
    knowledge_base_id = payload.knowledge_base_id
    query_vector = await embed_query(payload.content) if knowledge_base_id else None
    sources = search_chunks(db, knowledge_base_id, payload.content, query_vector) if knowledge_base_id else []
    user_message = Message(conversation_id=conversation_id, role="user", content=payload.content)
    if not conversation.messages:
        conversation.title = payload.content[:28]
    db.add(user_message); db.commit()
    history = [{"role": message.role, "content": message.content} for message in conversation.messages]
    if agent:
        history.append({"role": "system", "content": f"Agent identity and rules:\n{agent.system_prompt}"})
    if sources:
        context = "\n\n".join(f"[{item['document']}, page {item['page']}]\n{item['content']}" for item in sources)
        history.append({"role": "system", "content": "Knowledge base sources (answer only from these sources):\n" + context})
    if payload.attachment_text:
        history.append({"role": "system", "content": f"User-uploaded file '{payload.attachment_name or 'attachment'}':\n{payload.attachment_text[:8000]}"})
    history.append({"role": "user", "content": payload.content})
    model = payload.model

    # Combine static connector tools with tools discovered through GitHub MCP.
    tool_specs = agent_tools.specs(payload.connectors)
    mcp_write_names: set[str] = set()
    if "github" in payload.connectors and github_mcp.is_configured():
        try:
            gh_specs, mcp_write_names = await github_mcp.tool_specs()
            tool_specs = tool_specs + gh_specs
        except Exception:
            pass  # Keep chat available if GitHub MCP discovery fails.

    def needs_confirm(name: str) -> bool:
        return agent_tools.is_write(name) or name in mcp_write_names

    async def events():
        parts: list[str] = []
        public_sources = [{key: item[key] for key in ("document", "page", "quote", "score")} for item in sources]
        try:
            if public_sources:
                yield f"data: {json.dumps({'type': 'sources', 'sources': public_sources}, ensure_ascii=False)}\n\n"
            async for ev in stream_agent(history, model, tool_specs, execute_tool, needs_confirm=needs_confirm):
                if ev["type"] == "confirm_required":
                    args = json.loads(ev["args"] or "{}") if isinstance(ev["args"], str) else ev["args"]
                    _pending_actions[conversation_id] = {"name": ev["name"], "args": args}
                    prompt = agent_tools.describe_call(ev["name"], args) + "\n\nReply `confirm` to continue or `cancel` to stop."
                    parts.append(prompt)
                    yield f"data: {json.dumps({'type': 'token', 'content': prompt}, ensure_ascii=False)}\n\n"
                    continue
                if ev["type"] == "token":
                    parts.append(ev["content"])
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
            save_assistant("".join(parts), json.dumps(public_sources, ensure_ascii=False))
            yield f"data: {json.dumps({'type': 'done'}, ensure_ascii=False)}\n\n"
        except Exception as exc:
            yield f"data: {json.dumps({'type': 'error', 'message': str(exc)}, ensure_ascii=False)}\n\n"

    return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
