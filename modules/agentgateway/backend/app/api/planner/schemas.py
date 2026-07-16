"""Pydantic request/response models for the planner API."""

from datetime import datetime
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field

from app.core.model_caps import DEFAULT_CHAT_MODEL_ID


class StartRequest(BaseModel):
    model_id: str = DEFAULT_CHAT_MODEL_ID


class StartResponse(BaseModel):
    conversation_id: str
    greeting: str
    resolved_model: str


class ApplyRequest(BaseModel):
    proposal: dict
    agent_name: str
    memory: dict = {}
    conversation_id: Optional[str] = None


class ApplyReplanRequest(BaseModel):
    """Land a Replan proposal onto an existing agent.

    ``landing`` selects one of three modes (design D3):
      - "save_new_version": write v(N+1) as latest, keep old versions (default)
      - "override_draft":  override the current latest DAG version in place
      - "partial":         merge only ``selected_node_ids`` into the existing graph
    """
    agent_id: int
    proposal: dict
    memory: dict = {}
    conversation_id: Optional[str] = None
    landing: str = "save_new_version"
    selected_node_ids: List[str] = []


class PlannerSessionListItem(BaseModel):
    conversation_id: str
    session_title: str
    stage: str
    linked_agent_id: Optional[int]
    last_updated_at: datetime
    created_at: datetime


class PlannerSessionDetail(BaseModel):
    conversation_id: str
    session_title: str
    stage: str
    user_request: str
    memory: dict
    messages: List[dict]
    file_artifacts: List[dict]
    linked_agent_id: Optional[int]
    mode: str = "create"
    replan_context: str = ""
    last_updated_at: datetime
    created_at: datetime


class FromAgentResponse(BaseModel):
    conversation_id: str
    session_title: str
    mode: str = "replan"


class AttachmentMeta(BaseModel):
    path: str
    kind: str
    mime: str
    name: str
    size: int
    preview_url: str



class PlannerFileItem(BaseModel):
    path: str
    filename: str
    kind: str
    size: int
    updated_at: str



class PlannerSkillItem(BaseModel):
    name: str
    description: str
    tags: List[str]
    source: str
    entrypoint: str
    attached_to_current: Optional[bool] = None
    is_default: bool = False
    # Three-tier contract fields (planner-effective-skills). Derived from
    # resolve_effective_skills(), never persisted.
    #   default_source: which default tier this skill belongs to.
    #   effective_in_current: whether it is in the session's effective_skills.
    #   lock_reason: why a default skill cannot be detached (empty for user skills).
    default_source: str = "none"  # "system" | "workspace" | "none"
    effective_in_current: Optional[bool] = None
    lock_reason: Optional[str] = None


class ApplyReadiness(BaseModel):
    """Optional apply-readiness block carried on a proposal / in memory.

    optional-first: status defaults to ``not_ready`` so an absent block is
    treated as "not ready" rather than failing (freeze contract)."""
    status: str = "not_ready"
    missing: List[str] = []
    recommendation: str = ""


class AgentSpec(BaseModel):
    """Optional agent_spec carried on a proposal (freeze contract D5).

    All six sub-structures are optional dicts so a partial spec validates; the
    DAG nodes/edges projection remains the authoritative landing source."""
    identity: dict = {}
    runtime: dict = {}
    memory: dict = {}
    knowledge: dict = {}
    evaluation: dict = {}
    rollout: dict = {}


class ProposalDecision(BaseModel):
    """One structured decision-ledger entry (decisions_confirmed)."""
    id: str
    topic: str = ""
    prompt: str = ""
    options: List[dict] = []
    selected_option: dict = {}
    free_text: str = ""
    status: str = "confirmed"
    effect_on_plan: str = ""


class PlannerSnapshotResponse(BaseModel):
    requirement_summary: str
    task_classification: str
    confirmed_constraints: List[str]
    rationale: str
    user_feedback_summary: List[str]
    proposal_version: int
    created_at: datetime


# ── Planning Context schema (T1: planning-context-aggregation) ──
# The unified object the planner consumes (design doc §3 / §7.1). Seven
# segments; every segment is always present (empty/default form when no data),
# so the shape is stable for both prompt injection and the read-only API.
# Each sub-model is optional-first — unknown fields are tolerated and MUST
# fields carry safe defaults so a partial source never breaks aggregation.


class UserInput(BaseModel):
    """This turn's explicit user input (planning_context.user_input)."""
    goal_text: str = ""
    attachments: List[Dict[str, Any]] = []
    explicit_constraints: List[str] = []
    preferred_delivery: List[str] = []


class UserProfileContext(BaseModel):
    """Stable identity master-data (planning_context.user_profile).

    Sourced from the user_profiles table, NOT from memory recall."""
    user_id: str = "default"
    name: str = ""
    role: str = ""
    department: str = ""
    industry: str = ""
    permissions: List[str] = []
    preferences: Dict[str, Any] = {}


class MemoryProviderStatus(BaseModel):
    provider: str = "none"
    enabled: bool = False
    healthy: Optional[bool] = None


class MemoryContext(BaseModel):
    """Lightweight, layered historical context (planning_context.memory_context).

    NOT raw chat history; no heavy semantic retrieval at T1 — just structured
    placeholders sourced from the existing layered-memory store."""
    preference_memory: List[Dict[str, Any]] = []
    recent_activity_summary: List[str] = []
    memory_provider_status: MemoryProviderStatus = Field(default_factory=MemoryProviderStatus)


class WorkspaceContextSegment(BaseModel):
    """Workspace static/semi-static context (planning_context.workspace_context)."""
    workspace_id: str = "default"
    workspace_name: str = ""
    project_context: str = ""
    connected_systems: List[str] = []
    default_entities: List[str] = []
    default_capability_tags: List[str] = []


class ExternalSystem(BaseModel):
    """One external system abstracted into planner-understandable entities and
    operations (not just "what is connected")."""
    system: str = ""
    type: str = ""
    entities: List[str] = []
    operations: List[str] = []
    access_scope: str = "workspace"


class ExternalContext(BaseModel):
    """External integration surface (planning_context.external_context).

    T1: no binding source yet → ``systems`` is an empty list (key still present)."""
    systems: List[ExternalSystem] = []


class CapabilityRef(BaseModel):
    """Structured capability reference — id/type/name only, never raw markdown."""
    id: Optional[int] = None
    type: str = ""
    name: str = ""


class InternalContext(BaseModel):
    """Platform capabilities & cognitive assets (planning_context.internal_context).

    ``capabilities`` carries only structured refs (id/type/name); never raw
    content. ``expert_templates`` / ``workflow_blueprints`` stay empty at T1."""
    capabilities: List[CapabilityRef] = []
    runtime_modes: List[str] = []
    expert_templates: List[Dict[str, Any]] = []
    workflow_blueprints: List[str] = []


class PolicyContext(BaseModel):
    """Planning governance boundaries (planning_context.policy_context).

    Defaults embody the plan-first stance: minimal clarification, draft before
    publish."""
    clarification_policy: str = "minimal"
    publish_policy: str = "draft_before_publish"
    memory_policy_defaults: Dict[str, Any] = {}
    runtime_restrictions: List[str] = []


class PlanningContext(BaseModel):
    """The unified planning_context object consumed by the planner. All seven
    segments are always present; each defaults to its empty/default form."""
    user_input: UserInput = Field(default_factory=UserInput)
    user_profile: UserProfileContext = Field(default_factory=UserProfileContext)
    memory_context: MemoryContext = Field(default_factory=MemoryContext)
    workspace_context: WorkspaceContextSegment = Field(default_factory=WorkspaceContextSegment)
    external_context: ExternalContext = Field(default_factory=ExternalContext)
    internal_context: InternalContext = Field(default_factory=InternalContext)
    policy_context: PolicyContext = Field(default_factory=PolicyContext)


class PlanningContextResponse(BaseModel):
    """Read-only API envelope: ``{"planning_context": {...}}``."""
    planning_context: PlanningContext


class PlanningContextRequest(BaseModel):
    """Optional body for the read-only planning-context endpoint."""
    conversation_id: Optional[str] = None
    user_id: Optional[str] = None
    workspace_id: Optional[str] = None
    user_input: Optional[UserInput] = None


# ── Plan-first structured output (T2: planner-proposal-flow) ──
# The planner's fixed structured intermediate output and the Proposal Schema it
# carries. Validated after extraction (design D2/D5): every field is present in
# the model (schema completeness = the "MUST present" contract) with safe
# defaults, so a partial-but-well-formed output validates; genuine format drift
# (wrong types / non-dict proposal) fails validation and the turn degrades.


class ProposalPayload(BaseModel):
    """The structured Proposal Schema (design doc §7.3).

    ``recommended_capabilities`` are structured refs (id/type/name reuse the T1
    CapabilityRef) — never raw markdown / prompt text. ``primary_expert_template_id``
    is present but stays None until T3 lands expert templates."""
    title: str = ""
    goal: str = ""
    primary_expert_template_id: Optional[str] = None
    recommended_capabilities: List[CapabilityRef] = []
    runtime_mode: str = "direct"
    deliverables: List[str] = []
    risks: List[str] = []
    context_used_explanation: List[str] = []

    model_config = {"extra": "allow"}  # tolerate existing proposal keys (nodes/edges/agent_spec/...)


class PlannerIntermediate(BaseModel):
    """The planner's fixed structured intermediate output (design doc §7.2).

    Wraps the Proposal Schema plus the plan-first decision fields. ``expert_candidates``
    is present but empty at T2 (real retrieval arrives in T3); ``capability_candidates``
    uses the minimal heuristic match."""
    inferred_goal: str = ""
    task_type: str = ""
    confidence: float = 0.0
    missing_critical_info: List[str] = []
    expert_candidates: List[Dict[str, Any]] = []
    capability_candidates: List[CapabilityRef] = []
    recommended_runtime_mode: str = "direct"
    clarification_required: bool = False
    context_used_explanation: List[str] = []
    proposal: Optional[ProposalPayload] = None
