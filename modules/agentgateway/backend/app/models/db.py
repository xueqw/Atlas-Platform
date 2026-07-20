from datetime import datetime, timezone
from sqlmodel import SQLModel, Field, Relationship
from sqlalchemy import Index
from typing import Optional, List
from enum import Enum


def _utcnow() -> datetime:
    """Return current UTC time as a naive datetime.

    Mirrors the pre-Python-3.12 ``datetime.utcnow()`` semantics so existing
    SQLite columns (stored as naive UTC) continue to compare and serialize
    the same way. ``datetime.now(timezone.utc).replace(tzinfo=None)`` is
    the recommended drop-in for the deprecated ``utcnow``.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


class AgentStatus(str, Enum):
    DRAFT = "draft"
    TESTING = "testing"
    PUBLISHED = "published"
    DEPRECATED = "deprecated"


class PromptConfig(SQLModel, table=True):
    __tablename__ = "prompt_configs"
    id: Optional[int] = Field(default=None, primary_key=True)
    role_name: str = Field(default="", max_length=100)
    role_description: str = Field(default="", max_length=2000)
    output_format: str = Field(default="markdown", max_length=50)
    constraints: str = Field(default="[]")  # JSON array
    system_prompt: str = Field(default="", max_length=10000)
    template_id: Optional[int] = Field(default=None, foreign_key="prompt_templates.id")
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


class ModelConfig(SQLModel, table=True):
    __tablename__ = "model_configs"
    id: Optional[int] = Field(default=None, primary_key=True)
    provider: str = Field(max_length=50)
    model_name: str = Field(max_length=100)
    temperature: float = Field(default=0.7)
    max_tokens: int = Field(default=4096)
    top_p: float = Field(default=1.0)
    streaming: bool = Field(default=True)
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


class Agent(SQLModel, table=True):
    __tablename__ = "agents"
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(max_length=200)
    description: str = Field(default="", max_length=1000)
    status: AgentStatus = Field(default=AgentStatus.DRAFT)
    version: int = Field(default=1)
    prompt_config_id: Optional[int] = Field(default=None, foreign_key="prompt_configs.id")
    model_config_id: Optional[int] = Field(default=None, foreign_key="model_configs.id")
    dag_graph_id: Optional[int] = Field(default=None, foreign_key="dag_graphs.id")
    release_gate_thresholds: str = Field(default="{}")  # JSON: per-agent release-gate threshold overrides
    # ── L3 Agent Capability Layer: memory policy (optional-first, default off) ──
    memory_enabled: bool = Field(default=False)
    memory_provider: str = Field(default="none", max_length=20)  # none | pgonly | mem0
    memory_scope: str = Field(default="", max_length=20)  # user | agent | agent_user | workspace
    memory_policy_json: str = Field(default="{}")  # JSON: full memory policy from planner agent_spec.memory
    procedural_refs_json: str = Field(default="[]")  # JSON array: procedural skill/blueprint refs
    architecture_pattern: str = Field(default="", max_length=100)
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


class PromptTemplate(SQLModel, table=True):
    __tablename__ = "prompt_templates"
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(max_length=200)
    category: str = Field(default="general", max_length=50)
    content: str = Field(max_length=10000)
    preview: str = Field(default="", max_length=200)
    created_at: datetime = Field(default_factory=_utcnow)


class ModelRegistry(SQLModel, table=True):
    __tablename__ = "model_registry"
    id: Optional[int] = Field(default=None, primary_key=True)
    provider: str = Field(max_length=50)
    model_id: str = Field(max_length=100)
    display_name: str = Field(max_length=200)
    capability_tags: str = Field(default="[]")  # JSON array
    context_window: int = Field(default=8192)
    max_output_tokens: int = Field(default=4096)
    input_price_per_1k: float = Field(default=0.0)
    output_price_per_1k: float = Field(default=0.0)
    supports_streaming: bool = Field(default=True)
    supports_vision: bool = Field(default=False)
    is_available: bool = Field(default=True)


class Conversation(SQLModel, table=True):
    __tablename__ = "conversations"
    id: Optional[int] = Field(default=None, primary_key=True)
    agent_id: int = Field(foreign_key="agents.id")
    title: str = Field(default="新对话", max_length=200)
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


class Message(SQLModel, table=True):
    __tablename__ = "messages"
    id: Optional[int] = Field(default=None, primary_key=True)
    conversation_id: int = Field(foreign_key="conversations.id")
    role: str = Field(max_length=20)  # user or assistant
    content: str = Field(max_length=50000)
    # ── L1 session-history retrieval metadata (optional-first) ──
    message_type: str = Field(default="", max_length=30)  # user | assistant | tool | planner | system_summary
    session_kind: str = Field(default="", max_length=30)  # agent_chat | planner | evaluation | release
    metadata_json: str = Field(default="{}")  # JSON object: arbitrary message metadata
    importance_score: float = Field(default=0.0)
    summary_status: str = Field(default="", max_length=20)  # "" | pending | done
    embedding_status: str = Field(default="", max_length=20)  # "" | pending | done
    created_at: datetime = Field(default_factory=_utcnow)


class PipelineNodeStatusModel(SQLModel, table=True):
    __tablename__ = "pipeline_node_status"
    id: Optional[int] = Field(default=None, primary_key=True)
    agent_id: int = Field(foreign_key="agents.id")
    node_name: str = Field(max_length=50)  # "p", "m", "k"
    status: str = Field(default="pending", max_length=20)  # pending, in_progress, complete, failed
    quality_score: Optional[float] = Field(default=None)
    started_at: Optional[datetime] = Field(default=None)
    completed_at: Optional[datetime] = Field(default=None)
    trace_url: Optional[str] = Field(default=None, max_length=500)


class CapabilityItem(SQLModel, table=True):
    __tablename__ = "capability_items"
    id: Optional[int] = Field(default=None, primary_key=True)
    type: str = Field(max_length=20)  # prompt, model, tool
    name: str = Field(max_length=200)
    description: str = Field(default="", max_length=2000)
    tags: str = Field(default="[]")  # JSON array
    config: str = Field(default="{}")  # JSON object
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


class PromptVersion(SQLModel, table=True):
    __tablename__ = "prompt_versions"
    id: Optional[int] = Field(default=None, primary_key=True)
    agent_id: int = Field(foreign_key="agents.id")
    version_number: int = Field(default=1)
    prompt_config: str = Field(default="{}")  # JSON snapshot of full PromptConfig
    created_at: datetime = Field(default_factory=_utcnow)


class ToolConfig(SQLModel, table=True):
    __tablename__ = "tool_configs"
    id: Optional[int] = Field(default=None, primary_key=True)
    agent_id: int = Field(foreign_key="agents.id")
    name: str = Field(max_length=200)
    description: str = Field(default="", max_length=2000)
    parameters: str = Field(default="{}")  # JSON Schema
    mock_endpoint: str = Field(default="", max_length=500)
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


# ── DAG Workbench Models ──

class DAGGraph(SQLModel, table=True):
    __tablename__ = "dag_graphs"
    id: Optional[int] = Field(default=None, primary_key=True)
    agent_id: int = Field(foreign_key="agents.id")
    graph_json: str = Field(default="{}")  # JSON: nodes, edges, positions
    state_schema: str = Field(default="{}")  # JSON: State schema definition
    version: int = Field(default=1)
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


class NodeTypeRegistry(SQLModel, table=True):
    __tablename__ = "node_type_registry"
    id: Optional[int] = Field(default=None, primary_key=True)
    node_type: str = Field(max_length=50, unique=True)
    display_name: str = Field(max_length=100)
    category: str = Field(max_length=50)  # core, knowledge, tool, flow, external
    config_schema: str = Field(default="{}")  # JSON Schema for config fields
    input_keys: str = Field(default="[]")  # JSON array of State keys this node reads
    output_keys: str = Field(default="[]")  # JSON array of State keys this node writes
    enabled: bool = Field(default=True)


class HermesRule(SQLModel, table=True):
    __tablename__ = "hermes_rules"
    id: Optional[int] = Field(default=None, primary_key=True)
    rule_type: str = Field(max_length=50)  # validation, constraint, evaluation
    node_type: Optional[str] = Field(default=None, max_length=50)  # None = global rule
    condition_expr: str = Field(default="")  # Expression or rule key
    severity: str = Field(default="error", max_length=20)  # error, warning
    message_template: str = Field(default="", max_length=500)


class EvaluationResult(SQLModel, table=True):
    """Legacy "combined" evaluation result (one row = whole run + all cases).

    Retained as-is for backward compatibility with the existing
    /agents/{id}/evaluate + /agents/{id}/evaluations endpoints. The four-layer
    Suite/Case/Run/Result model (below) supersedes it for new evaluation flows.
    """
    __tablename__ = "evaluation_results"
    id: Optional[int] = Field(default=None, primary_key=True)
    agent_id: int = Field(foreign_key="agents.id")
    dag_version: int = Field(default=1)
    test_cases: str = Field(default="[]")  # JSON: list of test case results
    scores: str = Field(default="{}")  # JSON: accuracy, latency, token_efficiency
    passed: bool = Field(default=False)
    created_at: datetime = Field(default_factory=_utcnow)


class EvaluationSuite(SQLModel, table=True):
    __tablename__ = "evaluation_suites"
    id: Optional[int] = Field(default=None, primary_key=True)
    agent_id: int = Field(foreign_key="agents.id", index=True)
    name: str = Field(max_length=200)
    description: str = Field(default="", max_length=1000)
    # Dimension-driven evaluation (Phase 1): suite type drives the default
    # dimension template the frontend pre-fills when creating cases.
    suite_type: str = Field(default="general", max_length=50)  # general/planner/customer_support/rag/workflow
    default_dimensions_json: str = Field(default="{}")  # JSON: default {"dimensions": [...]} for new cases
    # Release-gate policy override for this suite (Phase 3). JSON object layered
    # over the suite_type gate template: {"generic": {...}, "dimensions": [...]}.
    release_gate_policy_json: str = Field(default="{}")  # JSON: per-suite gate policy override
    pass_threshold: float = Field(default=0.6)  # case overall threshold when not set per-case
    created_at: datetime = Field(default_factory=_utcnow)


class AgentTestCase(SQLModel, table=True):
    __tablename__ = "agent_test_cases"
    id: Optional[int] = Field(default=None, primary_key=True)
    agent_id: int = Field(foreign_key="agents.id")
    suite_id: Optional[int] = Field(default=None, foreign_key="evaluation_suites.id", index=True)
    name: str = Field(max_length=200)
    input_message: str = Field(default="")
    expected_keywords: str = Field(default="[]")  # JSON array
    expected_sentiment: Optional[str] = Field(default=None, max_length=50)
    expected_schema: str = Field(default="{}")  # JSON Schema for output validation
    judge_prompt: str = Field(default="")  # Free-text LLM-as-judge criteria
    is_key: bool = Field(default=False)  # Key cases are tracked with a separate pass rate
    notes: str = Field(default="", max_length=2000)
    sort_order: int = Field(default=0)
    # Dimension-driven evaluation (Phase 1). When dimensions_json is empty the
    # engine falls back to the legacy keyword/schema/judge fields above.
    dimensions_json: str = Field(default="")  # JSON: {"dimensions": [{name,type,weight,threshold,required,...}]}
    reference_output: str = Field(default="")  # Expected/reference answer for ragas + judge
    context_json: str = Field(default="{}")  # JSON: retrieved_contexts etc. (RAG, Phase 4)
    constraints_json: str = Field(default="{}")  # JSON: output constraints / forbidden / style rules
    metadata_json: str = Field(default="{}")  # JSON: arbitrary case metadata for scorers
    # Multi-turn conversation support. JSON array of turns:
    # [{"user_query": "...", "expected_answer": "..."}]. When non-empty, the
    # engine executes turns sequentially preserving DAGRunner state between them.
    # Single-turn cases keep this empty and use `input_message` as before.
    turns_json: str = Field(default="")  # JSON: [{user_query, expected_answer?}]
    created_at: datetime = Field(default_factory=_utcnow)


class EvaluationRun(SQLModel, table=True):
    """One batch evaluation of a suite against a specific agent version."""
    __tablename__ = "evaluation_runs"
    id: Optional[int] = Field(default=None, primary_key=True)
    agent_id: int = Field(foreign_key="agents.id", index=True)
    suite_id: int = Field(foreign_key="evaluation_suites.id", index=True)
    dag_version: int = Field(default=0)
    prompt_version: int = Field(default=0)
    model: str = Field(default="", max_length=100)
    trace_ids: str = Field(default="[]")  # JSON array of per-case trace_ids
    summary: str = Field(default="{}")  # JSON: pass_rate, key_pass_rate, latency, token
    passed: bool = Field(default=False)
    created_at: datetime = Field(default_factory=_utcnow, index=True)


class EvaluationCaseResult(SQLModel, table=True):
    """Per-case result within an EvaluationRun."""
    __tablename__ = "evaluation_case_results"
    id: Optional[int] = Field(default=None, primary_key=True)
    run_id: int = Field(foreign_key="evaluation_runs.id", index=True)
    case_id: int = Field(foreign_key="agent_test_cases.id", index=True)
    case_name: str = Field(default="", max_length=200)
    is_key: bool = Field(default=False)
    output: str = Field(default="")  # agent final output text
    scores: str = Field(default="{}")  # JSON: per-dimension scores + overall (flat, legacy-compatible)
    # Dimension-driven evaluation (Phase 1): rich per-dimension breakdown.
    dimension_results_json: str = Field(default="{}")  # JSON: {"dimensions": [{name,score,passed,threshold,weight,reason,evidence}], "overall": x}
    evidence_json: str = Field(default="{}")  # JSON: supplementary evidence per dimension
    # Phase 2 (Langfuse score evidence layer): JSON array of Langfuse score ids
    # written back for this case's dimensions + overall. Empty "[]" when Langfuse
    # is unconfigured/unreachable (best-effort writeback).
    langfuse_score_ids: str = Field(default="[]")  # JSON array of Langfuse score ids
    passed: bool = Field(default=False)
    trace_id: str = Field(default="", max_length=64)
    duration_ms: int = Field(default=0)
    # Human evaluation scoring (campus-eval extension). Filled via PATCH API
    # after a reviewer inspects the agent's output.
    human_score: Optional[int] = Field(default=None)  # 0=wrong, 1=partial, 2=correct
    human_notes: str = Field(default="")  # reviewer comments
    human_scored_at: Optional[datetime] = Field(default=None)
    created_at: datetime = Field(default_factory=_utcnow)


class DAGTemplate(SQLModel, table=True):
    __tablename__ = "dag_templates"
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(max_length=200)
    description: str = Field(default="", max_length=1000)
    category: str = Field(default="general", max_length=50)
    graph_json: str = Field(default="{}")  # JSON: pre-built DAG topology
    icon: str = Field(default="", max_length=50)
    sort_order: int = Field(default=0)


class ArchitectureProposal(SQLModel, table=True):
    __tablename__ = "architecture_proposals"
    id: Optional[int] = Field(default=None, primary_key=True)
    agent_id: Optional[int] = Field(default=None)
    trigger_type: str = Field(default="create", max_length=20)
    user_request: str = Field(default="")
    requirement_summary: str = Field(default="")
    proposed_graph_json: str = Field(default="{}")
    rationale: str = Field(default="")
    status: str = Field(default="draft", max_length=20)
    confirmed_constraints: str = Field(default="[]")
    task_classification: str = Field(default="", max_length=50)
    proposal_version: int = Field(default=1)
    user_feedback_summary: str = Field(default="[]")
    created_at: datetime = Field(default_factory=_utcnow)
    sort_order: int = Field(default=0)


class AgentRunSummary(SQLModel, table=True):
    __tablename__ = "agent_run_summaries"
    id: Optional[int] = Field(default=None, primary_key=True)
    agent_id: int = Field(index=True)
    dag_version: int = Field(default=0)
    trace_id: str = Field(default="", max_length=64, index=True)
    status: str = Field(default="completed", max_length=20)  # completed | error | partial
    total_duration_ms: int = Field(default=0)
    token_input: int = Field(default=0)
    token_output: int = Field(default=0)
    node_count: int = Field(default=0)
    error: str = Field(default="")
    created_at: datetime = Field(default_factory=_utcnow, index=True)


class PlannerSession(SQLModel, table=True):
    __tablename__ = "planner_sessions"
    id: Optional[int] = Field(default=None, primary_key=True)
    conversation_id: str = Field(index=True, unique=True, max_length=64)
    # Explicit ownership boundary. The local single-user frontend omits scope
    # headers and therefore lands in the well-known default scope; production
    # gateways provide verified tenant/workspace/user claims.
    tenant_id: str = Field(default="default", max_length=128, index=True)
    workspace_id: str = Field(default="default", max_length=128, index=True)
    user_id: str = Field(default="default", max_length=128, index=True)
    session_title: str = Field(default="", max_length=200)
    stage: str = Field(default="clarifying", max_length=30)
    user_request: str = Field(default="")
    requirement_summary: str = Field(default="")
    confirmed_constraints: str = Field(default="[]")
    task_classification: str = Field(default="", max_length=50)
    latest_proposal_summary: str = Field(default="")
    planner_messages: str = Field(default="[]")
    file_artifacts: str = Field(default="[]")
    selected_skills: str = Field(default="[]")  # JSON array of skill names mounted to this session
    # "create" (design from scratch) or "replan" (revise an existing agent's architecture).
    mode: str = Field(default="create", max_length=20)
    # Assembled Replan context block (existing graph summary + node configs +
    # planning metadata + optional trace/eval evidence). Injected into the
    # system prompt; empty for create-mode sessions.
    replan_context: str = Field(default="")
    # Richer planner state (enhance-planner-state-schema, design D1). Aggregated
    # JSON storage for the 14 new logical fields, plus query-friendly columns for
    # the decision ledger / architecture pattern / apply-readiness so they can be
    # read without parsing the whole blob. All optional-first with safe defaults
    # so old rows load unchanged.
    planning_state_json: str = Field(default="{}")
    decision_log_json: str = Field(default="[]")
    architecture_pattern: str = Field(default="", max_length=50)
    apply_readiness_json: str = Field(default="{}")
    linked_agent_id: Optional[int] = Field(default=None, index=True)
    created_at: datetime = Field(default_factory=_utcnow)
    last_updated_at: datetime = Field(default_factory=_utcnow, index=True)


# ── Layered Memory core schema (Batch B) ──
# Enums kept as module constants (not DB-level enum types) so the same logical
# contract holds on both SQLite (dev) and PostgreSQL (prod) without binding to a
# physical enum. Validation/normalization happens at the service boundary; the
# columns themselves stay plain strings for cross-DB portability.

MEMORY_TYPES = frozenset({
    "profile_fact", "profile_preference", "agent_constraint",
    "episodic", "semantic", "summary", "system_fact",
})
MEMORY_SCOPES = frozenset({"user", "agent", "agent_user", "workspace"})
MEMORY_STATUSES = frozenset({"active", "superseded", "archived"})
MEMORY_SOURCE_KINDS = frozenset({
    "planner_session", "conversation", "proposal", "run_summary", "evaluation", "manual",
})
MEMORY_LINK_TYPES = frozenset({"derived_from", "summarizes", "supports", "supersedes"})
MEMORY_JOB_TYPES = frozenset({
    "extract_profile", "extract_episodic", "merge_semantic", "compress_summary",
})
MEMORY_JOB_STATUSES = frozenset({"pending", "running", "done", "failed"})


# ── Planning Context source tables (T1: planning-context-aggregation) ──
# Thin "source of record" tables for the planner's unified planning_context.
# Single-user phase: each carries one seeded ``id="default"`` row that the
# aggregator falls back to when no explicit user/workspace is supplied. Kept
# deliberately minimal (design D2) — fields are a subset of the design doc's
# §6.1 / §6.3 logical models; multi-tenant rows can be added later without
# schema change. user_profile is structured master data, NOT memory.


class UserProfile(SQLModel, table=True):
    """Stable identity master-data for the planner's ``user_profile`` segment.

    Read directly (never via semantic recall). ``id`` is a string so the seeded
    single-user row can be the well-known ``"default"`` key."""
    __tablename__ = "user_profiles"
    id: str = Field(default="default", primary_key=True, max_length=64)
    name: str = Field(default="", max_length=200)
    role: str = Field(default="", max_length=100)
    department: str = Field(default="", max_length=100)
    industry: str = Field(default="", max_length=100)
    permissions_json: str = Field(default="[]")  # JSON array of permission strings
    preferences_json: str = Field(default="{}")  # JSON object of structured preferences
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


class WorkspaceContext(SQLModel, table=True):
    """Static / semi-static workspace context for the ``workspace_context``
    segment. One seeded ``id="default"`` row in the single-user phase."""
    __tablename__ = "workspace_contexts"
    id: str = Field(default="default", primary_key=True, max_length=64)
    name: str = Field(default="", max_length=200)
    project_context: str = Field(default="", max_length=2000)
    connected_systems_json: str = Field(default="[]")  # JSON array of system names
    default_entities_json: str = Field(default="[]")  # JSON array of business entities
    default_capability_tags_json: str = Field(default="[]")  # JSON array of capability tags
    updated_at: datetime = Field(default_factory=_utcnow)


class MemoryItem(SQLModel, table=True):
    """Structured metadata for one long-term memory (L4-L8 profile/episodic/
    semantic/summary). Body text lives in mem0 when enabled (``mem0_ref`` set);
    otherwise it falls back to PG ``content``."""
    __tablename__ = "memory_items"
    __table_args__ = (
        Index("ix_memory_items_scope_type_status", "scope", "memory_type", "status"),
        Index("ix_memory_items_agent_status", "agent_id", "status"),
    )
    id: Optional[int] = Field(default=None, primary_key=True)
    agent_id: Optional[int] = Field(default=None)
    user_id: Optional[int] = Field(default=None, index=True)  # null in single-user phase
    conversation_id: Optional[int] = Field(default=None)
    planner_session_id: Optional[int] = Field(default=None)
    memory_type: str = Field(default="semantic", max_length=30)
    scope: str = Field(default="agent", max_length=20)
    content: str = Field(default="")  # body text; empty when stored in mem0
    summary: str = Field(default="", max_length=2000)
    importance: float = Field(default=0.0)
    confidence: float = Field(default=0.0)
    status: str = Field(default="active", max_length=20)
    source_kind: str = Field(default="manual", max_length=30)
    source_ref: str = Field(default="", max_length=200)
    mem0_ref: Optional[str] = Field(default=None, max_length=200)  # mem0 record handle
    last_accessed_at: Optional[datetime] = Field(default=None)
    access_count: int = Field(default=0)
    superseded_by: Optional[int] = Field(default=None)
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


class MemoryLink(SQLModel, table=True):
    """Lineage edge between two memory_items."""
    __tablename__ = "memory_links"
    id: Optional[int] = Field(default=None, primary_key=True)
    from_memory_id: int = Field(index=True)
    to_memory_id: int = Field(index=True)
    link_type: str = Field(default="derived_from", max_length=20)
    created_at: datetime = Field(default_factory=_utcnow)


class MemoryWritebackJob(SQLModel, table=True):
    """Async extract / merge / compress job feeding the long-term memory layer.
    State machine: pending -> running -> (done | failed); failed is re-queueable.
    Enqueue is idempotent on (source_kind, source_ref, job_type)."""
    __tablename__ = "memory_writeback_jobs"
    __table_args__ = (
        Index("ix_memory_writeback_jobs_status_type", "status", "job_type"),
    )
    id: Optional[int] = Field(default=None, primary_key=True)
    source_kind: str = Field(default="manual", max_length=30)
    source_ref: str = Field(default="", max_length=200)
    job_type: str = Field(default="extract_episodic", max_length=30)
    status: str = Field(default="pending", max_length=20)
    payload_json: str = Field(default="{}")
    error: Optional[str] = Field(default=None)
    created_at: datetime = Field(default_factory=_utcnow)
    finished_at: Optional[datetime] = Field(default=None)


# ── Cognitive asset ingestion (T10: agency-agents-minio-ingestion) ──
# Observability for the external-asset import chain (Gitee → MinIO raw → parse/
# classify → expert_template index). One AssetIngestRun per import batch; one
# AssetEvent per file-stage (fetch/store/parse/classify/index/error). JSON-string
# columns + _utcnow, matching the table conventions above.


class AssetIngestRun(SQLModel, table=True):
    """One import batch of an external cognitive-asset repo (e.g. agency-agents).
    State machine: running -> (completed | failed)."""
    __tablename__ = "asset_ingest_runs"
    id: Optional[int] = Field(default=None, primary_key=True)
    source_name: str = Field(default="", max_length=100)  # e.g. "agency-agents"
    source_repo: str = Field(default="", max_length=300)  # gitee.com/owner/repo
    storage: str = Field(default="minio", max_length=20)
    bucket: str = Field(default="", max_length=100)
    prefix: str = Field(default="", max_length=300)
    manifest_key: str = Field(default="", max_length=400)
    source_version: str = Field(default="", max_length=100)  # sync batch / branch / rev
    status: str = Field(default="running", max_length=20)  # running | completed | failed
    summary_json: str = Field(default="{}")  # JSON: counts, timings
    created_at: datetime = Field(default_factory=_utcnow)
    completed_at: Optional[datetime] = Field(default=None)


class AssetEvent(SQLModel, table=True):
    """One observable event in an ingest run (per file, per stage)."""
    __tablename__ = "asset_events"
    __table_args__ = (
        Index("ix_asset_events_run_type", "ingest_run_id", "event_type"),
    )
    id: Optional[int] = Field(default=None, primary_key=True)
    ingest_run_id: Optional[int] = Field(default=None, index=True)
    source_name: str = Field(default="", max_length=100)
    event_type: str = Field(default="", max_length=20)  # fetch|store|parse|classify|index|review|error
    object_key: str = Field(default="", max_length=400)  # MinIO object key (when stored)
    source_path: str = Field(default="", max_length=400)  # path in the source repo
    asset_type: str = Field(default="", max_length=30)  # expert_template|workflow_blueprint|prompt_template|docs
    status: str = Field(default="success", max_length=20)  # success|failed|skipped
    message: str = Field(default="", max_length=2000)
    details_json: str = Field(default="{}")
    created_at: datetime = Field(default_factory=_utcnow)


# ── Proposal / Draft Agent lifecycle (T5: proposal-draft-agent-lifecycle) ──
# First-class planner proposal lifecycle (design doc §6.8-6.10). DISTINCT from
# the legacy ArchitectureProposal (which is a post-apply archive bound to an
# agent_id). These tables stage "propose → confirm → draft → publish" BEFORE a
# production Agent exists; they relate to planner_sessions via conversation_id
# and never touch the existing apply flow. JSON-string columns + _utcnow, per
# the conventions above.


class Proposal(SQLModel, table=True):
    """A planner proposal as a first-class, queryable entity (design §6.8).

    Lifecycle status: draft → confirmed | rejected → applied. The full structured
    ProposalPayload lives in ``proposal_json`` (capability refs only, no raw md)."""
    __tablename__ = "proposals"
    __table_args__ = (
        Index("ix_proposals_conversation_status", "conversation_id", "status"),
    )
    id: Optional[int] = Field(default=None, primary_key=True)
    conversation_id: str = Field(default="", index=True, max_length=64)
    user_goal: str = Field(default="")  # raw user goal text this turn
    inferred_goal: str = Field(default="")  # planner-inferred goal
    task_type: str = Field(default="", max_length=50)
    proposal_json: str = Field(default="{}")  # full ProposalPayload
    selected_expert_template_id: Optional[str] = Field(default=None, max_length=64)
    selected_runtime_mode: str = Field(default="", max_length=30)
    status: str = Field(default="draft", max_length=20)  # draft|confirmed|rejected|applied
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


class ProposalState(SQLModel, table=True):
    """Per-turn interaction state for a proposal (design §6.9). One row per
    proposal (upsert), updated across clarification rounds."""
    __tablename__ = "proposal_states"
    id: Optional[int] = Field(default=None, primary_key=True)
    proposal_id: int = Field(index=True)
    selected_option: str = Field(default="", max_length=200)
    rejected_options_json: str = Field(default="[]")
    confirmed_constraints_json: str = Field(default="[]")
    clarification_answers_json: str = Field(default="[]")
    latest_summary: str = Field(default="")
    updated_at: datetime = Field(default_factory=_utcnow)


class DraftAgent(SQLModel, table=True):
    """Pre-publish staging of an agent derived from a confirmed proposal
    (design §6.10). Editable / testable / publishable. Does NOT create an Agent
    or DAGGraph row — compilation into those is a later stage (T6)."""
    __tablename__ = "draft_agents"
    id: Optional[int] = Field(default=None, primary_key=True)
    proposal_id: int = Field(index=True)
    name: str = Field(default="", max_length=200)
    config_json: str = Field(default="{}")  # derived agent spec (identity/runtime/memory/...)
    capability_refs_json: str = Field(default="[]")  # structured refs (id/type/name), no raw
    runtime_mode: str = Field(default="direct", max_length=30)
    memory_policy_json: str = Field(default="{}")
    status: str = Field(default="draft", max_length=20)  # draft|tested|published
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


# ── Planner run events (T8: planner-run-events) ──
# Persisted, queryable timeline of a planner turn's normalized stages (design
# §6.11). Complements the ephemeral WebSocket ``activity`` stream (which stays
# unchanged) — these rows make the run auditable + the stuck-step explainable.
# JSON-string column + _utcnow, matching the conventions above.


class PlannerRunEvent(SQLModel, table=True):
    """One normalized stage event of a planner run (design §6.11).

    ``run_id`` groups all events of one turn; ``proposal_id`` links the
    proposal-compose stage to its first-class proposal (T5). ``step`` is from the
    normalized turn-loop set; ``status`` is queued|running|completed|failed."""
    __tablename__ = "planner_run_events"
    __table_args__ = (
        Index("ix_planner_run_events_run_proposal", "run_id", "proposal_id"),
        Index("ix_planner_run_events_step_status", "step", "status"),
    )
    id: Optional[int] = Field(default=None, primary_key=True)
    run_id: str = Field(default="", index=True, max_length=64)
    proposal_id: Optional[int] = Field(default=None, index=True)
    step: str = Field(default="", max_length=30)  # intent_inference|context_load|memory_recall|expert_retrieval|capability_match|proposal_compose
    status: str = Field(default="queued", max_length=20)  # queued|running|completed|failed
    message: str = Field(default="", max_length=2000)
    details_json: str = Field(default="{}")
    progress: float = Field(default=0.0)  # 0.0 - 1.0
    created_at: datetime = Field(default_factory=_utcnow)


# ── Memory observability (T9: mem0-observability-and-verification) ──
# Auditable trail of memory operations (design §6.12). Answers the four T9
# questions: was it written / what / recalled in the right place / did it affect
# output. Persisted always; pushed over WS (type "memory_event") when a planner
# socket is live. Mirrors the PlannerRunEvent conventions above.

MEMORY_EVENT_TYPES = frozenset({"write", "recall", "healthcheck", "review"})
MEMORY_EVENT_STATUSES = frozenset({"success", "failed", "skipped", "degraded"})


class MemoryEvent(SQLModel, table=True):
    """One observable memory operation (write / recall / healthcheck / review).

    ``provider`` records which backend served it (mem0 / pgonly / none);
    ``query_or_reason`` captures why a recall ran or what a write was for;
    ``payload_summary`` is a short human-readable digest (no raw transcript)."""
    __tablename__ = "memory_events"
    __table_args__ = (
        Index("ix_memory_events_run_type", "run_id", "event_type"),
        Index("ix_memory_events_source_status", "source", "status"),
    )
    id: Optional[int] = Field(default=None, primary_key=True)
    run_id: str = Field(default="", index=True, max_length=64)
    proposal_id: Optional[int] = Field(default=None, index=True)
    provider: str = Field(default="none", max_length=20)  # mem0|pgonly|none
    event_type: str = Field(default="", max_length=20)  # write|recall|healthcheck|review
    scope: str = Field(default="", max_length=20)  # user|agent|agent_user|workspace
    source: str = Field(default="", max_length=40)  # proposal_confirmation|planner_recall|...
    source_ref: str = Field(default="", max_length=200)
    query_or_reason: str = Field(default="", max_length=2000)
    payload_summary: str = Field(default="", max_length=2000)
    payload_json: str = Field(default="{}")
    status: str = Field(default="success", max_length=20)  # success|failed|skipped|degraded
    created_at: datetime = Field(default_factory=_utcnow)
