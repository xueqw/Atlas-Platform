from datetime import timedelta

import pytest

from app.multi_agent_runtime import (
    DagScheduler, DagTask, WorkerSpec, consume_ticket, effective_tools, issue_ticket,
    utcnow, validate_ticket,
)


def test_worker_contract_and_authority_intersection_are_bounded():
    spec = WorkerSpec("research", "v1", "researcher", "find facts", {}, {}, frozenset({"search", "write"}))
    spec.validate()
    assert effective_tools({"search", "write"}, {"search"}, spec.allowed_tools, {"search", "delete"}) == {"search"}
    assert effective_tools({"search"}, set()) == set()
    with pytest.raises(ValueError):
        WorkerSpec("", "v1", "role", "goal", {}, {}).validate()


def test_ticket_is_parameter_bound_expiring_and_not_replayable():
    now = utcnow()
    ticket = issue_ticket("run", "worker", "send", {"to": "a", "body": "hi"}, now=now, ttl_seconds=10)
    used = set()
    assert validate_ticket(ticket, run_id="run", worker_id="worker", tool_name="send", parameters={"body": "hi", "to": "a"}, used_ticket_ids=used, now=now) == (True, "authorized")
    assert validate_ticket(ticket, run_id="run", worker_id="worker", tool_name="send", parameters={"to": "b", "body": "hi"}, used_ticket_ids=used, now=now) == (False, "parameters_changed")
    consume_ticket(ticket, used)
    assert validate_ticket(ticket, run_id="run", worker_id="worker", tool_name="send", parameters={"to": "a", "body": "hi"}, used_ticket_ids=used, now=now) == (False, "replayed_ticket")
    expired = issue_ticket("run", "worker", "send", {}, now=now, ttl_seconds=1)
    assert validate_ticket(expired, run_id="run", worker_id="worker", tool_name="send", parameters={}, used_ticket_ids=set(), now=now + timedelta(seconds=2)) == (False, "ticket_expired")


def test_dag_dispatches_independent_tasks_with_cap_and_cancels_dependents():
    scheduler = DagScheduler([
        DagTask("a", "one"), DagTask("b", "two"), DagTask("c", "three", {"a"}),
    ], concurrency=2)
    assert [task.task_id for task in scheduler.dispatch()] == ["a", "b"]
    scheduler.complete("a", success=False)
    scheduler.complete("b", success=True)
    assert scheduler.tasks["c"].status == "cancelled"
    assert scheduler.terminal()


def test_dag_retries_and_replan_limit():
    scheduler = DagScheduler([DagTask("a", "one", max_attempts=2)], max_replans=2)
    scheduler.dispatch()
    assert scheduler.complete("a", success=False, retryable=True) == "pending"
    scheduler.dispatch()
    assert scheduler.complete("a", success=True) == "succeeded"
    assert [scheduler.request_replan() for _ in range(3)] == [True, True, False]


def test_worker_cap_rejects_unbounded_team():
    with pytest.raises(ValueError, match="worker cap"):
        DagScheduler([DagTask(str(i), "w") for i in range(7)])
