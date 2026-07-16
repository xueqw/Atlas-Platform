import uuid
from datetime import datetime, timezone
from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from pgvector.sqlalchemy import Vector
from .database import Base
from .encrypted_type import EncryptedText


def uid() -> str:
    return str(uuid.uuid4())


def now() -> datetime:
    return datetime.now(timezone.utc)


# === 多租户地基：工作区 / 用户 / 成员关系 / 会话 ===

class Workspace(Base):
    """租户边界。一个工作区 = 一家公司/组织，下属对话/智能体/知识库都隔离在此。"""
    __tablename__ = "workspaces"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String(120), default="默认工作区")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class User(Base):
    """登录用户。Phase 1 入口=账号密码（预置测试账号点选登录）。飞书是连接器能力，不参与入口认证。"""
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    username: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120), default="")
    email: Mapped[str] = mapped_column(String(200), default="")
    password_hash: Mapped[str] = mapped_column(Text, default="")
    avatar_url: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Membership(Base):
    """用户 ↔ 工作区 多对多，带角色。role: owner | member。"""
    __tablename__ = "memberships"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(20), default="member")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Session(Base):
    """服务端签发的 httpOnly cookie 会话。id 即随机 session token。"""
    __tablename__ = "sessions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Conversation(Base):
    __tablename__ = "conversations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    workspace_id: Mapped[str | None] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True, nullable=True)
    title: Mapped[str] = mapped_column(String(160), default="新任务")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)
    messages: Mapped[list["Message"]] = relationship(back_populates="conversation", cascade="all, delete-orphan", order_by="Message.created_at")


class Message(Base):
    __tablename__ = "messages"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(20))
    content: Mapped[str] = mapped_column(Text)
    sources: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    conversation: Mapped[Conversation] = relationship(back_populates="messages")


class Agent(Base):
    """智能体。kind=prompt：纯提示词+知识库；kind=code：会话生成的代码项目，工作区落在
    storage/app_drafts/{id}/（id 与工作区目录名一致）。两种类型共用同一套版本管理。"""
    __tablename__ = "agents"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    workspace_id: Mapped[str | None] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True, nullable=True)
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(String(500), default="")
    kind: Mapped[str] = mapped_column(String(20), default="prompt")  # prompt | code
    system_prompt: Mapped[str] = mapped_column(Text, default="你是一名可靠、严谨的企业智能助手。")
    model: Mapped[str] = mapped_column(String(120), default="gpt-4.1-mini")
    knowledge_base_id: Mapped[str | None] = mapped_column(ForeignKey("knowledge_bases.id", ondelete="SET NULL"), nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="draft")  # draft | published | archived
    current_version_id: Mapped[str | None] = mapped_column(String(36), nullable=True)  # 正在编辑的草稿版本
    published_version_id: Mapped[str | None] = mapped_column(String(36), nullable=True)  # 发布后固定，不随草稿变化
    created_by: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)  # 历史数据为空=工作区内可见，向后兼容
    deploy_config_json: Mapped[str] = mapped_column(Text, default="{}")  # 落地配置：可见范围/资源权限/写操作确认等，见 deploy_policy.py
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)
    versions: Mapped[list["AgentVersion"]] = relationship(back_populates="agent", cascade="all, delete-orphan", order_by="AgentVersion.version_no")


class AgentVersion(Base):
    """一次 Agent 配置/代码快照。snapshot_json 对 kind=prompt 是
    {system_prompt, model, knowledge_base_id}；对 kind=code 是 {manifest, files: {path: content}}。"""
    __tablename__ = "agent_versions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id", ondelete="CASCADE"), index=True)
    version_no: Mapped[int] = mapped_column(Integer, default=1)
    kind: Mapped[str] = mapped_column(String(20), default="prompt")
    label: Mapped[str] = mapped_column(String(30), default="draft")  # draft | published | history
    note: Mapped[str] = mapped_column(String(200), default="")  # 例如"回滚自 v2"
    snapshot_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    agent: Mapped[Agent] = relationship(back_populates="versions")


class AgentEvalRun(Base):
    """一次测试(run_draft_app)或评测(evaluate_draft_app)的持久化结果。
    发布前检查清单（PRD §5.8）读取最近一条判断「是否通过基础测试/是否有评测结果」。"""
    __tablename__ = "agent_eval_runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(20), default="test")  # test | evaluate
    ok: Mapped[bool] = mapped_column(Boolean, default=False)
    passed: Mapped[int] = mapped_column(Integer, default=0)
    total: Mapped[int] = mapped_column(Integer, default=0)
    pass_rate: Mapped[float] = mapped_column(Float, default=0.0)
    results_json: Mapped[str] = mapped_column(Text, default="[]")
    suite_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    agent_version_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    summary_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class EvaluationSuite(Base):
    """可复用评测集。属于一个 Agent，发布门禁默认读取 is_release_gate=True 的评测集。"""
    __tablename__ = "evaluation_suites"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(160))
    description: Mapped[str] = mapped_column(String(500), default="")
    pass_threshold: Mapped[float] = mapped_column(Float, default=1.0)
    is_release_gate: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class EvaluationCase(Base):
    """单条确定性评测样例。scorers_json 控制关键词、JSON Schema、延迟等评分器。"""
    __tablename__ = "evaluation_cases"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    suite_id: Mapped[str] = mapped_column(ForeignKey("evaluation_suites.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(160))
    input_text: Mapped[str] = mapped_column(Text)
    expected_text: Mapped[str] = mapped_column(Text, default="")
    scorers_json: Mapped[str] = mapped_column(Text, default="{}")
    is_key: Mapped[bool] = mapped_column(Boolean, default=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class AgentApiKey(Base):
    """Agent 对外发布后的 API Key（PRD §6）。一个 Agent 最多一条活跃记录，重置=作废旧的建新的。
    只存 hash，明文只在创建/重置的响应里出现一次。"""
    __tablename__ = "agent_api_keys"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id", ondelete="CASCADE"), unique=True, index=True)
    key_hash: Mapped[str] = mapped_column(String(64))  # sha256 hex
    key_prefix: Mapped[str] = mapped_column(String(16), default="")  # 列表页脱敏展示用，如 "sk-ab12"
    status: Mapped[str] = mapped_column(String(20), default="active")  # active | disabled
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    daily_quota: Mapped[int | None] = mapped_column(Integer, nullable=True)  # None=不限
    allowed_origins: Mapped[str] = mapped_column(Text, default="")  # 逗号分隔，空=不限制
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class KnowledgeBase(Base):
    __tablename__ = "knowledge_bases"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    workspace_id: Mapped[str | None] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True, nullable=True)
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(String(500), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    documents: Mapped[list["Document"]] = relationship(back_populates="knowledge_base", cascade="all, delete-orphan")


class Document(Base):
    __tablename__ = "documents"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    knowledge_base_id: Mapped[str] = mapped_column(ForeignKey("knowledge_bases.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str] = mapped_column(String(120), default="text/plain")
    size: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(30), default="ready")
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    object_key: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    knowledge_base: Mapped[KnowledgeBase] = relationship(back_populates="documents")
    chunks: Mapped[list["DocumentChunk"]] = relationship(back_populates="document", cascade="all, delete-orphan")


class ConnectorConfig(Base):
    """连接器的应用级配置（公司统一）。如飞书的 app_id / app_secret，管理员配一次全公司用。"""
    __tablename__ = "connector_configs"
    provider: Mapped[str] = mapped_column(String(40), primary_key=True)
    data: Mapped[str] = mapped_column(EncryptedText(), default="{}")  # encrypted JSON
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class ConnectorToken(Base):
    """连接器(飞书等)的 OAuth 令牌。demo 单用户：每个 provider 存一条。"""
    __tablename__ = "connector_tokens"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    provider: Mapped[str] = mapped_column(String(40), index=True)  # 如 "feishu"
    access_token: Mapped[str] = mapped_column(EncryptedText())
    refresh_token: Mapped[str] = mapped_column(EncryptedText(), default="")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    account_name: Mapped[str] = mapped_column(String(120), default="")  # 已连接账号显示名
    open_id: Mapped[str] = mapped_column(String(120), default="")  # 发消息收件人定位
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class DocumentChunk(Base):
    __tablename__ = "document_chunks"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    chunk_index: Mapped[int] = mapped_column(Integer)
    page: Mapped[int] = mapped_column(Integer, default=1)
    content: Mapped[str] = mapped_column(Text)
    embedding: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON 向量，空则该块未建索引
    document: Mapped[Document] = relationship(back_populates="chunks")


class AgentMemory(Base):
    """Explicit long-term memory. Private rows are always scoped to their owner."""
    __tablename__ = "agent_memories"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id", ondelete="CASCADE"), index=True)
    category: Mapped[str] = mapped_column(String(40), default="preference")
    content: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list[float] | str | None] = mapped_column(Vector(1024).with_variant(Text(), "sqlite"), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


# === Agent Runtime Pipeline（PRD M1）：一次执行=一个 run，拆成若干 step ===

class WorkflowRun(Base):
    """一次任务运行的边界与生命周期记录。见 PRD §8.2。"""
    __tablename__ = "workflow_runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    workspace_id: Mapped[str | None] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True, nullable=True)
    conversation_id: Mapped[str | None] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"), index=True, nullable=True)
    agent_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    user_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    source: Mapped[str] = mapped_column(String(20), default="chat")  # chat（工作台对话）| api（外部 API Key 调用）
    input_text: Mapped[str] = mapped_column(Text, default="")
    # created|planning|running|waiting_confirmation|succeeded|failed|cancelled
    status: Mapped[str] = mapped_column(String(30), default="created", index=True)
    plan_json: Mapped[str] = mapped_column(Text, default="")     # M2 起 Strategy Agent 计划
    output_json: Mapped[str] = mapped_column(Text, default="")   # {answer, sources}
    error: Mapped[str] = mapped_column(Text, default="")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    steps: Mapped[list["WorkflowStep"]] = relationship(back_populates="run", cascade="all, delete-orphan", order_by="WorkflowStep.index")


class WorkflowStep(Base):
    """run 内的一个执行步骤。见 PRD §8.3。"""
    __tablename__ = "workflow_steps"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    run_id: Mapped[str] = mapped_column(ForeignKey("workflow_runs.id", ondelete="CASCADE"), index=True)
    index: Mapped[int] = mapped_column(Integer, default=0)
    type: Mapped[str] = mapped_column(String(30), default="respond")   # retrieve|skill|tool|respond
    title: Mapped[str] = mapped_column(String(200), default="")
    executor: Mapped[str] = mapped_column(String(40), default="llm")   # rag|skill_agent|mcp_tool|llm
    skill_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="created")
    input_json: Mapped[str] = mapped_column(Text, default="")
    output_json: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str] = mapped_column(Text, default="")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    run: Mapped[WorkflowRun] = relationship(back_populates="steps")


class RuntimeRun(Base):
    """Durable boundary for one LangGraph-backed execution.

    WorkflowRun stays the product projection during the migration. RuntimeRun
    owns request idempotency, graph/checkpoint identity, and durable event order.
    """
    __tablename__ = "runtime_runs"
    __table_args__ = (
        UniqueConstraint("workspace_id", "idempotency_key", name="uq_runtime_runs_workspace_idempotency"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    agent_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    conversation_id: Mapped[str | None] = mapped_column(ForeignKey("conversations.id", ondelete="SET NULL"), nullable=True, index=True)
    version_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    source: Mapped[str] = mapped_column(String(30), default="workbench")
    graph_template: Mapped[str] = mapped_column(String(60), default="react")
    execution_mode: Mapped[str] = mapped_column(String(30), default="legacy")
    graph_schema_version: Mapped[str] = mapped_column(String(40), default="atlas.agent-graph.v1")
    thread_id: Mapped[str] = mapped_column(String(180), unique=True, index=True)
    idempotency_key: Mapped[str] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(30), default="created", index=True)
    input_json: Mapped[str] = mapped_column(Text, default="{}")
    state_json: Mapped[str] = mapped_column(Text, default="{}")
    last_sequence: Mapped[int] = mapped_column(Integer, default=0)
    legacy_workflow_run_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    events: Mapped[list["RuntimeEvent"]] = relationship(back_populates="run", cascade="all, delete-orphan", order_by="RuntimeEvent.sequence")
    interrupts: Mapped[list["RuntimeInterrupt"]] = relationship(back_populates="run", cascade="all, delete-orphan")


class RuntimeEvent(Base):
    """Immutable Atlas Runtime Event ledger, ordered only within one run."""
    __tablename__ = "runtime_events"
    __table_args__ = (
        UniqueConstraint("run_id", "sequence", name="uq_runtime_events_run_sequence"),
        UniqueConstraint("run_id", "event_id", name="uq_runtime_events_run_event"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    run_id: Mapped[str] = mapped_column(ForeignKey("runtime_runs.id", ondelete="CASCADE"), index=True)
    event_id: Mapped[str] = mapped_column(String(64), index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    schema_version: Mapped[str] = mapped_column(String(40), default="atlas.runtime-event.v1")
    type: Mapped[str] = mapped_column(String(60), index=True)
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    run: Mapped[RuntimeRun] = relationship(back_populates="events")


class RuntimeInterrupt(Base):
    """Persisted approval/resume request. Phase 1 stores it but denies writes."""
    __tablename__ = "runtime_interrupts"
    __table_args__ = (UniqueConstraint("run_id", "nonce", name="uq_runtime_interrupt_run_nonce"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    run_id: Mapped[str] = mapped_column(ForeignKey("runtime_runs.id", ondelete="CASCADE"), index=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(30), default="pending", index=True)
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    parameter_digest: Mapped[str] = mapped_column(String(128), default="")
    resource_version: Mapped[str] = mapped_column(String(80), default="")
    nonce: Mapped[str] = mapped_column(String(80))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    run: Mapped[RuntimeRun] = relationship(back_populates="interrupts")


class Skill(Base):
    """Skill Hub 的 instruction 型技能（PRD M3，§8.4 简化版）。"""
    __tablename__ = "skills"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    workspace_id: Mapped[str | None] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True, nullable=True)
    name: Mapped[str] = mapped_column(String(100), default="")
    description: Mapped[str] = mapped_column(Text, default="")      # 自动选主依据
    type: Mapped[str] = mapped_column(String(30), default="instruction")
    trigger_phrases: Mapped[str] = mapped_column(Text, default="")  # 逗号分隔，弱信号
    content: Mapped[str] = mapped_column(Text, default="")          # 注入回答步的正文
    category_path: Mapped[str] = mapped_column(String(240), default="general")
    summary: Mapped[str] = mapped_column(Text, default="")
    use_when: Mapped[str] = mapped_column(Text, default="[]")
    do_not_use_when: Mapped[str] = mapped_column(Text, default="[]")
    examples: Mapped[str] = mapped_column(Text, default="[]")
    input_schema: Mapped[str] = mapped_column(Text, default="{}")
    output_schema: Mapped[str] = mapped_column(Text, default="{}")
    requirements: Mapped[str] = mapped_column(Text, default="[]")
    permissions: Mapped[str] = mapped_column(Text, default="[]")
    version: Mapped[str] = mapped_column(String(40), default="1.0.0")
    builtin: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(30), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)
