from datetime import datetime
from pydantic import BaseModel, ConfigDict, Field


class MessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    role: str
    content: str
    sources: str = ""
    created_at: datetime


class ConversationCreate(BaseModel):
    title: str = Field(default="新任务", max_length=160)


class ConversationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    title: str
    created_at: datetime
    updated_at: datetime


class ConversationDetail(ConversationOut):
    messages: list[MessageOut]


class ChatRequest(BaseModel):
    content: str = Field(min_length=1, max_length=20000)
    knowledge_base_id: str | None = None
    agent_id: str | None = None
    model: str | None = None
    connectors: list[str] = []  # 本次对话启用的连接器（其工具才提供给模型）
    attachment_name: str | None = None
    attachment_text: str | None = Field(default=None, max_length=40000)


class ModelTestRequest(BaseModel):
    model: str = Field(min_length=1, max_length=120)


class AgentCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=500)
    system_prompt: str = Field(default="你是一名可靠、严谨的企业智能助手。", max_length=12000)
    model: str = Field(default="gpt-4.1-mini", max_length=120)
    knowledge_base_id: str | None = None


class AgentUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=500)
    system_prompt: str = Field(max_length=12000)
    model: str = Field(max_length=120)
    knowledge_base_id: str | None = None
    status: str = Field(default="draft", max_length=30)


class AgentOut(AgentUpdate):
    model_config = ConfigDict(from_attributes=True)
    id: str
    created_at: datetime
    updated_at: datetime


class KnowledgeBaseCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=500)


class DocumentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    knowledge_base_id: str
    name: str
    content_type: str
    size: int
    status: str
    chunk_count: int
    created_at: datetime


class KnowledgeBaseOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str
    description: str
    created_at: datetime
    documents: list[DocumentOut] = []


# ── knowledge base (Chroma RAG) ────────────────────────────────────

class KnowledgeIndexRequest(BaseModel):
    collection: str = Field(default="default", max_length=80)
    documents: list[str] = Field(min_length=1, max_length=200)


class KnowledgeSearchRequest(BaseModel):
    collection: str = Field(default="default", max_length=80)
    query: str = Field(min_length=1, max_length=2000)
    top_k: int = Field(default=5, ge=1, le=50)
