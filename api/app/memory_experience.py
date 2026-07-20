"""Consolidate successful redacted trajectories into non-public Skill candidates."""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .governance_models import EpisodicEvidence, ProceduralCandidate
from .memory_ledger import MemoryScope, propose_procedural_candidate, record_episodic_evidence


_STRUCTURAL_REPLAY_INPUT_SCHEMA = {
    "type": "object",
    "required": ["task_type", "status"],
    "properties": {
        "task_type": {"type": "string", "minLength": 1},
        "status": {"type": "string", "enum": ["succeeded"]},
        "task_id": {"type": "string"},
        "worker_id": {"type": "string"},
        "runtime_run_id": {"type": "string"},
    },
    "additionalProperties": False,
}

_STRUCTURAL_REPLAY_OUTPUT_SCHEMA = {
    "type": "object",
    "required": ["task_type", "status"],
    "properties": {
        "task_type": {"type": "string", "minLength": 1},
        "status": {"type": "string", "enum": ["succeeded"]},
    },
    "additionalProperties": False,
}


def _structural_replay_fixture(task_type: str, trajectory: dict[str, Any]) -> dict[str, Any]:
    """Build a redacted replay contract from server-owned execution metadata.

    Experience consolidation intentionally does not copy worker result bodies.
    The fixture therefore proves the learned procedure's structural contract
    without inventing a business answer that the stored trace cannot support.
    """
    replay_input: dict[str, Any] = {"task_type": task_type, "status": "succeeded"}
    for field in ("task_id", "worker_id", "runtime_run_id"):
        value = trajectory.get(field)
        if isinstance(value, str) and value:
            replay_input[field] = value
    expected = {"task_type": task_type, "status": "succeeded"}
    return {
        "contract_version": "atlas.structural-replay.v1",
        "input": replay_input,
        "input_schema": _STRUCTURAL_REPLAY_INPUT_SCHEMA,
        "expected_output": expected,
        "output_schema": _STRUCTURAL_REPLAY_OUTPUT_SCHEMA,
        "assertions": [
            {"path": "$.task_type", "operator": "equals", "expected": task_type},
            {"path": "$.status", "operator": "equals", "expected": "succeeded"},
        ],
    }


def consolidate_successful_trajectory(
    db: Session, *, scope: MemoryScope, task_type: str, trajectory: dict[str, Any],
    metrics: dict[str, Any], idempotency_key: str, redaction_status: str,
    minimum_examples: int = 3,
) -> tuple[EpisodicEvidence, ProceduralCandidate | None]:
    """Write Episodic Memory and propose, but never publish, repeated experience."""
    durable_trajectory = dict(trajectory)
    durable_trajectory["replay_fixture"] = _structural_replay_fixture(task_type, trajectory)
    evidence, _ = record_episodic_evidence(
        db, scope=scope, task_type=task_type, outcome="succeeded",
        trajectory=durable_trajectory, metrics=metrics, idempotency_key=idempotency_key,
        redaction_status=redaction_status,
    )
    if redaction_status != "redacted":
        return evidence, None
    matches = list(db.scalars(select(EpisodicEvidence).where(
        EpisodicEvidence.workspace_id == scope.workspace_id,
        EpisodicEvidence.user_id == scope.user_id,
        EpisodicEvidence.agent_id == scope.agent_id,
        EpisodicEvidence.task_type == task_type,
        EpisodicEvidence.outcome == "succeeded",
        EpisodicEvidence.redaction_status == "redacted",
    ).order_by(EpisodicEvidence.created_at, EpisodicEvidence.evidence_id)))
    if len(matches) < minimum_examples:
        return evidence, None
    existing = list(db.scalars(select(ProceduralCandidate).where(
        ProceduralCandidate.workspace_id == scope.workspace_id,
        ProceduralCandidate.user_id == scope.user_id,
        ProceduralCandidate.agent_id == scope.agent_id,
        ProceduralCandidate.candidate_type == "skill",
    )))
    for candidate in existing:
        if candidate.content.get("task_type") == task_type and candidate.status not in {"rejected", "rolled_back"}:
            return evidence, candidate
    ids = [item.evidence_id for item in matches]
    candidate, _ = propose_procedural_candidate(
        db, scope=scope, candidate_type="skill", name=f"Learned procedure: {task_type}",
        content={
            "task_type": task_type,
            "auto_generated": True,
            "description": f"Candidate learned from repeated successful {task_type} trajectories.",
            "use_when": [f"The requested task type is {task_type}."],
            "do_not_use_when": ["The task type or authorization boundary differs."],
            "content": "Draft only: requires redaction, server replay evaluation, and human approval.",
            "input_schema": _STRUCTURAL_REPLAY_INPUT_SCHEMA,
            "output_schema": _STRUCTURAL_REPLAY_OUTPUT_SCHEMA,
            "replay_program": {
                "operation": "template",
                "output": {
                    "task_type": "{{input.task_type}}",
                    "status": "{{input.status}}",
                },
            },
        },
        evidence_ids=ids,
        idempotency_key=f"auto-skill:{scope.run_id or '-'}:{task_type}",
        allow_cross_run_evidence=True,
    )
    return evidence, candidate
