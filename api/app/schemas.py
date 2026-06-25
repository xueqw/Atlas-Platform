from datetime import datetime
from pydantic import BaseModel, ConfigDict, Field


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    username: str
    name: str
    email: str = ""
    avatar_url: str = ""


class WorkspaceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str


class MeOut(BaseModel):
    user: UserOut
    workspace: WorkspaceOut
    role: str


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=1, max_length=200)


class AccountOut(BaseModel):
    username: str
    name: str


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


class GithubConfigRequest(BaseModel):
    pat: str = Field(min_length=1, max_length=255)


class FeishuConfigRequest(BaseModel):
    app_id: str = Field(min_length=1, max_length=120)
    app_secret: str = Field(min_length=1, max_length=255)


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


# === Agent Runtime Pipeline（M1） ===

class WorkflowStepOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    index: int
    type: str
    title: str
    executor: str
    skill_id: str | None = None
    status: str
    input_json: str = ""
    output_json: str = ""
    error: str = ""
    started_at: datetime | None = None
    ended_at: datetime | None = None


class WorkflowRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    conversation_id: str | None = None
    agent_id: str | None = None
    user_id: str | None = None
    input_text: str
    status: str
    plan_json: str = ""
    output_json: str = ""
    error: str = ""
    started_at: datetime | None = None
    ended_at: datetime | None = None
    created_at: datetime
    steps: list[WorkflowStepOut] = []
