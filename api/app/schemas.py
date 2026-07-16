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
    skill_ids: list[str] = []  # 本次对话手动勾选（强制启用）的 skill
    attachment_name: str | None = None
    attachment_text: str | None = Field(default=None, max_length=40000)


class ModelTestRequest(BaseModel):
    model: str = Field(min_length=1, max_length=120)


class ModelProviderConfigRequest(BaseModel):
    provider_id: str = Field(min_length=1, max_length=40)
    api_key: str = Field(min_length=1, max_length=512)
    base_url: str | None = Field(default=None, max_length=300)


class GithubConfigRequest(BaseModel):
    pat: str = Field(min_length=1, max_length=255)


class FeishuConfigRequest(BaseModel):
    app_id: str = Field(min_length=1, max_length=120)
    app_secret: str = Field(min_length=1, max_length=255)


class McpKeyRequest(BaseModel):
    key: str = Field(min_length=1, max_length=512)


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
    kind: str = "prompt"
    version_no: int = 0
    published_version_no: int | None = None
    knowledge_base_count: int = 0
    skills_count: int = 0
    connector_count: int = 0
    call_count: int = 0
    created_by: str | None = None
    last_eval_ok: bool | None = None
    has_passed_test: bool = False
    deploy_config_configured: bool = False
    workflow_stage: str = "draft"
    health_status: str = "unknown"
    success_rate: float | None = None
    has_unpublished_changes: bool = False
    created_at: datetime
    updated_at: datetime


# === Agent 模板（PRD §5.2） ===

class AgentTemplateOut(BaseModel):
    id: str
    name: str
    description: str


class CreateFromTemplateRequest(BaseModel):
    template_id: str = Field(min_length=1, max_length=60)


# === Agents + 版本管理 ===

class AgentVersionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    agent_id: str
    version_no: int
    kind: str
    label: str
    note: str = ""
    created_at: datetime


class AgentVersionDetail(AgentVersionOut):
    snapshot: dict


class VersionFileDiff(BaseModel):
    path: str
    status: str  # added | removed | modified
    diff: str = ""  # unified diff text；added/removed 时为空，前端直接展示整份内容


class AgentVersionDiff(BaseModel):
    from_version: int
    to_version: int
    kind: str
    files: list[VersionFileDiff] = []  # kind=code 时按文件对比
    fields: list[dict] = []  # kind=prompt 时按字段对比：[{field, old, new}]


class SaveVersionRequest(BaseModel):
    note: str = Field(default="", max_length=200)


# === 落地配置（PRD §5.7） ===

class DeployConfigOut(BaseModel):
    visibility: str = "workspace"  # private | shared | workspace | marketplace
    shared_user_ids: list[str] = []
    allowed_knowledge_base_ids: list[str] = []
    allowed_skill_ids: list[str] = []
    allowed_connectors: list[str] = []
    write_confirm: bool = True
    api_access: bool = False  # 占位：需要 API Key 功能完成后才有实际入口消费它
    call_log_enabled: bool = True
    high_risk_approved: bool = False


class DeployConfigUpdate(DeployConfigOut):
    pass


class CallLogEntryOut(BaseModel):
    id: str
    time: datetime
    source: str = "chat"
    status: str
    latency_ms: int | None = None
    error: str = ""
    request_path: str = ""
    status_code: int | None = None
    token_usage: int | None = None


class WorkspaceMemberOut(BaseModel):
    id: str
    name: str
    username: str


# === 发布前检查清单（PRD §5.8） ===

class PublishChecklistItem(BaseModel):
    key: str
    label: str
    ok: bool
    level: str  # blocking | warning


class PublishChecklistOut(BaseModel):
    items: list[PublishChecklistItem]
    can_publish: bool  # 所有 blocking 项通过；warning 项不影响这个值


# === API Key（PRD §6） ===

class ApiKeyUpdate(BaseModel):
    status: str = Field(default="active", max_length=20)  # active | disabled
    expires_at: datetime | None = None
    daily_quota: int | None = Field(default=None, ge=0)
    allowed_origins: str = Field(default="", max_length=1000)  # 逗号分隔


class ApiKeyOut(BaseModel):
    """元信息，不含明文 key。"""
    exists: bool
    key_prefix: str = ""
    status: str = "active"
    expires_at: datetime | None = None
    daily_quota: int | None = None
    allowed_origins: str = ""
    created_at: datetime | None = None
    last_used_at: datetime | None = None


class ApiKeyCreateOut(ApiKeyOut):
    """创建/重置的一次性响应，含明文 key。之后再也拿不到明文。"""
    key: str


class InvokeRequest(BaseModel):
    input: str = Field(min_length=1, max_length=20000)


class InvokeResponse(BaseModel):
    output: str
    elapsed_ms: int
    version_no: int | None = None
    run_id: str | None = None


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


class RuntimeStartRequest(BaseModel):
    agent_id: str | None = None
    conversation_id: str | None = None
    source: str = Field(default="workbench", max_length=30)
    version_selector: str = Field(default="published", max_length=30)
    version_id: str | None = None
    input: dict = Field(default_factory=dict)
    requested_resources: dict = Field(default_factory=dict)
    idempotency_key: str = Field(min_length=8, max_length=120)


class RuntimeResumeRequest(BaseModel):
    interrupt_id: str
    command: dict = Field(default_factory=dict)


class RuntimeEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    event_id: str
    run_id: str
    sequence: int
    schema_version: str
    type: str
    payload: dict
    time: datetime


class RuntimeRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    workspace_id: str
    user_id: str
    agent_id: str | None = None
    conversation_id: str | None = None
    version_id: str | None = None
    source: str
    graph_template: str
    graph_schema_version: str
    thread_id: str
    status: str
    last_sequence: int
    error: str = ""
    created_at: datetime
    started_at: datetime | None = None
    ended_at: datetime | None = None


# === Skill Hub（M3） ===

class SkillCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=2000)
    content: str = Field(default="", max_length=12000)
    trigger_phrases: str = Field(default="", max_length=1000)
    category_path: str = Field(default="general", max_length=240)
    summary: str = Field(default="", max_length=2000)
    use_when: list[str] = []
    do_not_use_when: list[str] = []
    examples: list[str] = []
    input_schema: dict = {}
    output_schema: dict = {}
    requirements: list[str] = []
    permissions: list[str] = []
    version: str = Field(default="1.0.0", max_length=40)


class SkillUpdate(SkillCreate):
    status: str = Field(default="active", max_length=30)


class SkillOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str
    description: str
    type: str
    trigger_phrases: str
    content: str
    builtin: bool
    status: str
    category_path: str = "general"
    summary: str = ""
    use_when: str = "[]"
    do_not_use_when: str = "[]"
    examples: str = "[]"
    input_schema: str = "{}"
    output_schema: str = "{}"
    requirements: str = "[]"
    permissions: str = "[]"
    version: str = "1.0.0"
    created_at: datetime
    updated_at: datetime
