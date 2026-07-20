import json
import asyncio
import secrets
import re
from fastapi import Cookie, Depends, FastAPI, File, Form, HTTPException, Query, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from sqlalchemy import select, text
from sqlalchemy.orm import Session, selectinload
from . import auth
from . import strategy
from . import workflow as wf
from .tool_strategy import build_tool_policy
from .auth import current_user, current_workspace_id
from .config import settings
from .database import SessionLocal, ensure_schema, get_db
from .knowledge import extract_pages, search_chunks, split_pages
from .model_gateway import configure_provider, embed_query, embed_texts, list_providers, stream_agent, stream_model, test_model
from .connectors import feishu, github_mcp, remote_mcp
from . import tools as agent_tools
from .apps import agents_router, create_version, execute_agent_runtime, next_version_no, snapshot_prompt_agent, router as apps_router
from .api_invoke import invoke_router
from .evaluation import router as evaluation_router
from .memory import router as memory_router
from .runtime_api import router as runtime_router
from .multi_agent_api import router as multi_agent_router
from .object_storage import knowledge_object_key, object_storage
from .state_store import state_store
from .code_runner import runner_health
from .deploy_policy import apply_resource_permissions, check_visibility, parse_deploy_config
from .models import Agent, Conversation, Document, DocumentChunk, KnowledgeBase, Membership, Message, Skill, User, WorkflowRun
from .schemas import AccountOut, AgentCreate, AgentOut, AgentUpdate, ChatRequest, ConversationCreate, ConversationDetail, ConversationOut, FeishuConfigRequest, GithubConfigRequest, KnowledgeBaseCreate, KnowledgeBaseOut, LoginRequest, McpKeyRequest, MeOut, ModelProviderConfigRequest, ModelTestRequest, SkillCreate, SkillOut, SkillUpdate, WorkflowRunOut, WorkspaceMemberOut

app = FastAPI(title="Atlas Agent Platform API", version="0.3.0")
cors_origins = [origin.strip() for origin in settings.cors_origins.split(",") if origin.strip()]
app.add_middleware(CORSMiddleware, allow_origins=cors_origins, allow_credentials=True, allow_methods=["*"], allow_headers=["*"])
app.include_router(apps_router)
app.include_router(agents_router)
app.include_router(invoke_router)
app.include_router(evaluation_router)
app.include_router(memory_router)
app.include_router(runtime_router)
app.include_router(multi_agent_router)

@app.on_event("startup")
def startup():
    if settings.environment == "production" and not settings.secret_encryption_key:
        raise RuntimeError("SECRET_ENCRYPTION_KEY_FILE is required in production")
    if (
        settings.environment == "production"
        and settings.object_storage_required
        and settings.object_storage_backend.strip().lower() != "minio"
    ):
        raise RuntimeError("Production object storage requires OBJECT_STORAGE_BACKEND=minio")
    ensure_schema()
    if settings.object_storage_required and not object_storage.ping():
        raise RuntimeError("Required object storage is unavailable")
    if settings.redis_required and not state_store.ping():
        raise RuntimeError("Required Redis is unavailable")


@app.get("/api/health")
def health():
    dependencies = {"database": False, "redis": state_store.ping(), "object_storage": object_storage.ping(), "code_runner": runner_health()}
    try:
        with SessionLocal() as db:
            db.execute(text("SELECT 1"))
        dependencies["database"] = True
    except Exception:
        pass
    required = [dependencies["database"]]
    if settings.redis_required:
        required.append(dependencies["redis"])
    if settings.object_storage_required:
        required.append(dependencies["object_storage"])
    if settings.code_runner_required:
        required.append(dependencies["code_runner"])
    return {"status": "ok" if all(required) else "degraded", "service": "atlas-api", "version": "0.4.0", "dependencies": dependencies}


_URL_RE = re.compile(r"https?://[^\s，。；、）)]+", re.I)
_WEB_FETCH_WORDS = ("抓取", "爬取", "提取页面", "网页", "所有链接", "正文要点", "scrape", "fetch", "links")


def _looks_like_web_fetch(text: str) -> bool:
    lowered = text.lower()
    return bool(_URL_RE.search(text)) and any(word in lowered for word in _WEB_FETCH_WORDS)


# ============ 认证：账号密码 + httpOnly cookie 会话 ============

def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        auth.COOKIE_NAME, token, httponly=True, samesite=settings.cookie_samesite, secure=settings.cookie_secure,
        max_age=settings.session_ttl_hours * 3600, path="/",
    )


@app.get("/api/auth/accounts", response_model=list[AccountOut])
def list_accounts():
    """登录页用：列出预置测试账号（不含密码），前端渲染成点选登录的卡片。"""
    return auth.TEST_ACCOUNTS


@app.post("/api/auth/login", response_model=MeOut)
def login(payload: LoginRequest, response: Response, db: Session = Depends(get_db)):
    user = auth.authenticate(db, payload.username, payload.password)
    if not user:
        raise HTTPException(401, "账号或密码错误")
    token = auth.start_session(db, user)
    _set_session_cookie(response, token)
    role = db.scalar(select(Membership.role).where(
        Membership.user_id == user.id, Membership.workspace_id == auth.get_or_create_default_workspace(db).id))
    return MeOut(user=user, workspace=auth.get_or_create_default_workspace(db), role=role or "member")


@app.post("/api/auth/logout", status_code=204)
def logout(response: Response, atlas_session: str | None = Cookie(default=None), db: Session = Depends(get_db)):
    auth.destroy_session(db, atlas_session)
    response.delete_cookie(auth.COOKIE_NAME, path="/")


@app.get("/api/me", response_model=MeOut)
def me(user: User = Depends(current_user), ws: str = Depends(current_workspace_id), db: Session = Depends(get_db)):
    workspace = db.get(auth.Workspace, ws)
    role = db.scalar(select(Membership.role).where(
        Membership.user_id == user.id, Membership.workspace_id == ws))
    return MeOut(user=user, workspace=workspace, role=role or "member")


@app.get("/api/conversations", response_model=list[ConversationOut])
def list_conversations(ws: str = Depends(current_workspace_id), db: Session = Depends(get_db)):
    return db.scalars(select(Conversation).where(Conversation.workspace_id == ws).order_by(Conversation.updated_at.desc())).all()


@app.post("/api/conversations", response_model=ConversationOut, status_code=201)
def create_conversation(payload: ConversationCreate, ws: str = Depends(current_workspace_id), db: Session = Depends(get_db)):
    item = Conversation(title=payload.title.strip() or "新任务", workspace_id=ws)
    db.add(item); db.commit(); db.refresh(item)
    return item


@app.get("/api/conversations/{conversation_id}", response_model=ConversationDetail)
def get_conversation(conversation_id: str, ws: str = Depends(current_workspace_id), db: Session = Depends(get_db)):
    item = db.scalar(select(Conversation).options(selectinload(Conversation.messages)).where(Conversation.id == conversation_id, Conversation.workspace_id == ws))
    if not item:
        raise HTTPException(404, "任务不存在")
    return item


@app.delete("/api/conversations/{conversation_id}", status_code=204)
def delete_conversation(conversation_id: str, ws: str = Depends(current_workspace_id), db: Session = Depends(get_db)):
    item = db.scalar(select(Conversation).where(Conversation.id == conversation_id, Conversation.workspace_id == ws))
    if not item:
        raise HTTPException(404, "任务不存在")
    db.delete(item); db.commit()


@app.get("/api/models")
def get_models(user: User = Depends(current_user)):
    return list_providers()


@app.post("/api/models/test")
async def post_model_test(payload: ModelTestRequest, user: User = Depends(current_user)):
    return await test_model(payload.model)


@app.post("/api/models/config")
def post_model_config(payload: ModelProviderConfigRequest, user: User = Depends(current_user)):
    try:
        return configure_provider(payload.provider_id, payload.api_key, payload.base_url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _agent_out(agent: Agent, db: Session) -> AgentOut:
    from .apps import agent_out_fields
    return AgentOut.model_validate(agent).model_copy(update=agent_out_fields(agent, db))


@app.get("/api/agents", response_model=list[AgentOut])
def list_agents(ws: str = Depends(current_workspace_id), db: Session = Depends(get_db)):
    items = db.scalars(select(Agent).where(Agent.workspace_id == ws).order_by(Agent.updated_at.desc())).all()
    return [_agent_out(item, db) for item in items]


@app.post("/api/agents", response_model=AgentOut, status_code=201)
def create_agent(payload: AgentCreate, user: User = Depends(current_user), ws: str = Depends(current_workspace_id), db: Session = Depends(get_db)):
    item = Agent(**payload.model_dump(), workspace_id=ws, kind="prompt", created_by=user.id)
    db.add(item); db.flush()
    create_version(db, item, snapshot_prompt_agent(item), label="draft")
    db.commit(); db.refresh(item)
    return _agent_out(item, db)


@app.put("/api/agents/{agent_id}", response_model=AgentOut)
def update_agent(agent_id: str, payload: AgentUpdate, ws: str = Depends(current_workspace_id), db: Session = Depends(get_db)):
    item = db.scalar(select(Agent).where(Agent.id == agent_id, Agent.workspace_id == ws))
    if not item:
        raise HTTPException(404, "智能体不存在")
    for key, value in payload.model_dump().items():
        setattr(item, key, value)
    # 普通保存只改 Agent 行本身（当前工作态），不动任何 AgentVersion 快照——
    # 版本永远不可变，显式点「保存版本」才新增一行（见 /api/agents/{id}/versions）
    db.commit(); db.refresh(item)
    return _agent_out(item, db)


@app.delete("/api/agents/{agent_id}", status_code=204)
def delete_agent(agent_id: str, ws: str = Depends(current_workspace_id), db: Session = Depends(get_db)):
    item = db.scalar(select(Agent).where(Agent.id == agent_id, Agent.workspace_id == ws))
    if not item:
        raise HTTPException(404, "智能体不存在")
    db.delete(item); db.commit()


@app.get("/api/workspace/members", response_model=list[WorkspaceMemberOut])
def list_workspace_members(ws: str = Depends(current_workspace_id), db: Session = Depends(get_db)):
    """落地配置「指定用户」可见范围用：列出当前工作区成员，供选人。"""
    rows = db.execute(
        select(User.id, User.name, User.username).join(Membership, Membership.user_id == User.id)
        .where(Membership.workspace_id == ws)
    ).all()
    return [WorkspaceMemberOut(id=r.id, name=r.name, username=r.username) for r in rows]


@app.get("/api/knowledge-bases", response_model=list[KnowledgeBaseOut])
def list_knowledge_bases(ws: str = Depends(current_workspace_id), db: Session = Depends(get_db)):
    return db.scalars(select(KnowledgeBase).where(KnowledgeBase.workspace_id == ws).options(selectinload(KnowledgeBase.documents)).order_by(KnowledgeBase.created_at.desc())).all()


@app.post("/api/knowledge-bases", response_model=KnowledgeBaseOut, status_code=201)
def create_knowledge_base(payload: KnowledgeBaseCreate, ws: str = Depends(current_workspace_id), db: Session = Depends(get_db)):
    item = KnowledgeBase(name=payload.name.strip(), description=payload.description.strip(), workspace_id=ws)
    db.add(item); db.commit(); db.refresh(item)
    return item


@app.delete("/api/knowledge-bases/{knowledge_base_id}", status_code=204)
def delete_knowledge_base(knowledge_base_id: str, ws: str = Depends(current_workspace_id), db: Session = Depends(get_db)):
    item = db.scalar(select(KnowledgeBase).where(KnowledgeBase.id == knowledge_base_id, KnowledgeBase.workspace_id == ws))
    if not item:
        raise HTTPException(404, "知识库不存在")
    db.delete(item); db.commit()


@app.post("/api/knowledge-bases/{knowledge_base_id}/documents", status_code=201)
async def upload_document(knowledge_base_id: str, file: UploadFile = File(...), ws: str = Depends(current_workspace_id), db: Session = Depends(get_db)):
    knowledge_base = db.scalar(select(KnowledgeBase).where(KnowledgeBase.id == knowledge_base_id, KnowledgeBase.workspace_id == ws))
    if not knowledge_base:
        raise HTTPException(404, "知识库不存在")
    raw = await file.read()
    if len(raw) > 10 * 1024 * 1024:
        raise HTTPException(413, "文件不能超过 10 MB")
    try:
        pieces = split_pages(extract_pages(file.filename or "document.txt", raw))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not pieces:
        raise HTTPException(400, "文档中没有可索引的文本")
    document = Document(knowledge_base_id=knowledge_base_id, name=file.filename or "未命名文档", content_type=file.content_type or "application/octet-stream", size=len(raw), chunk_count=len(pieces))
    db.add(document); db.flush()
    object_key = knowledge_object_key(ws, knowledge_base_id, document.id, document.name)
    try:
        object_storage.put(object_key, raw, document.content_type)
        document.object_key = object_key
    except Exception:
        if settings.object_storage_required:
            db.rollback()
            raise HTTPException(503, "知识原文件存储不可用")
    try:
        vectors = await embed_texts([content for _, content in pieces])
    except Exception:
        vectors = []  # embedding 服务异常时仍入库，检索阶段自动回退关键词
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
async def extract_attachment(file: UploadFile = File(...), user: User = Depends(current_user)):
    raw = await file.read()
    if len(raw) > 10 * 1024 * 1024:
        raise HTTPException(413, "文件不能超过 10 MB")
    try:
        text = "\n".join(content for _, content in extract_pages(file.filename or "attachment.txt", raw)).strip()
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not text:
        raise HTTPException(400, "文件中没有可读取的文本")
    return {"name": file.filename or "未命名文件", "text": text[:40000]}


@app.delete("/api/documents/{document_id}", status_code=204)
def delete_document(document_id: str, ws: str = Depends(current_workspace_id), db: Session = Depends(get_db)):
    item = db.scalar(
        select(Document).join(KnowledgeBase, Document.knowledge_base_id == KnowledgeBase.id)
        .where(Document.id == document_id, KnowledgeBase.workspace_id == ws)
    )
    if not item:
        raise HTTPException(404, "文档不存在")
    object_storage.delete(item.object_key)
    db.delete(item); db.commit()


_AFFIRM = {"确认", "确定", "是", "好", "好的", "发送", "可以", "行", "嗯", "yes", "y", "ok"}
_DENY = {"取消", "不", "不要", "否", "算了", "no", "n"}


def _affirm(text: str) -> bool:
    return text.strip().lower() in _AFFIRM


def _deny(text: str) -> bool:
    return text.strip().lower() in _DENY


def _excel_rows_from_text(text: str) -> list[list[str]]:
    lines = [line.strip(" \t-•|") for line in text.splitlines()]
    rows = [["序号", "内容"]]
    for line in lines:
        if not line:
            continue
        if len(line) < 2:
            continue
        rows.append([str(len(rows)), line])
    return rows if len(rows) > 1 else []


async def _maybe_fill_excel_after_structure_change(pending: dict, result: str) -> str:
    if pending.get("provider") != "excel":
        return result
    if pending.get("name") not in {"create_workbook", "create_worksheet"}:
        return result
    rows = _excel_rows_from_text(pending.get("source_text", ""))
    if not rows:
        return result
    args = dict(pending.get("args") or {})
    filepath = args.get("filepath") or args.get("file_path") or args.get("path") or args.get("filename") or "workbook.xlsx"
    sheet_name = args.get("sheet_name") if pending.get("name") == "create_worksheet" else None
    sheet_name = sheet_name or "Sheet"
    write_args = {
        "filepath": filepath,
        "sheet_name": sheet_name,
        "data": rows,
        "start_cell": "A1",
    }
    write_result = await remote_mcp.call_tool("excel", "write_data_to_excel", json.dumps(write_args, ensure_ascii=False))
    if write_result.lower().startswith("error:"):
        return f"{result}\n{write_result}\n提示：如果这个 Excel 文件正在被 Excel 打开，请先关闭后再重试，打开中的文件可能会被 Windows 锁住。"
    try:
        await remote_mcp.call_tool("excel", "format_range", json.dumps({
            "filepath": filepath,
            "sheet_name": sheet_name,
            "start_cell": "A1",
            "end_cell": "B1",
            "bold": True,
            "bg_color": "4472C4",
            "font_color": "FFFFFF",
            "alignment": "center",
        }, ensure_ascii=False))
    except Exception:
        pass
    return f"{result}\n{write_result}\n已把本次输入内容写入表格：{sheet_name}。"


@app.get("/api/connectors")
async def list_connectors(user: User = Depends(current_user), db: Session = Depends(get_db)):
    async def safe_status(provider: str, name: str, coro, timeout: int = 12):
        try:
            return await asyncio.wait_for(coro, timeout=timeout)
        except Exception as exc:
            return {
                "provider": provider,
                "name": name,
                "description": "",
                "configured": False,
                "connected": False,
                "account_name": f"Connection timed out: {exc}"[:80],
                "actions": [],
            }

    statuses = await asyncio.gather(
        safe_status("feishu", "Feishu", feishu.get_status(db)),
        safe_status("github", "GitHub", github_mcp.get_status()),
        *[
            safe_status(provider, remote_mcp.REGISTRY[provider]["name"], remote_mcp.get_status(provider), timeout=30)
            for provider in remote_mcp.providers()
        ],
    )
    return {"connectors": statuses}


@app.post("/api/connectors/mcp/{provider}/config")
async def mcp_config(provider: str, payload: McpKeyRequest, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """通用远程 MCP 连接器配置 key（高德等）。"""
    if not remote_mcp.is_known(provider):
        raise HTTPException(404, "未知的 MCP 连接器")
    remote_mcp.save_key(db, provider, payload.key.strip())
    return await remote_mcp.get_status(provider)  # 立刻验证：返回连接状态+工具数


@app.delete("/api/connectors/mcp/{provider}", status_code=204)
def mcp_disconnect(provider: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    if not remote_mcp.is_known(provider):
        raise HTTPException(404, "未知的 MCP 连接器")
    remote_mcp.clear(db, provider)


@app.post("/api/connectors/feishu/config")
async def feishu_config(payload: FeishuConfigRequest, user: User = Depends(current_user), db: Session = Depends(get_db)):
    feishu.save_config(db, payload.app_id.strip(), payload.app_secret.strip())
    return await feishu.get_status(db)  # 立刻校验凭证


@app.delete("/api/connectors/feishu/config", status_code=204)
def feishu_config_clear(user: User = Depends(current_user), db: Session = Depends(get_db)):
    feishu.clear_config(db)


@app.post("/api/connectors/github/config")
async def github_config(payload: GithubConfigRequest, user: User = Depends(current_user), db: Session = Depends(get_db)):
    github_mcp.save_pat(db, payload.pat.strip())
    return await github_mcp.get_status()  # 立刻验证：返回连接状态+工具数


@app.delete("/api/connectors/github", status_code=204)
def github_disconnect(user: User = Depends(current_user), db: Session = Depends(get_db)):
    github_mcp.clear(db)


@app.get("/api/connectors/feishu/login")
def feishu_login():
    if not feishu.is_configured():
        raise HTTPException(400, "飞书未配置（缺 App ID / App Secret）")
    state = secrets.token_urlsafe(16)
    state_store.set("oauth", state, True, 600)
    return RedirectResponse(feishu.build_authorize_url(state))


@app.get("/api/connectors/feishu/callback")
async def feishu_callback(code: str = "", state: str = "", db: Session = Depends(get_db)):
    if not code or not state_store.get("oauth", state, consume=True):
        return HTMLResponse("<h3>授权失败：参数缺失或 state 不匹配</h3>", status_code=400)
    try:
        token = await feishu.exchange_code(code)
        info = await feishu.fetch_user_info(token["access_token"])
        name = info.get("name", "飞书用户")
        feishu.save_token(db, token, name, open_id=info.get("open_id", ""))
    except Exception as exc:
        return HTMLResponse(f"<h3>授权失败：{exc}</h3>", status_code=500)
    return HTMLResponse(f"<h3>✅ 已连接飞书：{name}</h3><p>可以关闭此页返回工作台。</p>")


@app.post("/api/conversations/{conversation_id}/messages/stream")
async def send_message(conversation_id: str, payload: ChatRequest, user: User = Depends(current_user), ws: str = Depends(current_workspace_id), db: Session = Depends(get_db)):
    conversation = db.scalar(select(Conversation).options(selectinload(Conversation.messages)).where(Conversation.id == conversation_id, Conversation.workspace_id == ws))
    if not conversation:
        raise HTTPException(404, "任务不存在")

    mcp_owner: dict[str, str] = {}  # 工具名 -> 所属连接器（github / amap / ...），路由 tools/call

    async def execute_tool(name: str, arguments: str, owner: str | None = None) -> str:
        prov = owner or mcp_owner.get(name)
        if prov == "github":
            return await github_mcp.call_tool(name, arguments)
        if prov and remote_mcp.is_known(prov):
            return await remote_mcp.call_tool(prov, name, arguments)
        # 兜底：非静态工具 + GitHub 已配 = GitHub MCP（保确认路径等旧行为）
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

    # === 待确认的写操作：把本条消息当作「确认/取消」处理，不走模型 ===
    pending = state_store.get("pending-action", conversation_id, consume=True)
    if pending and (_affirm(payload.content) or _deny(payload.content)):
        approved = _affirm(payload.content)
        db.add(Message(conversation_id=conversation_id, role="user", content=payload.content)); db.commit()

        async def confirm_events():
            pend_run = pending.get("run_id")
            if approved:
                connector = pending.get("provider") or agent_tools.TOOLS.get(pending["name"], {}).get("connector", "github")
                step_id = None
                if pend_run:
                    idx = wf.next_index(pend_run)
                    step_id = wf.add_step(pend_run, idx, "tool", f"调用工具：{pending['name']}", connector,
                                          input_data={"args": json.dumps(pending["args"], ensure_ascii=False),
                                                      "access": pending.get("access", "write")})
                    yield f"data: {json.dumps({'type': 'step_started', 'step_id': step_id, 'index': idx, 'step_type': 'tool', 'title': '调用工具：' + pending['name']}, ensure_ascii=False)}\n\n"
                result = await execute_tool(pending["name"], json.dumps(pending["args"], ensure_ascii=False), owner=pending.get("provider"))
                result = await _maybe_fill_excel_after_structure_change(pending, result)
                if pend_run and step_id:
                    wf.finish_step(step_id, "succeeded", output={"result": (result or "")[:500]})
                    wf.finish_run(pend_run, "succeeded", output={"answer": result})
                    yield f"data: {json.dumps({'type': 'step_completed', 'step_id': step_id, 'status': 'succeeded'}, ensure_ascii=False)}\n\n"
            else:
                result = "好的，已取消，未执行。"
                if pend_run:
                    wf.finish_run(pend_run, "cancelled")
            save_assistant(result)
            for ch in result:
                yield f"data: {json.dumps({'type': 'token', 'content': ch}, ensure_ascii=False)}\n\n"
            yield f"data: {json.dumps({'type': 'done'}, ensure_ascii=False)}\n\n"

        return StreamingResponse(confirm_events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    agent = db.scalar(select(Agent).where(Agent.id == payload.agent_id, Agent.workspace_id == ws)) if payload.agent_id else None
    if agent and agent.status == "archived":
        raise HTTPException(403, "该智能体已下架")
    deploy_config = parse_deploy_config(agent.deploy_config_json) if agent else None
    if agent and not check_visibility(agent.created_by, deploy_config, user.id):
        raise HTTPException(403, "该智能体不可用")
    # 以前端选择为准：选智能体时前端会自动把它的库填进下拉框；选「不使用知识库」即真的不用，
    # 不再用 agent.knowledge_base_id 偷偷回退（否则「不使用知识库」会被智能体绑定库覆盖）。
    # 跨租户防护：只接受属于当前工作区的知识库，否则忽略，绝不检索别家资料。
    knowledge_base_id = payload.knowledge_base_id
    if knowledge_base_id and not db.scalar(select(KnowledgeBase.id).where(KnowledgeBase.id == knowledge_base_id, KnowledgeBase.workspace_id == ws)):
        knowledge_base_id = None
    user_message = Message(conversation_id=conversation_id, role="user", content=payload.content)
    if not conversation.messages:
        conversation.title = payload.content[:28]
    db.add(user_message); db.commit()
    if agent:
        async def selected_agent_events():
            with SessionLocal() as runtime_db:
                runtime_agent = runtime_db.get(Agent, agent.id)
                if not runtime_agent:
                    yield f"data: {json.dumps({'type': 'error', 'message': 'Agent 不存在'}, ensure_ascii=False)}\n\n"
                    return
                result = await execute_agent_runtime(
                    runtime_agent,
                    payload.content,
                    runtime_db,
                    use_published=runtime_agent.status == "published",
                    source="chat",
                    conversation_id=conversation_id,
                    user_id=user.id,
                )
            answer = result.get("answer") or result.get("error") or "Agent 未返回内容"
            plan = {
                "goal": payload.content,
                "source": "agent_runtime",
                "requires_knowledge": bool(result.get("sources")),
                "requires_tools": bool(result.get("tool_calls")),
                "steps": [
                    {"id": str(index), "type": item.get("type", "respond"), "title": item.get("title", "执行步骤"), "executor": item.get("executor", "runtime")}
                    for index, item in enumerate(result.get("trace") or [])
                ],
            }
            yield f"data: {json.dumps({'type': 'run_started', 'run_id': result.get('run_id')}, ensure_ascii=False)}\n\n"
            yield f"data: {json.dumps({'type': 'plan_created', 'plan': plan}, ensure_ascii=False)}\n\n"
            if result.get("sources"):
                yield f"data: {json.dumps({'type': 'sources', 'sources': result['sources']}, ensure_ascii=False)}\n\n"
            pending = result.get("requires_confirmation")
            if pending:
                try:
                    args = json.loads(pending.get("arguments") or "{}") if isinstance(pending.get("arguments"), str) else pending.get("arguments") or {}
                except json.JSONDecodeError:
                    args = {}
                state_store.set("pending-action", conversation_id, {
                    "name": pending.get("tool"),
                    "args": args,
                    "run_id": result.get("run_id"),
                    "access": "write",
                    "provider": pending.get("provider"),
                    "source_text": payload.content,
                }, 900)
            save_assistant(answer, json.dumps(result.get("sources") or [], ensure_ascii=False))
            for ch in answer:
                yield f"data: {json.dumps({'type': 'token', 'content': ch}, ensure_ascii=False)}\n\n"
            yield f"data: {json.dumps({'type': 'done'}, ensure_ascii=False)}\n\n"

        return StreamingResponse(selected_agent_events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
    # 基础对话历史（不含知识库上下文）——是否检索交给 Strategy Agent 决定，命中后再注入
    base_history = [{"role": message.role, "content": message.content} for message in conversation.messages]
    if agent:
        base_history.append({"role": "system", "content": f"智能体身份与规则：\n{agent.system_prompt}"})
    if payload.attachment_text:
        base_history.append({"role": "system", "content": f"用户上传的文件「{payload.attachment_name or '附件'}」内容：\n{payload.attachment_text[:8000]}"})
    user_turn = {"role": "user", "content": payload.content}
    model = payload.model
    enabled_connectors = list(dict.fromkeys(payload.connectors or []))
    is_web_fetch = _looks_like_web_fetch(payload.content)
    if is_web_fetch and remote_mcp.is_configured("fetcher") and "fetcher" not in enabled_connectors:
        enabled_connectors.insert(0, "fetcher")

    # 本次可用 skill 池：规划器只见元数据；正文仅在权限通过且实际选中后读取。
    skill_rows = db.scalars(select(Skill).where(
        Skill.workspace_id == ws, Skill.status.in_(("active", "published"))
    )).all()
    skill_catalog = [{"id": s.id, "name": s.name, "description": s.description,
                      "summary": s.summary, "category_path": s.category_path,
                      "trigger_phrases": s.trigger_phrases} for s in skill_rows]

    # 落地配置：按该 Agent 的资源权限收窄本次实际可用的连接器/Skill/知识库（允许列表为空=不限制）
    if deploy_config:
        enabled_connectors, skill_catalog, knowledge_base_id = apply_resource_permissions(
            deploy_config, enabled_connectors, skill_catalog, knowledge_base_id)

    manual_skill_ids = [sid for sid in (payload.skill_ids or []) if any(s["id"] == sid for s in skill_catalog)]
    has_kb = bool(knowledge_base_id)

    # 组装本次可用工具：静态连接器（飞书）+ 动态 MCP 连接器（GitHub + 通用远程 MCP）
    tool_specs = agent_tools.specs(enabled_connectors)
    mcp_write_names: set[str] = set()
    if "github" in enabled_connectors and github_mcp.is_configured():
        try:
            gh_specs, gh_writes = await github_mcp.tool_specs()
            tool_specs = tool_specs + gh_specs
            mcp_write_names |= gh_writes
            for s in gh_specs:
                mcp_owner[s["function"]["name"]] = "github"
        except Exception:
            pass  # GitHub MCP 拉取失败就跳过其工具，不影响整体对话
    # 通用远程 MCP（高德等）：勾选且已配 key 才拉工具
    for prov in remote_mcp.providers():
        if prov in enabled_connectors and remote_mcp.is_configured(prov):
            try:
                sp, wr = await remote_mcp.tool_specs(prov)
                tool_specs = tool_specs + sp
                mcp_write_names |= wr
                for s in sp:
                    mcp_owner[s["function"]["name"]] = prov
            except Exception:
                pass  # 某个 MCP 拉取失败不影响整体对话

    # M4：MCP Strategy Agent 的输入——写操作名集合 + 工具的连接器归属（静态 + MCP）
    static_write_names = {s["function"]["name"] for s in tool_specs
                          if agent_tools.is_write(s["function"]["name"])}
    write_names = static_write_names | mcp_write_names
    connector_of = {name: t["connector"] for name, t in agent_tools.TOOLS.items()}
    connector_of.update(mcp_owner)  # MCP 工具归属其连接器，供策略/审计正确标注

    # === Agent Runtime Pipeline（M1+M2+M3）：本次执行登记为一个 run，由 Strategy Agent 规划 ===
    run_id = wf.create_run(conversation_id, agent.id if agent else None, user.id, ws, payload.content)

    def sse(payload_obj: dict) -> str:
        return f"data: {json.dumps(payload_obj, ensure_ascii=False)}\n\n"

    async def events():
        parts: list[str] = []
        history = list(base_history)
        public_sources: list[dict] = []
        respond_step_id: str | None = None
        awaiting_confirm = False
        next_idx = 0
        yield sse({"type": "run_started", "run_id": run_id})
        try:
            # === M2：Strategy Agent 规划（决定是否检索、是否需要工具、步骤拆解）===
            yield sse({"type": "planning_started", "run_id": run_id})
            plan = await strategy.plan(
                input_text=payload.content, has_knowledge_base=has_kb,
                enabled_connectors=enabled_connectors,
                agent_prompt=agent.system_prompt if agent else None, model=model,
                skill_catalog=skill_catalog,
            )
            # M3：手动勾选 ∪ 自动选取，覆写为解析后的对象数组随 plan_json 落库
            from .skill_router import load_selected_skill_content, persisted_vector_scores, persist_router_decision, route_skills
            from .memory_ledger import MemoryScope
            # The planner proposes candidates; the deterministic router is the
            # policy gate and emits the auditable no-selection/selection record.
            rows_by_skill_id = {row.id: row for row in skill_rows}
            router_catalog = [
                {
                    **skill,
                    "use_when": json.loads(rows_by_skill_id[skill["id"]].use_when or "[]"),
                    "do_not_use_when": json.loads(rows_by_skill_id[skill["id"]].do_not_use_when or "[]"),
                }
                for skill in skill_catalog
            ]
            proposed_ids = set(plan.get("skills", []))
            query_embedding = await embed_query(payload.content)
            stored_vector_scores = persisted_vector_scores(skill_rows, query_embedding or [])
            proposed_vector_scores = {
                skill_id: max(stored_vector_scores.get(skill_id, 0.0), 1.0 if skill_id in proposed_ids else 0.0)
                for skill_id in {row.id for row in skill_rows}
            }
            router_audit = route_skills(
                router_catalog, query=payload.content,
                permitted_ids=[skill["id"] for skill in skill_catalog], manual_ids=manual_skill_ids,
                # A model proposal is retained as a vector-stage signal; lexical
                # scoring remains deterministic and negative scenarios can veto it.
                vector_scores=proposed_vector_scores,
                allowed_statuses=("active", "published"),
            )
            with SessionLocal() as audit_db:
                persist_router_decision(
                    audit_db,
                    scope=MemoryScope(
                        workspace_id=ws, user_id=user.id,
                        agent_id=agent.id if agent else "__workbench__", run_id=run_id,
                    ),
                    decision=router_audit,
                )
                audit_db.commit()
            selected_ids = router_audit["selected_ids"]
            selection_sources = router_audit["selection_sources"]
            selected_skills = load_selected_skill_content(
                [{"id": row.id, "name": row.name, "content": row.content} for row in skill_rows], selected_ids
            )
            for selected in selected_skills:
                selected["source"] = selection_sources[selected["id"]]
            from .skills_engine import build_skill_instructions
            plan["skill_router"] = router_audit
            plan["skills"] = selected_skills
            # M4：MCP Strategy Agent——确定性工具策略，落进 plan_json 供审计
            tool_policy = build_tool_policy(tool_specs, write_names, connector_of, plan.get("requires_tools", False))
            plan["tool_policy"] = tool_policy
            wf.save_plan(run_id, plan)
            yield sse({"type": "plan_created", "run_id": run_id, "plan": plan})
            if selected_skills:
                yield sse({"type": "skills_selected", "skills": [
                    {"id": s["id"], "name": s["name"], "source": s["source"]} for s in selected_skills]})

            # 步骤①（可选，由计划决定）：知识库检索
            if plan["requires_knowledge"] and knowledge_base_id:
                rstep = wf.add_step(run_id, next_idx, "retrieve", "检索知识库", "rag", input_data={"knowledge_base_id": knowledge_base_id})
                yield sse({"type": "step_started", "step_id": rstep, "index": next_idx, "step_type": "retrieve", "title": "检索知识库"})
                query_vector = await embed_query(payload.content)
                with SessionLocal() as rdb:
                    sources = search_chunks(rdb, knowledge_base_id, payload.content, query_vector)
                public_sources = [{key: item[key] for key in ("document", "page", "quote", "score")} for item in sources]
                if sources:
                    context = "\n\n".join(f"[{item['document']} 第{item['page']}页]\n{item['content']}" for item in sources)
                    history.append({"role": "system", "content": "知识库资料（仅依据这些资料回答）：\n" + context})
                wf.finish_step(rstep, "succeeded", output={"hits": len(sources)})
                yield sse({"type": "step_completed", "step_id": rstep, "status": "succeeded", "output": {"hits": len(sources)}})
                if public_sources:
                    yield sse({"type": "sources", "sources": public_sources})
                next_idx += 1

            # M3：把选中 skill 的方法论/输出格式注入（在用户消息前）
            skill_text = build_skill_instructions(selected_skills)
            if skill_text:
                history.append({"role": "system", "content": skill_text})
            if is_web_fetch and "fetcher" in enabled_connectors:
                history.append({
                    "role": "system",
                    "content": (
                        "Web fetch rule: the user asked to fetch/extract a URL. "
                        "You must call the Fetcher MCP tool fetch_url before answering. "
                        "Do not say you cannot browse unless the fetch_url tool itself returns an error. "
                        "After the tool result, summarize the page title, main text points, and only key links in Chinese. "
                        "Do not expand long query URLs; omit noisy or repetitive search/result links."
                    ),
                })
            if "excel" in enabled_connectors:
                history.append({
                    "role": "system",
                    "content": (
                        "Excel connector rule: when the user asks to create an Excel file from any table, "
                        "plan, list, schedule, or structured content, do not stop after create_workbook. "
                        "Call create_workbook first if the file does not exist, then call write_data_to_excel "
                        "with headers and rows, and optionally format_range/create_table. Use relative filenames "
                        "such as next_week_plan.xlsx; the platform will save them in the writable output/excel folder."
                    ),
                })

            direct_web_fetch_done = False
            if is_web_fetch and "fetcher" in enabled_connectors:
                url_match = _URL_RE.search(payload.content)
                if url_match:
                    fetch_args = {"url": url_match.group(0), "maxLength": 1800}
                    web_step_id = wf.add_step(
                        run_id,
                        next_idx,
                        "tool",
                        "调用工具：fetch_url",
                        "fetcher",
                        input_data={"args": json.dumps(fetch_args, ensure_ascii=False), "access": "read"},
                    )
                    yield sse({
                        "type": "step_started",
                        "step_id": web_step_id,
                        "index": next_idx,
                        "step_type": "tool",
                        "title": "调用工具：fetch_url",
                    })
                    next_idx += 1
                    yield sse({"type": "tool_call", "name": "fetch_url", "args": json.dumps(fetch_args, ensure_ascii=False), "access": "read"})
                    try:
                        fetch_result = await remote_mcp.call_tool("fetcher", "fetch_url", json.dumps(fetch_args, ensure_ascii=False))
                    except Exception as exc:
                        fetch_result = f"网页抓取失败：{exc}"
                    wf.finish_step(web_step_id, "succeeded", output={"result": fetch_result[:500]})
                    yield sse({"type": "step_completed", "step_id": web_step_id, "status": "succeeded"})
                    yield sse({"type": "tool_result", "name": "fetch_url"})
                    history.append({
                        "role": "system",
                        "content": (
                            "Fetched page result. Use only this content to answer. "
                            "Keep the answer concise; show the title, 3-6 main points, and at most 8 key links. "
                            "Do not include long query URLs.\n\n"
                            + fetch_result
                        ),
                    })
                    direct_web_fetch_done = True

            history.append(user_turn)

            # 步骤②：生成回答（LLM + 工具循环；M2 工具仍在此循环内执行，M4 由 MCP Strategy Agent 拆成独立 tool step）
            respond_step_id = wf.add_step(run_id, next_idx, "respond", "生成回答", "llm")
            yield sse({"type": "step_started", "step_id": respond_step_id, "index": next_idx, "step_type": "respond", "title": "生成回答"})
            tool_step_id: str | None = None
            response_tool_specs = [] if direct_web_fetch_done else tool_specs
            forced_tool = None if direct_web_fetch_done else ("fetch_url" if is_web_fetch and any(s["function"]["name"] == "fetch_url" for s in tool_specs) else None)
            write_confirm_enabled = deploy_config.get("write_confirm", True) if deploy_config else True
            async for ev in stream_agent(history, model, response_tool_specs, execute_tool,
                                         needs_confirm=lambda name: write_confirm_enabled and name in tool_policy["write_tools"],
                                         force_tool_name=forced_tool):
                if ev["type"] == "confirm_required":
                    args = json.loads(ev["args"] or "{}") if isinstance(ev["args"], str) else ev["args"]
                    access = "write" if ev["name"] in tool_policy["write_tools"] else "read"
                    state_store.set("pending-action", conversation_id, {"name": ev["name"], "args": args,
                                                         "run_id": run_id, "access": access,
                                                         "provider": mcp_owner.get(ev["name"]),
                                                         "source_text": payload.content}, 900)
                    prompt = agent_tools.describe_call(ev["name"], args) + "\n\n确认请回复「确认」，取消请回复「取消」。"
                    parts.append(prompt)
                    awaiting_confirm = True
                    yield sse({"type": "token", "content": prompt})
                    continue
                if ev["type"] == "tool_call":
                    info = next((t for t in tool_policy["tools"] if t["name"] == ev["name"]),
                                {"connector": "", "access": "read"})
                    tool_step_id = wf.add_step(run_id, next_idx, "tool", f"调用工具：{ev['name']}",
                                               info["connector"], input_data={"args": ev["args"], "access": info["access"]})
                    yield sse({"type": "step_started", "step_id": tool_step_id, "index": next_idx,
                               "step_type": "tool", "title": f"调用工具：{ev['name']}"})
                    next_idx += 1
                    yield sse({**ev, "access": info["access"]})
                    continue
                if ev["type"] == "tool_result":
                    if tool_step_id:
                        wf.finish_step(tool_step_id, "succeeded", output={"result": (ev.get("content") or "")[:500]})
                        yield sse({"type": "step_completed", "step_id": tool_step_id, "status": "succeeded"})
                        tool_step_id = None
                    yield sse({"type": "tool_result", "name": ev["name"]})
                    continue
                if ev["type"] == "token":
                    parts.append(ev["content"])
                yield sse(ev)
            answer = "".join(parts)
            save_assistant(answer, json.dumps(public_sources, ensure_ascii=False))

            if awaiting_confirm:
                # 命中写操作确认闸门：run 暂停等确认（确认回复会另起一次执行）
                wf.finish_step(respond_step_id, "waiting_confirmation", output={"chars": len(answer)})
                wf.finish_run(run_id, "waiting_confirmation", output={"answer": answer, "sources": public_sources})
                yield sse({"type": "step_completed", "step_id": respond_step_id, "status": "waiting_confirmation"})
                yield sse({"type": "run_completed", "run_id": run_id, "status": "waiting_confirmation"})
            else:
                wf.finish_step(respond_step_id, "succeeded", output={"chars": len(answer)})
                wf.finish_run(run_id, "succeeded", output={"answer": answer, "sources": public_sources})
                yield sse({"type": "step_completed", "step_id": respond_step_id, "status": "succeeded"})
                yield sse({"type": "run_completed", "run_id": run_id, "status": "succeeded"})
            yield sse({"type": "done"})
        except Exception as exc:
            if respond_step_id:
                wf.finish_step(respond_step_id, "failed", error=str(exc))
            wf.finish_run(run_id, "failed", error=str(exc))
            yield sse({"type": "step_failed", "step_id": respond_step_id, "error": str(exc)})
            yield sse({"type": "run_failed", "run_id": run_id, "error": str(exc)})
            yield sse({"type": "error", "message": str(exc)})

    return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ============ Agent Runtime Pipeline：Run 查询（PRD §9.1） ============

@app.get("/api/conversations/{conversation_id}/runs", response_model=list[WorkflowRunOut])
def list_conversation_runs(conversation_id: str, ws: str = Depends(current_workspace_id), db: Session = Depends(get_db)):
    if not db.scalar(select(Conversation.id).where(Conversation.id == conversation_id, Conversation.workspace_id == ws)):
        raise HTTPException(404, "任务不存在")
    return db.scalars(select(WorkflowRun).options(selectinload(WorkflowRun.steps)).where(
        WorkflowRun.conversation_id == conversation_id, WorkflowRun.workspace_id == ws).order_by(WorkflowRun.created_at.desc())).all()


@app.get("/api/workflows/runs/{run_id}", response_model=WorkflowRunOut)
def get_workflow_run(run_id: str, ws: str = Depends(current_workspace_id), db: Session = Depends(get_db)):
    run = db.scalar(select(WorkflowRun).options(selectinload(WorkflowRun.steps)).where(
        WorkflowRun.id == run_id, WorkflowRun.workspace_id == ws))
    if not run:
        raise HTTPException(404, "运行记录不存在")
    return run


# ============ Skill Hub（PRD §9.2，M3） ============

@app.get("/api/skills", response_model=list[SkillOut])
def list_skills(ws: str = Depends(current_workspace_id), db: Session = Depends(get_db)):
    return db.scalars(select(Skill).where(Skill.workspace_id == ws).order_by(Skill.builtin.desc(), Skill.created_at)).all()


@app.get("/api/skills/discovery")
async def discover_skills(
    category: str | None = None,
    query: str = Query(default="", max_length=500),
    status: list[str] | None = Query(default=None),
    required_permission: list[str] = Query(default=[]),
    sort_by: str = Query(default="relevance"),
    descending: bool = Query(default=True),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    ws: str = Depends(current_workspace_id),
    db: Session = Depends(get_db),
):
    """Metadata-only Skill discovery; no unselected instruction body crosses this boundary."""
    from .skill_router import build_category_tree, persisted_vector_scores, search_skill_metadata
    rows = db.scalars(select(Skill).where(Skill.workspace_id == ws)).all()
    catalog = [
        {
            "id": row.id, "name": row.name, "category_path": row.category_path,
            "summary": row.summary or row.description,
            "use_when": json.loads(row.use_when or "[]"),
            "do_not_use_when": json.loads(row.do_not_use_when or "[]"),
            "examples": json.loads(row.examples or "[]"),
            "input_schema": json.loads(row.input_schema or "{}"),
            "output_schema": json.loads(row.output_schema or "{}"),
            "requirements": json.loads(row.requirements or "[]"),
            "permissions": json.loads(row.permissions or "[]"),
            "version": row.version, "status": row.status,
            "trigger_phrases": [phrase.strip() for phrase in row.trigger_phrases.split(",") if phrase.strip()],
            "updated_at": row.updated_at.isoformat(),
        }
        for row in rows
    ]
    query_vector = await embed_query(query) if query else None
    vector_scores = persisted_vector_scores(rows, query_vector or [])
    try:
        result = search_skill_metadata(
            catalog,
            query=query,
            category_prefix=category,
            statuses=status or ("active", "published"),
            required_permissions=required_permission,
            vector_scores=vector_scores,
            sort_by=sort_by,
            descending=descending,
            page=page,
            page_size=page_size,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "tree": build_category_tree(catalog),
        # Keep the original key during the client migration while exposing the
        # stable pagination envelope beside it.
        "skills": result["items"],
        **result,
    }


@app.get("/api/skills/discovery/{skill_id}/content")
def load_discovered_skill_content(
    skill_id: str,
    decision_id: str = Query(min_length=1, max_length=36),
    user: User = Depends(current_user),
    ws: str = Depends(current_workspace_id),
    db: Session = Depends(get_db),
):
    """Load a body only after the current user has an audited selection decision."""
    from .governance_models import SkillRouterDecision

    decision = db.scalar(select(SkillRouterDecision).where(
        SkillRouterDecision.decision_id == decision_id,
        SkillRouterDecision.workspace_id == ws,
        SkillRouterDecision.user_id == user.id,
    ))
    if decision is None or skill_id not in decision.selected_ids:
        raise HTTPException(status_code=403, detail="Skill 未通过本次路由策略选择")
    skill = db.scalar(select(Skill).where(
        Skill.id == skill_id,
        Skill.workspace_id == ws,
        Skill.status.in_(("active", "published")),
    ))
    if skill is None:
        raise HTTPException(status_code=404, detail="Skill 不存在或不可执行")
    return {"id": skill.id, "name": skill.name, "version": skill.version, "content": skill.content}


def _skill_payload(data: dict) -> dict:
    """Persist Skill discovery collections as JSON while the public create schema stays ergonomic."""
    from .skill_router import validate_category_path
    validate_category_path(data.get("category_path", "general"))
    for key in ("use_when", "do_not_use_when", "examples", "input_schema", "output_schema", "requirements", "permissions"):
        if key in data and not isinstance(data[key], str):
            data[key] = json.dumps(data[key], ensure_ascii=False)
    return data


def _validated_skill_payload(payload: SkillCreate) -> dict:
    """Apply the same governed contract to both legacy-active and published Skills."""
    from .skill_router import validate_skill_metadata

    data = payload.model_dump()
    # ``active`` is the legacy persisted spelling of runtime-eligible
    # ``published``. Validation accepts both, while routing normalizes active at
    # its boundary so old records remain deployable during migration.
    validate_skill_metadata({"id": "validation", **data})
    return data


def _skill_embedding_text(payload: SkillCreate) -> str:
    return "\n".join(filter(None, (
        payload.name, payload.description, payload.summary,
        *payload.use_when, *[item.strip() for item in payload.trigger_phrases.split(",") if item.strip()],
    )))


@app.post("/api/skills", response_model=SkillOut, status_code=201)
async def create_skill(payload: SkillCreate, ws: str = Depends(current_workspace_id), db: Session = Depends(get_db)):
    data = _validated_skill_payload(payload)
    embedding = await embed_query(_skill_embedding_text(payload))
    stored_embedding = json.dumps(embedding) if embedding and db.bind and db.bind.dialect.name == "sqlite" else embedding
    skill = Skill(
        workspace_id=ws, type="instruction", builtin=False,
        embedding=stored_embedding, embedding_model=settings.embedding_model if embedding else "",
        **_skill_payload(data),
    )
    db.add(skill); db.commit(); db.refresh(skill)
    return skill


def _get_owned_skill(skill_id: str, ws: str, db: Session) -> Skill:
    skill = db.scalar(select(Skill).where(Skill.id == skill_id, Skill.workspace_id == ws))
    if not skill:
        raise HTTPException(404, "技能不存在")
    return skill


@app.get("/api/skills/{skill_id}", response_model=SkillOut)
def get_skill(skill_id: str, ws: str = Depends(current_workspace_id), db: Session = Depends(get_db)):
    return _get_owned_skill(skill_id, ws, db)


@app.put("/api/skills/{skill_id}", response_model=SkillOut)
async def update_skill(skill_id: str, payload: SkillUpdate, ws: str = Depends(current_workspace_id), db: Session = Depends(get_db)):
    skill = _get_owned_skill(skill_id, ws, db)
    if skill.builtin:
        raise HTTPException(403, "内置技能只读")
    data = _validated_skill_payload(payload)
    for k, v in _skill_payload(data).items():
        setattr(skill, k, v)
    embedding = await embed_query(_skill_embedding_text(payload))
    skill.embedding = json.dumps(embedding) if embedding and db.bind and db.bind.dialect.name == "sqlite" else embedding
    skill.embedding_model = settings.embedding_model if embedding else ""
    db.commit(); db.refresh(skill)
    return skill


@app.delete("/api/skills/{skill_id}")
def delete_skill(skill_id: str, ws: str = Depends(current_workspace_id), db: Session = Depends(get_db)):
    skill = _get_owned_skill(skill_id, ws, db)
    if skill.builtin:
        raise HTTPException(403, "内置技能只读")
    db.delete(skill); db.commit()
    return {"ok": True}



# ============ 单端口部署：FastAPI 直接端出打包后的前端 SPA ============
# 必须在所有 @app 路由注册之后挂载——/api/* 等已注册路由优先匹配，这里只兜未匹配路径。
from pathlib import Path as _Path
from fastapi.staticfiles import StaticFiles

_WEB_DIST = _Path(__file__).resolve().parent.parent.parent / "web" / "dist"
if _WEB_DIST.exists():
    app.mount("/", StaticFiles(directory=str(_WEB_DIST), html=True), name="web")
