"""Stable, versioned DTOs and failure policy for the Runtime Foundation."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any
import json
import uuid

from pydantic import BaseModel, ConfigDict, Field, field_validator


RUNTIME_EVENT_SCHEMA = "atlas.runtime-event.v1"
RUNTIME_REQUEST_SCHEMA = "atlas.runtime-request.v1"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class RuntimeSource(str, Enum):
    WORKBENCH = "workbench"
    BUILDER = "builder"
    SUBAGENT = "subagent"
    CHAT = "chat"
    PREVIEW = "preview"
    API = "api"
    EVALUATION = "evaluation"


class RuntimeStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RuntimeErrorCategory(str, Enum):
    TRANSIENT = "transient"
    VALIDATION = "validation"
    PERMISSION = "permission"
    DEPENDENCY = "dependency"
    PLAN = "plan"
    SIDE_EFFECT = "side_effect"
    TERMINAL = "terminal"


class RuntimeContractModel(BaseModel):
    """Reject unknown fields so public Runtime payloads cannot drift silently."""

    model_config = ConfigDict(extra="forbid", strict=True)


class RuntimeIdentity(RuntimeContractModel):
    """Server-resolved identity. A graph may read it but must never replace it."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    workspace_id: str = Field(min_length=1, max_length=128)
    user_id: str = Field(min_length=1, max_length=128)
    agent_id: str = Field(min_length=1, max_length=128)
    agent_version_id: str = Field(min_length=1, max_length=128)
    run_id: str = Field(min_length=1, max_length=128)
    thread_id: str = Field(min_length=1, max_length=300)
    source: RuntimeSource
    conversation_id: str | None = Field(default=None, max_length=128)


class RuntimeStartRequest(RuntimeContractModel):
    schema_version: str = RUNTIME_REQUEST_SCHEMA
    workspace_id: str = Field(min_length=1, max_length=128)
    user_id: str = Field(min_length=1, max_length=128)
    agent_id: str = Field(min_length=1, max_length=128)
    agent_version_id: str = Field(min_length=1, max_length=128)
    source: RuntimeSource
    input: str = Field(min_length=1, max_length=100_000)
    idempotency_key: str = Field(min_length=1, max_length=255)
    conversation_id: str | None = Field(default=None, max_length=128)
    requested_resources: tuple[str, ...] = ()

    @field_validator("requested_resources")
    @classmethod
    def resources_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item for item in value) or len(set(value)) != len(value):
            raise ValueError("requested_resources must contain unique non-empty values")
        return value

    def make_identity(self, run_id: str) -> RuntimeIdentity:
        return RuntimeIdentity(
            workspace_id=self.workspace_id,
            user_id=self.user_id,
            agent_id=self.agent_id,
            agent_version_id=self.agent_version_id,
            run_id=run_id,
            thread_id=f"{self.workspace_id}:{run_id}",
            source=self.source,
            conversation_id=self.conversation_id,
        )


class RuntimeHandle(RuntimeContractModel):
    schema_version: str = RUNTIME_REQUEST_SCHEMA
    run_id: str
    thread_id: str
    status: RuntimeStatus
    execution_mode: str
    created_at: datetime


class RuntimeResumeRequest(RuntimeContractModel):
    run_id: str = Field(min_length=1, max_length=128)
    workspace_id: str = Field(min_length=1, max_length=128)
    user_id: str = Field(min_length=1, max_length=128)


class RuntimeTransition(RuntimeContractModel):
    event_type: str = Field(min_length=1, max_length=128)
    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("payload")
    @classmethod
    def payload_is_json(cls, value: dict[str, Any]) -> dict[str, Any]:
        try:
            json.dumps(value, sort_keys=True, default=str)
        except (TypeError, ValueError) as exc:
            raise ValueError("runtime payload must be JSON serializable") from exc
        return value


class RuntimeEvent(RuntimeContractModel):
    """The only event shape product clients are allowed to consume."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    schema_version: str = RUNTIME_EVENT_SCHEMA
    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    run_id: str = Field(min_length=1)
    workspace_id: str = Field(min_length=1)
    sequence: int = Field(ge=1)
    timestamp: datetime = Field(default_factory=utcnow)
    type: str = Field(min_length=1, max_length=128)
    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("payload")
    @classmethod
    def event_payload_is_json(cls, value: dict[str, Any]) -> dict[str, Any]:
        RuntimeTransition(payload=value, event_type="validation")
        return value


class RuntimeState(RuntimeContractModel):
    identity: RuntimeIdentity
    status: RuntimeStatus
    node_status: dict[str, str] = Field(default_factory=dict)
    retry_counters: dict[str, int] = Field(default_factory=dict)
    errors: tuple[dict[str, Any], ...] = ()
    output: str | None = None
    side_effects_started: bool = False


class RuntimeFailure(Exception):
    def __init__(self, category: RuntimeErrorCategory, message: str, *, retry_after_seconds: float | None = None):
        super().__init__(message)
        self.category = category
        self.retry_after_seconds = retry_after_seconds


class RuntimeAccessDenied(RuntimeFailure):
    def __init__(self) -> None:
        super().__init__(RuntimeErrorCategory.PERMISSION, "runtime resource not available")


def classify_error(error: BaseException) -> RuntimeErrorCategory:
    if isinstance(error, RuntimeFailure):
        return error.category
    if isinstance(error, (TimeoutError, ConnectionError, OSError)):
        return RuntimeErrorCategory.TRANSIENT
    if isinstance(error, PermissionError):
        return RuntimeErrorCategory.PERMISSION
    if isinstance(error, (ValueError, TypeError)):
        return RuntimeErrorCategory.VALIDATION
    status_code = getattr(error, "status_code", None)
    if status_code in {408, 409, 425, 429, 500, 502, 503, 504}:
        return RuntimeErrorCategory.TRANSIENT
    if status_code in {401, 403, 404}:
        return RuntimeErrorCategory.PERMISSION
    return RuntimeErrorCategory.TERMINAL


class RetryPolicy(RuntimeContractModel):
    max_attempts: int = Field(default=3, ge=1, le=8)
    base_delay_seconds: float = Field(default=0.25, ge=0.0, le=60.0)
    max_delay_seconds: float = Field(default=4.0, ge=0.0, le=300.0)

    def delay_for(self, attempt: int) -> float:
        return min(self.max_delay_seconds, self.base_delay_seconds * (2 ** max(0, attempt - 1)))

    def allows(self, category: RuntimeErrorCategory, *, attempt: int, read_only: bool, side_effects_started: bool) -> bool:
        return (
            category is RuntimeErrorCategory.TRANSIENT
            and read_only
            and not side_effects_started
            and attempt < self.max_attempts
        )
