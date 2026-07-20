"""Adaptive execution contracts, routing policy, and complex-task primitives.

This module keeps all model-authored data behind strict Pydantic contracts. The
router is server-owned: optional model signals can raise complexity but can
never lower a deterministic safety floor.
"""

from __future__ import annotations

from enum import Enum
import inspect
import json
import re
from typing import Any, AsyncIterator, Awaitable, Callable, Literal, TypedDict
import uuid

from pydantic import Field, field_validator, model_validator

from .runtime_contract import (
    ExecutionStrategy,
    RuntimeContractModel,
    RuntimeErrorCategory,
    RuntimeStatus,
    RuntimeTransition,
)
from .runtime_graph import AtlasAgentState, GraphExecutionResult, LANGGRAPH_AVAILABLE

try:  # The manual runner remains available for unit tests and legacy installs.
    from langgraph.graph import END, StateGraph
except ImportError:  # pragma: no cover
    END = "__end__"
    StateGraph = None


ADAPTIVE_POLICY_VERSION = "atlas.strategy-policy.v1"
ADAPTIVE_SCHEMA_VERSION = "v1"
MAX_PLAN_WORKERS = 6
MAX_PLAN_TASKS = 18


class ReviewVerdict(str, Enum):
    PASS = "PASS"
    REVISE = "REVISE"
    REPLAN = "REPLAN"
    REJECT = "REJECT"
    ESCALATE = "ESCALATE"


class StrategySignals(RuntimeContractModel):
    objective_count: int = Field(default=1, ge=1, le=32)
    estimated_steps: int = Field(default=1, ge=1, le=64)
    requires_dependencies: bool = False
    requires_artifact: bool = False
    requires_write: bool = False
    high_risk: bool = False
    parallelizable: bool = False
    specialist_roles: int = Field(default=0, ge=0, le=16)
    ambiguity: float = Field(default=0.0, ge=0.0, le=1.0)

    def merge_safety(self, other: "StrategySignals | None") -> "StrategySignals":
        if other is None:
            return self
        return StrategySignals(
            objective_count=max(self.objective_count, other.objective_count),
            estimated_steps=max(self.estimated_steps, other.estimated_steps),
            requires_dependencies=self.requires_dependencies or other.requires_dependencies,
            requires_artifact=self.requires_artifact or other.requires_artifact,
            requires_write=self.requires_write or other.requires_write,
            high_risk=self.high_risk or other.high_risk,
            parallelizable=self.parallelizable or other.parallelizable,
            specialist_roles=max(self.specialist_roles, other.specialist_roles),
            ambiguity=max(self.ambiguity, other.ambiguity),
        )


class StrategyDecision(RuntimeContractModel):
    requested: ExecutionStrategy
    selected: ExecutionStrategy
    policy_version: str = ADAPTIVE_POLICY_VERSION
    reason_codes: tuple[str, ...]
    score: int = Field(ge=0, le=100)
    confidence: float = Field(ge=0.0, le=1.0)
    signals: StrategySignals

    @model_validator(mode="after")
    def selected_strategy_is_executable(self) -> "StrategyDecision":
        if self.selected is ExecutionStrategy.AUTO:
            raise ValueError("AUTO is not an executable strategy")
        if not self.reason_codes:
            raise ValueError("strategy decision requires at least one reason code")
        return self


class PlannedWorker(RuntimeContractModel):
    worker_id: str = Field(min_length=1, max_length=80)
    role: str = Field(min_length=1, max_length=120)
    objective: str = Field(min_length=1, max_length=2_000)
    input_schema: dict[str, Any] = Field(default_factory=lambda: {"type": "object"})
    output_schema: dict[str, Any] = Field(default_factory=lambda: {"type": "object"})
    allowed_tools: tuple[str, ...] = ()
    max_steps: int = Field(default=8, ge=1, le=32)
    timeout_seconds: int = Field(default=120, ge=1, le=1_800)
    lifecycle: Literal["ephemeral", "session"] = "ephemeral"

    @field_validator("input_schema", "output_schema")
    @classmethod
    def object_schema(cls, value: dict[str, Any]) -> dict[str, Any]:
        if value.get("type") not in {None, "object"}:
            raise ValueError("worker schema root must be object")
        return value


class PlannedTask(RuntimeContractModel):
    task_id: str = Field(min_length=1, max_length=120)
    worker_id: str = Field(min_length=1, max_length=80)
    objective: str = Field(min_length=1, max_length=4_000)
    payload: dict[str, Any] = Field(default_factory=dict)
    depends_on: tuple[str, ...] = ()
    artifact_refs: tuple[str, ...] = ()
    max_attempts: int = Field(default=2, ge=1, le=5)
    timeout_seconds: int = Field(default=120, ge=1, le=1_800)


class PlanEnvelope(RuntimeContractModel):
    schema_version: str = ADAPTIVE_SCHEMA_VERSION
    plan_id: str = Field(default_factory=lambda: str(uuid.uuid4()), min_length=1, max_length=128)
    run_id: str = Field(min_length=1, max_length=128)
    workspace_id: str = Field(min_length=1, max_length=128)
    user_id: str = Field(min_length=1, max_length=128)
    agent_id: str = Field(min_length=1, max_length=128)
    strategy: ExecutionStrategy
    version: int = Field(default=1, ge=1, le=16)
    goal: str = Field(min_length=1, max_length=100_000)
    acceptance_criteria: tuple[str, ...] = Field(min_length=1, max_length=32)
    workers: tuple[PlannedWorker, ...] = Field(min_length=1, max_length=MAX_PLAN_WORKERS)
    tasks: tuple[PlannedTask, ...] = Field(min_length=1, max_length=MAX_PLAN_TASKS)
    max_revision_rounds: int = Field(default=2, ge=0, le=4)
    max_replans: int = Field(default=2, ge=0, le=4)

    @model_validator(mode="after")
    def valid_bounded_dag(self) -> "PlanEnvelope":
        if self.schema_version != ADAPTIVE_SCHEMA_VERSION:
            raise ValueError("unsupported PlanEnvelope schema version")
        if self.strategy not in {
            ExecutionStrategy.PLAN_EXECUTE_REVIEW,
            ExecutionStrategy.MULTI_AGENT_PLAN_EXECUTE_REVIEW,
        }:
            raise ValueError("PlanEnvelope requires a complex execution strategy")
        worker_ids = [item.worker_id for item in self.workers]
        task_ids = [item.task_id for item in self.tasks]
        if len(worker_ids) != len(set(worker_ids)):
            raise ValueError("plan worker IDs must be unique")
        if len(task_ids) != len(set(task_ids)):
            raise ValueError("plan task IDs must be unique")
        known_workers, known_tasks = set(worker_ids), set(task_ids)
        if any(task.worker_id not in known_workers for task in self.tasks):
            raise ValueError("plan task references an unknown worker")
        if any(not set(task.depends_on).issubset(known_tasks) for task in self.tasks):
            raise ValueError("plan task references an unknown dependency")
        if any(task.task_id in task.depends_on for task in self.tasks):
            raise ValueError("plan task cannot depend on itself")
        remaining = {task.task_id: set(task.depends_on) for task in self.tasks}
        while remaining:
            roots = {task_id for task_id, deps in remaining.items() if not deps}
            if not roots:
                raise ValueError("plan dependencies must form an acyclic DAG")
            remaining = {
                task_id: deps - roots
                for task_id, deps in remaining.items()
                if task_id not in roots
            }
        if self.strategy is ExecutionStrategy.PLAN_EXECUTE_REVIEW and len(self.workers) != 1:
            raise ValueError("sequential Plan-Execute-Review requires exactly one worker")
        return self


class ReviewCriterionFinding(RuntimeContractModel):
    criterion: str = Field(min_length=1, max_length=2_000)
    status: Literal["passed", "failed", "uncertain"]
    finding: str = Field(default="", max_length=8_000)
    evidence_refs: tuple[str, ...] = ()


class WorkerResultEnvelope(RuntimeContractModel):
    """Strict logical worker result crossing the parent orchestration boundary."""

    schema_version: str = ADAPTIVE_SCHEMA_VERSION
    envelope_id: str = Field(min_length=1, max_length=128)
    run_id: str = Field(min_length=1, max_length=128)
    task_id: str = Field(min_length=1, max_length=120)
    worker_id: str = Field(min_length=1, max_length=80)
    workspace_id: str = Field(min_length=1, max_length=128)
    user_id: str = Field(min_length=1, max_length=128)
    agent_id: str = Field(min_length=1, max_length=128)
    status: Literal["succeeded", "failed", "cancelled", "timed_out", "paused"]
    result: dict[str, Any] = Field(default_factory=dict)
    artifact_refs: tuple[str, ...] = ()
    error: str = Field(default="", max_length=8_000)
    error_category: str = Field(default="", max_length=80)
    runtime_run_id: str = Field(default="", max_length=128)

    @model_validator(mode="after")
    def result_is_consistent(self) -> "WorkerResultEnvelope":
        if self.schema_version != ADAPTIVE_SCHEMA_VERSION:
            raise ValueError("unsupported WorkerResultEnvelope schema version")
        if self.status == "succeeded" and self.error:
            raise ValueError("successful worker result cannot contain an error")
        return self


class ReviewRequestEnvelope(RuntimeContractModel):
    schema_version: str = ADAPTIVE_SCHEMA_VERSION
    envelope_id: str = Field(default_factory=lambda: str(uuid.uuid4()), min_length=1, max_length=128)
    run_id: str = Field(min_length=1, max_length=128)
    workspace_id: str = Field(min_length=1, max_length=128)
    user_id: str = Field(min_length=1, max_length=128)
    agent_id: str = Field(min_length=1, max_length=128)
    sender_agent_id: str = Field(default="orchestrator", min_length=1, max_length=128)
    reviewer_id: str = Field(min_length=1, max_length=80)
    plan: PlanEnvelope
    results: tuple[WorkerResultEnvelope, ...] = Field(min_length=1, max_length=MAX_PLAN_TASKS)
    reviewed_task_ids: tuple[str, ...] = Field(min_length=1, max_length=MAX_PLAN_TASKS)
    deterministic_findings: tuple[ReviewCriterionFinding, ...] = ()
    artifact_refs: tuple[str, ...] = ()
    review_round: int = Field(default=1, ge=1, le=8)

    @model_validator(mode="after")
    def scope_matches_plan(self) -> "ReviewRequestEnvelope":
        scope = (self.run_id, self.workspace_id, self.user_id, self.agent_id)
        plan_scope = (self.plan.run_id, self.plan.workspace_id, self.plan.user_id, self.plan.agent_id)
        if scope != plan_scope:
            raise ValueError("review request scope must match plan scope")
        task_ids = {task.task_id for task in self.plan.tasks}
        if not set(self.reviewed_task_ids).issubset(task_ids):
            raise ValueError("review request references an unknown task")
        result_task_ids = {item.task_id for item in self.results}
        if len(result_task_ids) != len(self.results) or result_task_ids != set(self.reviewed_task_ids):
            raise ValueError("review request requires exactly one result per reviewed task")
        tasks = {task.task_id: task for task in self.plan.tasks}
        for result in self.results:
            if (
                result.run_id != self.run_id
                or result.workspace_id != self.workspace_id
                or result.user_id != self.user_id
                or result.agent_id != self.agent_id
                or result.worker_id != tasks[result.task_id].worker_id
            ):
                raise PermissionError("worker result scope does not match its planned task")
        if self.reviewer_id in {worker.worker_id for worker in self.plan.workers}:
            raise ValueError("reviewer must be independent from execution workers")
        return self


class ReviewResultEnvelope(RuntimeContractModel):
    schema_version: str = ADAPTIVE_SCHEMA_VERSION
    envelope_id: str = Field(default_factory=lambda: str(uuid.uuid4()), min_length=1, max_length=128)
    request_envelope_id: str = Field(min_length=1, max_length=128)
    run_id: str = Field(min_length=1, max_length=128)
    workspace_id: str = Field(min_length=1, max_length=128)
    user_id: str = Field(min_length=1, max_length=128)
    agent_id: str = Field(min_length=1, max_length=128)
    reviewer_id: str = Field(min_length=1, max_length=80)
    verdict: ReviewVerdict
    criteria: tuple[ReviewCriterionFinding, ...] = Field(min_length=1, max_length=32)
    reviewed_task_ids: tuple[str, ...] = Field(min_length=1, max_length=MAX_PLAN_TASKS)
    required_actions: tuple[str, ...] = ()
    revise_task_ids: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    confidence: float = Field(ge=0.0, le=1.0)

    @model_validator(mode="after")
    def verdict_fields_are_consistent(self) -> "ReviewResultEnvelope":
        if self.schema_version != ADAPTIVE_SCHEMA_VERSION:
            raise ValueError("unsupported ReviewResultEnvelope schema version")
        if self.verdict in {ReviewVerdict.REVISE, ReviewVerdict.REPLAN} and not self.required_actions:
            raise ValueError("revision/replan verdict requires actions")
        if self.verdict is ReviewVerdict.REVISE and not self.revise_task_ids:
            raise ValueError("REVISE requires scoped task IDs")
        return self


class RevisionRequestEnvelope(RuntimeContractModel):
    schema_version: str = ADAPTIVE_SCHEMA_VERSION
    envelope_id: str = Field(default_factory=lambda: str(uuid.uuid4()), min_length=1, max_length=128)
    run_id: str = Field(min_length=1, max_length=128)
    workspace_id: str = Field(min_length=1, max_length=128)
    user_id: str = Field(min_length=1, max_length=128)
    agent_id: str = Field(min_length=1, max_length=128)
    plan_id: str = Field(min_length=1, max_length=128)
    plan_version: int = Field(ge=1, le=16)
    review_envelope_id: str = Field(min_length=1, max_length=128)
    task_ids: tuple[str, ...] = Field(min_length=1, max_length=MAX_PLAN_TASKS)
    required_actions: tuple[str, ...] = Field(min_length=1, max_length=32)
    revision_round: int = Field(ge=1, le=8)


def validate_review_result(
    request: ReviewRequestEnvelope,
    result: ReviewResultEnvelope,
) -> ReviewResultEnvelope:
    expected_scope = (
        request.run_id, request.workspace_id, request.user_id, request.agent_id,
        request.reviewer_id, request.envelope_id,
    )
    supplied_scope = (
        result.run_id, result.workspace_id, result.user_id, result.agent_id,
        result.reviewer_id, result.request_envelope_id,
    )
    if supplied_scope != expected_scope:
        raise PermissionError("review result scope does not match its request")
    execution_workers = {worker.worker_id for worker in request.plan.workers}
    if result.reviewer_id in execution_workers:
        raise PermissionError("execution worker cannot review its own work")
    expected_tasks = set(request.reviewed_task_ids)
    if set(result.reviewed_task_ids) != expected_tasks:
        raise ValueError("review result must cover every requested task exactly")
    if result.verdict is ReviewVerdict.REVISE and not set(result.revise_task_ids).issubset(expected_tasks):
        raise ValueError("review revision references an unknown task")
    expected_criteria = set(request.plan.acceptance_criteria)
    supplied_criteria = {item.criterion for item in result.criteria}
    if supplied_criteria != expected_criteria:
        raise ValueError("review result must cover every acceptance criterion exactly")
    if result.verdict is ReviewVerdict.PASS and any(
        item.status != "passed" for item in request.deterministic_findings
    ):
        raise ValueError("PASS requires every deterministic finding to pass")
    if result.verdict is ReviewVerdict.PASS and any(item.status != "passed" for item in result.criteria):
        raise ValueError("PASS requires every criterion to pass")
    return result


def adaptive_contract_schemas() -> dict[str, dict[str, Any]]:
    return {
        "StrategySignals": StrategySignals.model_json_schema(),
        "StrategyDecision": StrategyDecision.model_json_schema(),
        "PlanEnvelope": PlanEnvelope.model_json_schema(),
        "WorkerResultEnvelope": WorkerResultEnvelope.model_json_schema(),
        "ReviewRequestEnvelope": ReviewRequestEnvelope.model_json_schema(),
        "ReviewResultEnvelope": ReviewResultEnvelope.model_json_schema(),
        "RevisionRequestEnvelope": RevisionRequestEnvelope.model_json_schema(),
        "WorkerAuthorizationRequest": WorkerAuthorizationRequest.model_json_schema(),
        "PlanExecutionBatch": PlanExecutionBatch.model_json_schema(),
    }


def _strict_from_json(model: type[RuntimeContractModel], value: Any):
    """Rehydrate strict contracts from JSON-compatible checkpoint payloads."""
    if isinstance(value, model):
        return value
    return model.model_validate_json(json.dumps(value, ensure_ascii=False))


class TaskStrategyRouter:
    """Deterministic policy with optional model signals that only raise safety."""

    _parallel = re.compile(r"\b(parallel|concurrent|multi[- ]?agent|specialists?|workers?)\b|并行|多智能体|多个智能体|分别处理", re.I)
    _dependency = re.compile(r"\b(depend|after|before|then|workflow|migration|refactor|implement)\b|依赖|然后|之后|迁移|重构|实施|开发", re.I)
    _artifact = re.compile(r"\b(report|document|proposal|design|code|test|release|artifact)\b|报告|文档|方案|设计|代码|测试|发布物|交付物", re.I)
    _write = re.compile(r"\b(create|update|delete|send|publish|deploy|write|modify)\b|创建|更新|删除|发送|发布|部署|写入|修改", re.I)
    _high_risk = re.compile(r"\b(production|payment|credential|delete|publish|deploy|legal|security)\b|生产|付款|凭证|删除|发布|部署|法律|安全", re.I)

    def route(
        self,
        text: str,
        *,
        requested: ExecutionStrategy = ExecutionStrategy.AUTO,
        requested_resources: tuple[str, ...] = (),
        model_signals: StrategySignals | None = None,
    ) -> StrategyDecision:
        signals = self._signals(text, requested_resources).merge_safety(model_signals)
        score, reasons = self._score(signals, text)
        minimum = self._minimum_strategy(signals, score)
        selected = minimum if requested is ExecutionStrategy.AUTO else self._max_strategy(requested, minimum)
        if requested is not ExecutionStrategy.AUTO:
            reasons.append("explicit_strategy_requested")
            if selected is not requested:
                reasons.append("override_raised_by_policy")
        if selected is ExecutionStrategy.MULTI_AGENT_PLAN_EXECUTE_REVIEW:
            reasons.append("multi_agent_execution_required")
        elif selected is ExecutionStrategy.PLAN_EXECUTE_REVIEW:
            reasons.append("reviewed_plan_required")
        else:
            reasons.append("bounded_react_sufficient")
        confidence = min(0.99, 0.55 + min(score, 12) * 0.035)
        return StrategyDecision(
            requested=requested,
            selected=selected,
            reason_codes=tuple(dict.fromkeys(reasons)),
            score=score,
            confidence=confidence,
            signals=signals,
        )

    def _signals(self, text: str, resources: tuple[str, ...]) -> StrategySignals:
        normalized = text.strip()
        enumerated = len(re.findall(r"(?:^|\n)\s*(?:\d+[.)、]|[-*])\s+", normalized))
        conjunctions = len(re.findall(r"以及|并且|同时|然后|\band\b|\bthen\b", normalized, re.I))
        objective_count = max(1, min(32, enumerated or conjunctions + 1))
        resource_write = any(
            "write" in item.lower() or any(word in item.lower() for word in ("delete", "publish", "deploy", "send"))
            for item in resources
        )
        parallel = bool(self._parallel.search(normalized))
        return StrategySignals(
            objective_count=objective_count,
            estimated_steps=max(1, min(64, objective_count + conjunctions + (2 if self._dependency.search(normalized) else 0))),
            requires_dependencies=bool(self._dependency.search(normalized)),
            requires_artifact=bool(self._artifact.search(normalized)),
            requires_write=resource_write or bool(self._write.search(normalized)),
            high_risk=bool(self._high_risk.search(normalized)),
            parallelizable=parallel,
            specialist_roles=2 if parallel else 0,
            ambiguity=0.35 if len(normalized) > 1_200 else 0.1,
        )

    @staticmethod
    def _score(signals: StrategySignals, text: str) -> tuple[int, list[str]]:
        score = 0
        reasons: list[str] = []
        weighted = (
            (signals.objective_count > 1, 2, "multiple_objectives"),
            (signals.estimated_steps >= 4, 2, "multi_step"),
            (signals.requires_dependencies, 3, "task_dependencies"),
            (signals.requires_artifact, 2, "reviewed_artifact"),
            (signals.requires_write, 3, "write_or_side_effect"),
            (signals.high_risk, 4, "high_risk"),
            (signals.parallelizable, 5, "parallel_workstreams"),
            (signals.specialist_roles >= 2, 3, "specialist_roles"),
            (signals.ambiguity >= 0.5, 1, "high_ambiguity"),
            (len(text) > 1_200, 1, "large_request"),
        )
        for enabled, weight, reason in weighted:
            if enabled:
                score += weight
                reasons.append(reason)
        return min(score, 100), reasons

    @staticmethod
    def _minimum_strategy(signals: StrategySignals, score: int) -> ExecutionStrategy:
        if signals.parallelizable and (signals.objective_count > 1 or signals.specialist_roles >= 2):
            return ExecutionStrategy.MULTI_AGENT_PLAN_EXECUTE_REVIEW
        if signals.high_risk or signals.requires_write or signals.requires_dependencies or score >= 4:
            return ExecutionStrategy.PLAN_EXECUTE_REVIEW
        return ExecutionStrategy.REACT

    @staticmethod
    def _max_strategy(requested: ExecutionStrategy, minimum: ExecutionStrategy) -> ExecutionStrategy:
        rank = {
            ExecutionStrategy.REACT: 0,
            ExecutionStrategy.PLAN_EXECUTE_REVIEW: 1,
            ExecutionStrategy.MULTI_AGENT_PLAN_EXECUTE_REVIEW: 2,
        }
        if requested is ExecutionStrategy.AUTO:
            return minimum
        return requested if rank[requested] >= rank[minimum] else minimum


class WorkerAuthorizationRequest(RuntimeContractModel):
    logical_task_id: str = Field(min_length=1, max_length=120)
    orchestration_run_id: str = Field(min_length=1, max_length=128)
    physical_task_id: str = Field(min_length=1, max_length=120)
    worker_id: str = Field(min_length=1, max_length=80)
    tool_name: str = Field(min_length=1, max_length=128)
    parameters: dict[str, Any] = Field(default_factory=dict)
    ticket_id: str = Field(min_length=1, max_length=128)
    nonce: str = Field(min_length=1, max_length=128)
    parameter_digest: str = Field(min_length=1, max_length=128)
    resource_version: str = Field(min_length=1, max_length=128)
    scope: tuple[str, ...] = ()
    expires_at: str = Field(min_length=1, max_length=80)


class PlanExecutionBatch(RuntimeContractModel):
    results: tuple[WorkerResultEnvelope, ...] = Field(min_length=1, max_length=MAX_PLAN_TASKS)
    artifact_refs: tuple[str, ...] = ()
    side_effects_started: bool = False
    authorization_requests: tuple[WorkerAuthorizationRequest, ...] = ()


PlannerInvoker = Callable[
    [AtlasAgentState, ExecutionStrategy, int],
    PlanEnvelope | Awaitable[PlanEnvelope],
]
PlanExecutor = Callable[
    [AtlasAgentState, PlanEnvelope, tuple[str, ...], RevisionRequestEnvelope | None],
    PlanExecutionBatch | Awaitable[PlanExecutionBatch],
]
ReviewerInvoker = Callable[
    [ReviewRequestEnvelope],
    ReviewResultEnvelope | Awaitable[ReviewResultEnvelope],
]
ComplexGraphFactory = Callable[[ExecutionStrategy], "PlanExecuteReviewGraph"]


class _ComplexEnvelope(TypedDict):
    state: dict[str, Any]


class PlanExecuteReviewGraph:
    """Bounded complex-task graph with an independent reviewer boundary.

    The graph owns control flow only. Production injects a durable orchestrator
    executor and a reviewer that runs as its own child RuntimeRun.
    """

    def __init__(
        self,
        planner: PlannerInvoker,
        executor: PlanExecutor,
        reviewer: ReviewerInvoker,
        *,
        strategy: ExecutionStrategy,
        checkpointer: Any | None = None,
        prefer_langgraph: bool = True,
    ) -> None:
        if strategy not in {
            ExecutionStrategy.PLAN_EXECUTE_REVIEW,
            ExecutionStrategy.MULTI_AGENT_PLAN_EXECUTE_REVIEW,
        }:
            raise ValueError("complex graph requires a Plan-Execute-Review strategy")
        self.planner = planner
        self.executor = executor
        self.reviewer = reviewer
        self.strategy = strategy
        self.checkpointer = checkpointer
        self._compiled = self._compile() if prefer_langgraph and LANGGRAPH_AVAILABLE else None

    @property
    def engine(self) -> str:
        return "langgraph" if self._compiled is not None else "legacy-shim"

    async def ainvoke(self, state: AtlasAgentState) -> GraphExecutionResult:
        result = None
        async for result in self.astream(state):
            pass
        if result is None:
            return GraphExecutionResult(state=state, engine=self.engine)
        return result

    async def astream(self, state: AtlasAgentState) -> AsyncIterator[GraphExecutionResult]:
        prepared = self._prepare(state)
        if self._compiled is None:
            async for snapshot in self._manual_stream(prepared):
                yield GraphExecutionResult(state=snapshot, engine=self.engine)
            return
        async for raw in self._compiled.astream(
            {"state": prepared.model_dump(mode="json")},
            config={"configurable": {"thread_id": prepared.identity.thread_id}},
            stream_mode="values",
        ):
            yield GraphExecutionResult(
                state=self._from_json_state(raw["state"]), engine=self.engine,
            )

    async def aresume(self, state: AtlasAgentState) -> GraphExecutionResult:
        result = None
        async for result in self.aresume_stream(state):
            pass
        return result or GraphExecutionResult(state=state, engine=self.engine)

    async def aresume_stream(self, state: AtlasAgentState) -> AsyncIterator[GraphExecutionResult]:
        # Escalation resumes from serialized Atlas state after the actor-bound
        # Runtime interrupt is resolved. Completed LangGraph checkpoints are not
        # replayed with None because the parent state carries the approval bit.
        if state.complex_context.get("escalation_approved"):
            resumed = self._replace(
                state,
                status=RuntimeStatus.RUNNING,
                review_result={},
                complex_context={
                    **state.complex_context,
                    "escalation_approved": False,
                    "next_action": "plan",
                },
            )
            async for item in self.astream(resumed):
                yield item
            return
        if state.complex_context.get("worker_authorization_approved"):
            paused_task_ids = list(state.complex_context.get("paused_task_ids") or ())
            by_task = dict(state.complex_context.get("results_by_task") or {})
            for task_id in paused_task_ids:
                by_task.pop(task_id, None)
            resumed = self._replace(
                state,
                status=RuntimeStatus.RUNNING,
                complex_context={
                    **state.complex_context,
                    "results_by_task": by_task,
                    "worker_authorization_approved": False,
                    "worker_authorization_applied": False,
                    "runtime_interrupt_id": None,
                    "authorization_requests": [],
                    "pending_task_ids": paused_task_ids,
                    "paused_task_ids": [],
                    "next_action": "execute",
                },
            )
            async for item in self.astream(resumed):
                yield item
            return
        if self._compiled is None:
            async for snapshot in self._manual_stream(self._prepare(state)):
                yield GraphExecutionResult(state=snapshot, engine=self.engine)
            return
        async for raw in self._compiled.astream(
            None,
            config={"configurable": {"thread_id": state.identity.thread_id}},
            stream_mode="values",
        ):
            yield GraphExecutionResult(
                state=self._from_json_state(raw["state"]), engine=self.engine,
            )

    def _prepare(self, state: AtlasAgentState) -> AtlasAgentState:
        if state.execution_strategy and state.execution_strategy != self.strategy.value:
            raise ValueError("execution strategy is immutable after selection")
        return self._replace(
            state,
            execution_strategy=self.strategy.value,
            status=RuntimeStatus.RUNNING,
        )

    def _compile(self):
        graph = StateGraph(_ComplexEnvelope)
        for node in ("start", "plan", "prepare_execute", "execute", "create_reviewer", "review", "decide"):
            graph.add_node(node, self._envelope_node(node))
        graph.set_entry_point("start")
        graph.add_conditional_edges(
            "start", self._entry_action,
            {"plan": "plan", "execute": "prepare_execute", "end": END},
        )
        graph.add_conditional_edges("plan", self._route_or_end, {"continue": "prepare_execute", "end": END})
        graph.add_conditional_edges("prepare_execute", self._route_or_end, {"continue": "execute", "end": END})
        graph.add_conditional_edges("execute", self._route_or_end, {"continue": "create_reviewer", "end": END})
        graph.add_conditional_edges("create_reviewer", self._route_or_end, {"continue": "review", "end": END})
        graph.add_conditional_edges("review", self._route_or_end, {"continue": "decide", "end": END})
        graph.add_conditional_edges(
            "decide", self._after_decide,
            {"plan": "plan", "execute": "prepare_execute", "end": END},
        )
        return graph.compile(checkpointer=self.checkpointer)

    def _envelope_node(self, name: str):
        async def node(envelope: _ComplexEnvelope) -> dict[str, Any]:
            state = self._from_json_state(envelope["state"])
            return {"state": (await self._node(name, state)).model_dump(mode="json")}
        return node

    def _after_decide(self, envelope: _ComplexEnvelope) -> str:
        state = self._from_json_state(envelope["state"])
        return str(state.complex_context.get("next_action") or "end")

    def _entry_action(self, envelope: _ComplexEnvelope) -> str:
        state = self._from_json_state(envelope["state"])
        action = str(state.complex_context.get("next_action") or "plan")
        return action if action in {"plan", "execute"} else "end"

    @staticmethod
    def _route_or_end(envelope: _ComplexEnvelope) -> str:
        state = PlanExecuteReviewGraph._from_json_state(envelope["state"])
        return "end" if state.status in {RuntimeStatus.FAILED, RuntimeStatus.CANCELLED, RuntimeStatus.PAUSED} else "continue"

    @staticmethod
    def _from_json_state(value: dict[str, Any]) -> AtlasAgentState:
        """Restore checkpoint JSON through Pydantic's JSON coercion boundary.

        Runtime contracts remain strict for Python callers. LangGraph
        checkpoints are JSON payloads, so enums and tuples must be restored by
        the JSON validator instead of being treated as untrusted Python types.
        """
        return AtlasAgentState.model_validate_json(json.dumps(value))

    async def _manual_stream(self, state: AtlasAgentState) -> AsyncIterator[AtlasAgentState]:
        next_action = str(state.complex_context.get("next_action") or "plan")
        while next_action in {"plan", "execute"}:
            if next_action == "plan":
                state = await self._node("plan", state)
                yield state
                if state.status in {RuntimeStatus.FAILED, RuntimeStatus.CANCELLED, RuntimeStatus.PAUSED}:
                    return
            state = await self._node("prepare_execute", state)
            yield state
            if state.status in {RuntimeStatus.FAILED, RuntimeStatus.CANCELLED, RuntimeStatus.PAUSED}:
                return
            state = await self._node("execute", state)
            yield state
            if state.status in {RuntimeStatus.FAILED, RuntimeStatus.CANCELLED, RuntimeStatus.PAUSED}:
                return
            state = await self._node("create_reviewer", state)
            yield state
            if state.status in {RuntimeStatus.FAILED, RuntimeStatus.CANCELLED, RuntimeStatus.PAUSED}:
                return
            state = await self._node("review", state)
            yield state
            if state.status in {RuntimeStatus.FAILED, RuntimeStatus.CANCELLED, RuntimeStatus.PAUSED}:
                return
            state = await self._node("decide", state)
            yield state
            next_action = str(state.complex_context.get("next_action") or "end")

    async def _start(self, state: AtlasAgentState) -> AtlasAgentState:
        return state

    async def _node(self, name: str, state: AtlasAgentState) -> AtlasAgentState:
        started = self._event(state, "node.started", {"node": name, "strategy": self.strategy.value})
        try:
            handler = getattr(self, f"_{name}")
            result = await handler(started)
            return self._event(result, "node.completed", {"node": name, "status": result.status.value})
        except Exception as exc:
            failed = self._replace(
                started,
                status=RuntimeStatus.FAILED,
                errors=(*started.errors, {
                    "node": name,
                    "category": RuntimeErrorCategory.PLAN.value if name in {"plan", "decide"} else RuntimeErrorCategory.VALIDATION.value,
                    "message": str(exc),
                }),
                complex_context={**started.complex_context, "next_action": "end"},
            )
            failed = self._event(failed, "node.failed", {"node": name, "message": str(exc)})
            return self._event(failed, "run.failed", {"category": "plan", "message": str(exc)})

    async def _plan(self, state: AtlasAgentState) -> AtlasAgentState:
        version = state.plan_version + 1
        raw = self.planner(state, self.strategy, version)
        plan = await raw if inspect.isawaitable(raw) else raw
        plan = PlanEnvelope.model_validate(plan)
        expected_scope = (
            state.identity.run_id, state.identity.workspace_id,
            state.identity.user_id, state.identity.agent_id,
        )
        supplied_scope = (plan.run_id, plan.workspace_id, plan.user_id, plan.agent_id)
        if supplied_scope != expected_scope:
            raise PermissionError("plan scope does not match runtime identity")
        if plan.strategy is not self.strategy or plan.version != version:
            raise ValueError("plan strategy/version does not match selected execution")
        context = {
            **state.complex_context,
            "results_by_task": {},
            "pending_task_ids": [task.task_id for task in plan.tasks],
            "next_action": "execute",
        }
        updated = self._replace(
            state,
            plan=plan.model_dump(mode="json"),
            plan_version=version,
            complex_context=context,
        )
        for worker in plan.workers:
            updated = self._event(updated, "worker.created", {
                "worker_id": worker.worker_id,
                "role": worker.role,
                "lifecycle": worker.lifecycle,
                "allowed_tools": list(worker.allowed_tools),
                "plan_id": plan.plan_id,
                "plan_version": plan.version,
            })
        return self._event(updated, "plan.created", {
            "plan_id": plan.plan_id,
            "version": plan.version,
            "strategy": plan.strategy.value,
            "worker_count": len(plan.workers),
            "task_count": len(plan.tasks),
            "plan": plan.model_dump(mode="json"),
        })

    async def _prepare_execute(self, state: AtlasAgentState) -> AtlasAgentState:
        """Persist a conservative parent boundary before any child write can run."""
        plan = _strict_from_json(PlanEnvelope, state.plan)
        pending = tuple(state.complex_context.get("pending_task_ids") or [task.task_id for task in plan.tasks])
        checker = getattr(self.executor, "requires_side_effect_boundary", None)
        if checker is not None:
            boundary = checker(plan, pending)
            requires_boundary = bool(await boundary) if inspect.isawaitable(boundary) else bool(boundary)
        else:
            selected = {task.task_id: task for task in plan.tasks}
            requires_boundary = any(selected[task_id].payload.get("tool_call") for task_id in pending)
        if not requires_boundary or state.side_effects_started:
            return state
        updated = self._replace(state, side_effects_started=True)
        return self._event(updated, "side_effect.boundary_started", {
            "plan_id": plan.plan_id,
            "plan_version": plan.version,
            "task_ids": list(pending),
            "reason": "declared_write_tool",
        })

    async def _execute(self, state: AtlasAgentState) -> AtlasAgentState:
        plan = _strict_from_json(PlanEnvelope, state.plan)
        pending = tuple(state.complex_context.get("pending_task_ids") or [task.task_id for task in plan.tasks])
        revision_payload = state.complex_context.get("revision_request")
        revision = _strict_from_json(RevisionRequestEnvelope, revision_payload) if revision_payload else None
        raw = self.executor(state, plan, pending, revision)
        batch = await raw if inspect.isawaitable(raw) else raw
        batch = PlanExecutionBatch.model_validate(batch)
        by_task = dict(state.complex_context.get("results_by_task") or {})
        for result in batch.results:
            task_id = result.task_id
            if task_id not in pending:
                raise ValueError("executor returned an unexpected task result")
            by_task[task_id] = result.model_dump(mode="json")
        missing = set(pending) - set(by_task)
        if missing:
            raise ValueError(f"executor omitted task results: {sorted(missing)}")
        context = {
            **state.complex_context,
            "results_by_task": by_task,
            "artifact_refs": sorted(set(state.complex_context.get("artifact_refs") or ()) | set(batch.artifact_refs)),
            "pending_task_ids": [],
            "revision_request": None,
            "next_action": "review",
            "authorization_requests": [
                item.model_dump(mode="json") for item in batch.authorization_requests
            ],
            "worker_authorizations": {
                task_id: authorization
                for task_id, authorization in dict(
                    state.complex_context.get("worker_authorizations") or {}
                ).items()
                if task_id not in pending
            },
        }
        updated = self._replace(
            state,
            complex_context=context,
            side_effects_started=state.side_effects_started or batch.side_effects_started,
        )
        for task_id in pending:
            item = by_task[task_id]
            updated = self._event(updated, "worker.completed", {
                "task_id": task_id,
                "worker_id": item.get("worker_id"),
                "status": item.get("status"),
                "runtime_run_id": item.get("runtime_run_id", ""),
            })
        paused_tasks = [task_id for task_id in pending if by_task[task_id].get("status") == "paused"]
        if paused_tasks:
            if set(paused_tasks) != {item.logical_task_id for item in batch.authorization_requests}:
                raise ValueError("paused write tasks require bound authorization requests")
            updated = self._replace(
                updated,
                status=RuntimeStatus.PAUSED,
                complex_context={
                    **updated.complex_context,
                    "next_action": "end",
                    "paused_task_ids": paused_tasks,
                },
            )
            return self._event(updated, "run.paused", {
                "reason": "worker_authorization_required",
                "task_ids": paused_tasks,
                "authorization_count": len(batch.authorization_requests),
            })
        return updated

    async def _create_reviewer(self, state: AtlasAgentState) -> AtlasAgentState:
        plan = _strict_from_json(PlanEnvelope, state.plan)
        reviewer_id = f"reviewer-{state.review_round + 1}"
        by_task = dict(state.complex_context.get("results_by_task") or {})
        task_ids = tuple(task.task_id for task in plan.tasks)
        findings = tuple(
            ReviewCriterionFinding(
                criterion=criterion,
                status="passed" if all(by_task.get(task_id, {}).get("status") == "succeeded" for task_id in task_ids) else "failed",
                finding="deterministic result-envelope status check",
            )
            for criterion in plan.acceptance_criteria
        )
        request = ReviewRequestEnvelope(
            run_id=state.identity.run_id,
            workspace_id=state.identity.workspace_id,
            user_id=state.identity.user_id,
            agent_id=state.identity.agent_id,
            reviewer_id=reviewer_id,
            plan=plan,
            results=tuple(
                _strict_from_json(WorkerResultEnvelope, by_task[task_id])
                for task_id in task_ids
            ),
            reviewed_task_ids=task_ids,
            deterministic_findings=findings,
            artifact_refs=tuple(state.complex_context.get("artifact_refs") or ()),
            review_round=state.review_round + 1,
        )
        context = {**state.complex_context, "review_request": request.model_dump(mode="json")}
        updated = self._replace(state, complex_context=context)
        updated = self._event(updated, "worker.created", {
            "worker_id": reviewer_id, "role": "reviewer", "lifecycle": "ephemeral", "allowed_tools": [],
        })
        return self._event(updated, "review.requested", {
            "reviewer_id": reviewer_id,
            "request_envelope_id": request.envelope_id,
            "review_round": request.review_round,
            "task_count": len(request.reviewed_task_ids),
        })

    async def _review(self, state: AtlasAgentState) -> AtlasAgentState:
        request = _strict_from_json(ReviewRequestEnvelope, state.complex_context["review_request"])
        raw = self.reviewer(request)
        result = await raw if inspect.isawaitable(raw) else raw
        parsed = result if isinstance(result, ReviewResultEnvelope) else _strict_from_json(ReviewResultEnvelope, result)
        result = validate_review_result(request, parsed)
        updated = self._replace(
            state,
            review_round=state.review_round + 1,
            review_result=result.model_dump(mode="json"),
        )
        return self._event(updated, "review.completed", {
            "reviewer_id": result.reviewer_id,
            "review_envelope_id": result.envelope_id,
            "verdict": result.verdict.value,
            "confidence": result.confidence,
            "review_round": updated.review_round,
        })

    async def _decide(self, state: AtlasAgentState) -> AtlasAgentState:
        review = _strict_from_json(ReviewResultEnvelope, state.review_result)
        plan = _strict_from_json(PlanEnvelope, state.plan)
        context = dict(state.complex_context)
        if review.verdict is ReviewVerdict.PASS:
            context["next_action"] = "end"
            output = json.dumps({
                "strategy": self.strategy.value,
                "plan_id": plan.plan_id,
                "plan_version": plan.version,
                "results": [context["results_by_task"][task.task_id] for task in plan.tasks],
                "review": review.model_dump(mode="json"),
            }, ensure_ascii=False, sort_keys=True)
            completed = self._replace(state, status=RuntimeStatus.SUCCEEDED, output=output, complex_context=context)
            return self._event(completed, "run.completed", {
                "output": output,
                "strategy": self.strategy.value,
                "review_verdict": review.verdict.value,
            })
        if review.verdict is ReviewVerdict.REJECT:
            return self._terminal_failure(state, "review rejected the execution", "review_rejected")
        if review.verdict is ReviewVerdict.ESCALATE:
            context["next_action"] = "end"
            paused = self._replace(state, status=RuntimeStatus.PAUSED, complex_context=context)
            return self._event(paused, "run.escalated", {
                "review_envelope_id": review.envelope_id,
                "required_actions": list(review.required_actions),
            })
        if review.verdict is ReviewVerdict.REVISE:
            if state.revision_round >= plan.max_revision_rounds:
                return self._terminal_failure(state, "revision budget exhausted", "revision_budget_exhausted")
            revision_round = state.revision_round + 1
            revision = RevisionRequestEnvelope(
                run_id=state.identity.run_id,
                workspace_id=state.identity.workspace_id,
                user_id=state.identity.user_id,
                agent_id=state.identity.agent_id,
                plan_id=plan.plan_id,
                plan_version=plan.version,
                review_envelope_id=review.envelope_id,
                task_ids=review.revise_task_ids,
                required_actions=review.required_actions,
                revision_round=revision_round,
            )
            context.update({
                "next_action": "execute",
                "pending_task_ids": list(revision.task_ids),
                "revision_request": revision.model_dump(mode="json"),
            })
            revised = self._replace(state, revision_round=revision_round, complex_context=context)
            return self._event(revised, "review.revision_requested", {
                "revision_round": revision_round,
                "task_ids": list(revision.task_ids),
                "required_actions": list(revision.required_actions),
            })
        if state.replan_count >= plan.max_replans:
            return self._terminal_failure(state, "replan budget exhausted", "replan_budget_exhausted")
        context.update({"next_action": "plan", "pending_task_ids": [], "revision_request": None})
        replanned = self._replace(state, replan_count=state.replan_count + 1, complex_context=context)
        return self._event(replanned, "review.replan_requested", {
            "replan_count": replanned.replan_count,
            "required_actions": list(review.required_actions),
        })

    def _terminal_failure(self, state: AtlasAgentState, message: str, reason: str) -> AtlasAgentState:
        context = {**state.complex_context, "next_action": "end", "terminal_reason": reason}
        failed = self._replace(
            state,
            status=RuntimeStatus.FAILED,
            errors=(*state.errors, {"category": RuntimeErrorCategory.PLAN.value, "message": message, "reason": reason}),
            complex_context=context,
        )
        return self._event(failed, "run.failed", {"category": "plan", "message": message, "reason": reason})

    @staticmethod
    def _replace(state: AtlasAgentState, **changes: Any) -> AtlasAgentState:
        data = state.model_dump()
        data.update(changes)
        return AtlasAgentState.model_validate(data)

    @classmethod
    def _event(cls, state: AtlasAgentState, event_type: str, payload: dict[str, Any]) -> AtlasAgentState:
        return cls._replace(
            state,
            transitions=(*state.transitions, RuntimeTransition(event_type=event_type, payload=payload)),
        )


class AdaptiveRuntimeRunner:
    """Route once, persist the decision, then delegate to a versioned graph."""

    def __init__(
        self,
        react_graph: Any,
        complex_factory: ComplexGraphFactory,
        *,
        router: TaskStrategyRouter | None = None,
        model_signals: Callable[[AtlasAgentState], StrategySignals | Awaitable[StrategySignals | None] | None] | None = None,
    ) -> None:
        self.react_graph = react_graph
        self.complex_factory = complex_factory
        self.router = router or TaskStrategyRouter()
        self.model_signals = model_signals

    @property
    def engine(self) -> str:
        return "adaptive-runtime-v1"

    async def ainvoke(self, state: AtlasAgentState) -> GraphExecutionResult:
        result = None
        async for result in self.astream(state):
            pass
        return result or GraphExecutionResult(state=state, engine=self.engine)

    async def astream(self, state: AtlasAgentState) -> AsyncIterator[GraphExecutionResult]:
        selected = state
        if not selected.execution_strategy:
            extra = None
            if self.model_signals is not None:
                raw = self.model_signals(selected)
                extra = await raw if inspect.isawaitable(raw) else raw
                if extra is not None:
                    extra = StrategySignals.model_validate(extra)
            requested = ExecutionStrategy(selected.requested_execution_strategy)
            decision = self.router.route(
                selected.input,
                requested=requested,
                requested_resources=selected.requested_resources,
                model_signals=extra,
            )
            selected = PlanExecuteReviewGraph._replace(
                selected,
                execution_strategy=decision.selected.value,
                strategy_decision=decision.model_dump(mode="json"),
            )
            selected = PlanExecuteReviewGraph._event(selected, "strategy.selected", {
                "requested": decision.requested.value,
                "selected": decision.selected.value,
                "policy_version": decision.policy_version,
                "reason_codes": list(decision.reason_codes),
                "score": decision.score,
                "confidence": decision.confidence,
            })
            yield GraphExecutionResult(state=selected, engine=self.engine)
        async for result in self._stream_selected(selected, resume=False):
            yield result

    async def aresume(self, state: AtlasAgentState) -> GraphExecutionResult:
        result = None
        async for result in self.aresume_stream(state):
            pass
        return result or GraphExecutionResult(state=state, engine=self.engine)

    async def aresume_stream(self, state: AtlasAgentState) -> AsyncIterator[GraphExecutionResult]:
        if not state.execution_strategy:
            async for result in self.astream(state):
                yield result
            return
        async for result in self._stream_selected(state, resume=True):
            yield result

    async def _stream_selected(
        self,
        state: AtlasAgentState,
        *,
        resume: bool,
    ) -> AsyncIterator[GraphExecutionResult]:
        strategy = ExecutionStrategy(state.execution_strategy)
        graph = self.react_graph if strategy is ExecutionStrategy.REACT else self.complex_factory(strategy)
        method = getattr(graph, "aresume_stream" if resume else "astream")
        async for result in method(state):
            yield result
