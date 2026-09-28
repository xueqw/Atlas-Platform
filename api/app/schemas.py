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
    title: str = Field(default="New task", max_length=160)


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
    connectors: list[str] = []
    attachment_name: str | None = None
    attachment_text: str | None = Field(default=None, max_length=40000)


class ModelTestRequest(BaseModel):
    model: str = Field(min_length=1, max_length=120)


class GithubConfigRequest(BaseModel):
    pat: str = Field(min_length=1, max_length=255)


class AgentCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=500)
    system_prompt: str = Field(default="You are a reliable, precise AI assistant for business teams.", max_length=12000)
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
