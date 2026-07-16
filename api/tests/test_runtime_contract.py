import pytest
from pydantic import ValidationError

from app.runtime_contract import (
    RetryPolicy, RuntimeErrorCategory, RuntimeIdentity, RuntimeSource, RuntimeStartRequest, classify_error,
)


def request() -> RuntimeStartRequest:
    return RuntimeStartRequest(
        workspace_id="ws", user_id="user", agent_id="agent", agent_version_id="v1",
        source=RuntimeSource.CHAT, input="hello", idempotency_key="key",
    )


def test_versioned_request_rejects_unknown_fields_and_identity_is_frozen():
    with pytest.raises(ValidationError):
        RuntimeStartRequest(**request().model_dump(), unexpected=True)
    identity = request().make_identity("run-1")
    assert identity.thread_id == "ws:run-1"
    with pytest.raises(ValidationError):
        identity.workspace_id = "other"


def test_retry_policy_only_retries_transient_read_only_work():
    policy = RetryPolicy(max_attempts=3, base_delay_seconds=0)
    assert classify_error(TimeoutError()) is RuntimeErrorCategory.TRANSIENT
    assert policy.allows(RuntimeErrorCategory.TRANSIENT, attempt=1, read_only=True, side_effects_started=False)
    assert not policy.allows(RuntimeErrorCategory.TRANSIENT, attempt=3, read_only=True, side_effects_started=False)
    assert not policy.allows(RuntimeErrorCategory.TRANSIENT, attempt=1, read_only=False, side_effects_started=False)
    assert not policy.allows(RuntimeErrorCategory.PERMISSION, attempt=1, read_only=True, side_effects_started=False)
