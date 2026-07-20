from pydantic import BaseModel
from typing import Optional, List
from enum import Enum

from app.core.model_caps import DEFAULT_CHAT_MODEL_ID, DEFAULT_CHAT_PROVIDER


class AgentStatusEnum(str, Enum):
    DRAFT = "draft"
    TESTING = "testing"
    PUBLISHED = "published"
    DEPRECATED = "deprecated"


class PromptConfigSchema(BaseModel):
    role_name: str = ""
    role_description: str = ""
    output_format: str = "markdown"
    constraints: str = "[]"
    system_prompt: str = ""


class ModelConfigSchema(BaseModel):
    provider: str = DEFAULT_CHAT_PROVIDER
    model_name: str = DEFAULT_CHAT_MODEL_ID
    temperature: float = 0.7
    max_tokens: int = 4096
    top_p: float = 1.0
    streaming: bool = True


class AgentCreate(BaseModel):
    name: str
    description: str = ""


class AgentUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    prompt_config: Optional[PromptConfigSchema] = None
    model: Optional[ModelConfigSchema] = None


class AgentResponse(BaseModel):
    id: int
    name: str
    description: str
    status: AgentStatusEnum
    version: int
    prompt_config: Optional[PromptConfigSchema] = None
    model: Optional[ModelConfigSchema] = None
    created_at: str
    updated_at: str


class PromptTemplateCreate(BaseModel):
    name: str
    category: str = "general"
    content: str


class PromptTemplateResponse(BaseModel):
    id: int
    name: str
    category: str
    preview: str
    content: str
    created_at: str


class ModelRegistryResponse(BaseModel):
    id: int
    provider: str
    model_id: str
    display_name: str
    capability_tags: str
    context_window: int
    max_output_tokens: int
    input_price_per_1k: float = 0.0
    output_price_per_1k: float = 0.0
    supports_streaming: bool = True
    supports_vision: bool = False
    is_available: bool = True
    # A catalog entry can be enabled while its provider credentials are still
    # absent. Keep that distinction explicit so the UI never presents a model
    # name as proof that it can already be called.
    configured: bool = False


class ConversationResponse(BaseModel):
    id: int
    agent_id: int
    title: str
    created_at: str
    updated_at: str
    last_message_preview: str = ""


class MessageResponse(BaseModel):
    id: int
    conversation_id: int
    role: str
    content: str
    created_at: str


class PublishResponse(BaseModel):
    success: bool
    message: str
    agent_id: int
    status: str


class ReleaseGateResponse(BaseModel):
    passed: bool
    reasons: List[str] = []
    summary: dict = {}
    # Structured failing items (Phase 3): each {code, label, dimension?, actual,
    # threshold, ...}. Parallel to `reasons` (which stays human-readable text for
    # backward compatibility); drives the pre-publish quality report UI.
    failures: List[dict] = []


class PipelineNodeStatusSchema(BaseModel):
    node_name: str
    status: str
    quality_score: Optional[float] = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    trace_url: Optional[str] = None


class PipelineStatusResponse(BaseModel):
    agent_id: int
    nodes: List[PipelineNodeStatusSchema]


class TraceItem(BaseModel):
    trace_id: str
    trace_url: str
    created_at: str


class LatestTracesResponse(BaseModel):
    agent_id: int
    traces: List[TraceItem]


class CapabilityItemCreate(BaseModel):
    type: str  # prompt, model, tool
    name: str
    description: str = ""
    tags: str = "[]"
    config: str = "{}"


class CapabilityItemUpdate(BaseModel):
    type: Optional[str] = None
    name: Optional[str] = None
    description: Optional[str] = None
    tags: Optional[str] = None
    config: Optional[str] = None


class CapabilityItemResponse(BaseModel):
    id: int
    type: str
    name: str
    description: str
    tags: str
    config: str
    created_at: str
    updated_at: str


class PromptVersionResponse(BaseModel):
    id: int
    agent_id: int
    version_number: int
    prompt_config: str
    created_at: str


class ToolConfigCreate(BaseModel):
    name: str
    description: str = ""
    parameters: str = "{}"
    mock_endpoint: str = ""


class ToolConfigUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    parameters: Optional[str] = None
    mock_endpoint: Optional[str] = None


class ToolConfigResponse(BaseModel):
    id: int
    agent_id: int
    name: str
    description: str
    parameters: str
    mock_endpoint: str
    created_at: str
    updated_at: str


# ── DAG Workbench Schemas ──

class DAGGraphCreate(BaseModel):
    graph_json: str = "{}"
    state_schema: str = "{}"


class DAGGraphUpdate(BaseModel):
    graph_json: Optional[str] = None
    state_schema: Optional[str] = None


class DAGGraphResponse(BaseModel):
    id: int
    agent_id: int
    graph_json: str
    state_schema: str
    version: int
    created_at: str
    updated_at: str


class NodeTypeResponse(BaseModel):
    id: int
    node_type: str
    display_name: str
    category: str
    config_schema: str
    input_keys: str
    output_keys: str
    enabled: bool


class HermesRuleResponse(BaseModel):
    id: int
    rule_type: str
    node_type: Optional[str] = None
    condition_expr: str
    severity: str
    message_template: str


class EvaluationResultResponse(BaseModel):
    id: int
    agent_id: int
    dag_version: int
    test_cases: str
    scores: str
    passed: bool
    created_at: str


class DAGTemplateResponse(BaseModel):
    id: int
    name: str
    description: str
    category: str
    graph_json: str
    icon: str
    sort_order: int


class ValidationItem(BaseModel):
    node_id: str
    node_type: str
    rule_type: str
    severity: str
    message: str


class DAGValidateResponse(BaseModel):
    valid: bool
    errors: List[ValidationItem] = []
    warnings: List[ValidationItem] = []


class TestCaseCreate(BaseModel):
    name: str
    input_message: str = ""
    expected_keywords: str = "[]"
    expected_sentiment: Optional[str] = None
    expected_schema: str = "{}"
    judge_prompt: str = ""
    suite_id: Optional[int] = None
    is_key: bool = False
    notes: str = ""
    # Dimension-driven evaluation (Phase 1)
    dimensions_json: str = ""
    reference_output: str = ""
    context_json: str = "{}"
    constraints_json: str = "{}"
    metadata_json: str = "{}"
    # Multi-turn conversation (campus-eval extension)
    turns_json: str = ""  # JSON: [{user_query, expected_answer?}]


class TestCaseUpdate(BaseModel):
    name: Optional[str] = None
    input_message: Optional[str] = None
    expected_keywords: Optional[str] = None
    expected_sentiment: Optional[str] = None
    expected_schema: Optional[str] = None
    judge_prompt: Optional[str] = None
    suite_id: Optional[int] = None
    is_key: Optional[bool] = None
    notes: Optional[str] = None
    # Dimension-driven evaluation (Phase 1)
    dimensions_json: Optional[str] = None
    reference_output: Optional[str] = None
    context_json: Optional[str] = None
    constraints_json: Optional[str] = None
    metadata_json: Optional[str] = None
    # Multi-turn conversation (campus-eval extension)
    turns_json: Optional[str] = None


class TestCaseResponse(BaseModel):
    id: int
    agent_id: int
    suite_id: Optional[int] = None
    name: str
    input_message: str
    expected_keywords: str
    expected_sentiment: Optional[str] = None
    expected_schema: str
    judge_prompt: str
    is_key: bool = False
    notes: str = ""
    sort_order: int
    created_at: str
    # Dimension-driven evaluation (Phase 1)
    dimensions_json: str = ""
    reference_output: str = ""
    context_json: str = "{}"
    constraints_json: str = "{}"
    metadata_json: str = "{}"
    # Multi-turn conversation (campus-eval extension)
    turns_json: str = ""


# ─── Evaluation Suite / Run / Comparison ─────────────────────────────────────


class SuiteCreate(BaseModel):
    name: str
    description: str = ""
    # Dimension-driven evaluation (Phase 1)
    suite_type: str = "general"
    default_dimensions_json: Optional[str] = None  # None → server fills from template
    pass_threshold: float = 0.6


class SuiteUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    # Dimension-driven evaluation (Phase 1)
    suite_type: Optional[str] = None
    default_dimensions_json: Optional[str] = None
    pass_threshold: Optional[float] = None


class SuiteResponse(BaseModel):
    id: int
    agent_id: int
    name: str
    description: str
    created_at: str
    case_count: int = 0
    # Dimension-driven evaluation (Phase 1)
    suite_type: str = "general"
    default_dimensions_json: str = "{}"
    pass_threshold: float = 0.6


class CaseResultResponse(BaseModel):
    id: int
    run_id: int
    case_id: int
    case_name: str
    is_key: bool
    output: str
    scores: str  # JSON
    passed: bool
    trace_id: str
    duration_ms: int
    # Dimension-driven evaluation (Phase 1)
    trace_url: str = ""
    dimension_results: List[dict] = []  # parsed [{dimension,score,passed,threshold,weight,required,reason,evidence,skipped}]
    dimension_results_json: str = "{}"
    evidence_json: str = "{}"
    # Phase 2: Langfuse score writeback evidence. ids = parsed list of score ids.
    langfuse_score_ids: List[str] = []
    langfuse_score_count: int = 0


class RunResponse(BaseModel):
    id: int
    agent_id: int
    suite_id: int
    dag_version: int
    prompt_version: int
    model: str
    trace_ids: str  # JSON array
    summary: str  # JSON
    passed: bool
    created_at: str


class RunDetailResponse(RunResponse):
    case_results: List[CaseResultResponse] = []


class EvaluationRunRequest(BaseModel):
    test_cases: str = "[]"  # JSON array of test cases


class EvaluateResponse(BaseModel):
    passed: bool
    scores: str
    results: str  # JSON: detailed test case results


class CredentialStatusItem(BaseModel):
    name: str
    status: str  # ok | missing | placeholder


class MonitoringResponse(BaseModel):
    agent_id: int
    request_count_24h: int
    request_count_7d: int
    request_count_30d: int
    p50_latency_ms: float
    p95_latency_ms: float
    token_consumption: int
    error_rate: float
    status: str  # healthy, degraded
    # Langfuse health (Phase 2). base_url is the configured host only — never a key.
    langfuse_enabled: bool = False
    langfuse_auth_ok: bool = False
    langfuse_base_url: str = ""
    # Credential health: per-provider + langfuse status (ok|missing|placeholder).
    # NEVER contains a key value — only name + status. Lets the UI attribute a
    # 401/404 to a specific unset/placeholder credential.
    credential_status: List[CredentialStatusItem] = []


class MonitoringTraceItem(BaseModel):
    trace_id: str
    trace_url: str
    duration_ms: float
    status: str
    node_count: int
    created_at: str


class MonitoringTracesResponse(BaseModel):
    agent_id: int
    traces: List[MonitoringTraceItem]
