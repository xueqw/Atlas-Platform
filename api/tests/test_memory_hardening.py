import json

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.governance_models import (
    EpisodicEvidence, GovernanceBase, LedgerEvent, ProceduralCandidate,
)
from app.models import Skill
from app.memory_experience import consolidate_successful_trajectory
from app.memory_ledger import MemoryScope, record_episodic_evidence
from app.memory_session import DurableSessionContextStore
from app.multi_agent_repository import OrchestrationScope
from app.multi_agent_service import (
    CandidateAssetPublisher, DurableOrchestrator, TaskPlan,
    deterministic_candidate_replay,
)
from app.multi_agent_runtime import WorkerSpec


class FakeRedis:
    def __init__(self): self.data = {}
    def get(self, key): return self.data.get(key)
    def setex(self, key, ttl, value): self.data[key] = value
    def delete(self, key): self.data.pop(key, None)


@pytest.fixture()
def sessions(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'memory-hardening.db'}")
    Base.metadata.create_all(engine)
    GovernanceBase.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def test_session_context_survives_redis_loss_and_rebuilds_from_ledger(sessions):
    redis = FakeRedis()
    store = DurableSessionContextStore(sessions, redis)
    scope = MemoryScope("ws", "user", "agent", "run")
    assert store.write(scope=scope, session_id="chat", snapshot={"turn": 1},
                       expected_version=0, idempotency_key="session-1")["version"] == 1
    redis.data.clear()
    assert store.read(scope=scope, session_id="chat") == {"version": 1, "snapshot": {"turn": 1}}
    assert redis.data
    with sessions() as db:
        assert db.scalar(select(LedgerEvent).where(
            LedgerEvent.event_type == "memory.session.checkpointed")) is not None


def test_repeated_success_creates_draft_only_skill_candidate(sessions):
    with sessions() as db:
        candidate = None
        for index in range(3):
            scope = MemoryScope("ws", "user", "agent", f"run-{index}")
            _, candidate = consolidate_successful_trajectory(
                db, scope=scope, task_type="refund", trajectory={"step": index},
                metrics={"ok": True}, idempotency_key=f"episode-{index}",
                redaction_status="redacted", minimum_examples=3,
            )
        db.commit()
        assert candidate is not None
        assert candidate.candidate_type == "skill" and candidate.status == "candidate"
        assert candidate.published_at is None and candidate.content["auto_generated"] is True


def test_repeated_success_replay_approval_publishes_skill_and_bad_fixture_fails(sessions):
    """The autonomous path reaches a real Skill only through every governance gate."""
    with sessions() as db:
        worker = WorkerSpec("worker", "1", "refund", "handle refund", {"type": "object"},
                            {"type": "object"}, frozenset())
        current = OrchestrationScope("ws", "user", "agent", "run-2")
        DurableOrchestrator(db).create(
            current, workers=[worker], tasks=[TaskPlan("task", "worker", {})]
        )
        candidate = None
        evidence_ids = []
        for index in range(3):
            evidence, candidate = consolidate_successful_trajectory(
                db,
                scope=MemoryScope("ws", "user", "agent", f"run-{index}", "worker"),
                task_type="refund",
                trajectory={
                    "task_id": f"refund-{index}",
                    "worker_id": "worker",
                    "runtime_run_id": f"runtime-{index}",
                    "status": "succeeded",
                    # Business output is deliberately not copied into the fixture.
                    "business_output": {"customer": f"customer-{index}"},
                },
                metrics={"successful": True},
                idempotency_key=f"closed-loop-{index}",
                redaction_status="redacted",
                minimum_examples=3,
            )
            evidence_ids.append(evidence.evidence_id)
            fixture = evidence.trajectory["replay_fixture"]
            assert fixture["input_schema"]["type"] == "object"
            assert fixture["output_schema"]["type"] == "object"
            assert "business_output" not in fixture["input"]
            assert fixture["expected_output"] == {
                "task_type": "refund", "status": "succeeded",
            }

        assert candidate is not None
        assert candidate.evidence_ids == evidence_ids
        assert candidate.content["replay_program"]["operation"] == "template"
        publisher = CandidateAssetPublisher(db, replay_executor=deterministic_candidate_replay)
        publisher.redact(
            current, candidate.candidate_id,
            redacted_content=dict(candidate.content), actor_id="redactor",
        )

        # A persisted fixture mismatch must fail closed, regardless of client annotation.
        bad_evidence = db.get(EpisodicEvidence, evidence_ids[0])
        original_trajectory = dict(bad_evidence.trajectory)
        bad_trajectory = dict(original_trajectory)
        bad_fixture = dict(bad_trajectory["replay_fixture"])
        bad_fixture["expected_output"] = {"task_type": "refund", "status": "failed"}
        bad_trajectory["replay_fixture"] = bad_fixture
        bad_evidence.trajectory = bad_trajectory
        db.commit()
        with pytest.raises(ValueError, match="pass replay"):
            publisher.evaluate(
                current, candidate.candidate_id,
                replay_report={"replay_pass_rate": 1.0}, actor_id="evaluator",
            )

        bad_evidence.trajectory = original_trajectory
        db.commit()
        evaluated = publisher.evaluate(
            current, candidate.candidate_id,
            replay_report={"replay_pass_rate": 0.0}, actor_id="evaluator",
        )
        assert evaluated.evaluation["passed"] == evaluated.evaluation["total"] == 3
        publisher.approve(
            current, candidate.candidate_id, actor_id="human", human_approval=True,
        )
        published = publisher.publish(
            current, candidate.candidate_id, version="1.0.0", actor_id="human",
        )
        skill = db.get(Skill, published.asset_id)
        assert skill is not None and skill.status == "active"
        assert skill.name == "Learned procedure: refund"


def test_candidate_evidence_scope_and_server_replay_fail_closed(sessions):
    scope = OrchestrationScope("ws", "user", "agent", "run")
    other = MemoryScope("other", "user", "agent", "run")
    with sessions() as db:
        worker = WorkerSpec("worker", "1", "role", "goal", {"type": "object"},
                            {"type": "object"}, frozenset())
        DurableOrchestrator(db).create(scope, workers=[worker],
                                       tasks=[TaskPlan("task", "worker", {})])
        foreign, _ = record_episodic_evidence(
            db, scope=other, task_type="support", outcome="succeeded", trajectory={},
            metrics={}, idempotency_key="foreign", redaction_status="redacted")
        with pytest.raises(LookupError, match="scope"):
            CandidateAssetPublisher(db).propose(
                scope, candidate_type="skill", name="bad", content={},
                evidence_ids=[foreign.evidence_id], actor_id="user", idempotency_key="bad")

        local, _ = record_episodic_evidence(
            db, scope=MemoryScope("ws", "user", "agent", "run"), task_type="support",
            outcome="succeeded", trajectory={"answer": "safe"}, metrics={},
            idempotency_key="local", redaction_status="redacted")
        publisher = CandidateAssetPublisher(db)
        candidate = publisher.propose(
            scope, candidate_type="skill", name="safe", content={},
            evidence_ids=[local.evidence_id], actor_id="user", idempotency_key="safe")
        publisher.redact(scope, candidate.candidate_id,
                         redacted_content={"content": "safe"}, actor_id="user")
        with pytest.raises(ValueError, match="not configured"):
            publisher.evaluate(scope, candidate.candidate_id,
                               replay_report={"replay_pass_rate": 1.0}, actor_id="user")

        verified = CandidateAssetPublisher(
            db, replay_executor=lambda content, evidence: {"passed": 0, "total": 1}
        )
        with pytest.raises(ValueError, match="pass replay"):
            verified.evaluate(scope, candidate.candidate_id,
                              replay_report={"replay_pass_rate": 1.0}, actor_id="user")
