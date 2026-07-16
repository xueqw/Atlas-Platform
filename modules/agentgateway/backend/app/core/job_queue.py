"""Job queue abstraction + writeback job state machine (Batch C).

Sits between the runtime (which enqueues memory-writeback work) and the worker
(which will consume it in a later batch). The durable source of truth is the
``memory_writeback_jobs`` table — ``enqueue`` reuses
``memory_service.queue_writeback_job`` for idempotent row creation (dedupe on
``(source_kind, source_ref, job_type)`` against pending/running jobs), then, when
``JOB_QUEUE_PROVIDER=redis``, pushes a lightweight envelope (job id + type) onto
a Redis list as a wake-up signal.

With ``JOB_QUEUE_PROVIDER=inprocess`` (default) nothing is pushed: the job is
left as ``pending`` in the table and the main request path is never blocked. No
worker process consumes the queue in this batch — that is task 4.6.

The state-machine helpers (``mark_running`` / ``mark_done`` / ``mark_failed``)
move a job ``pending → running → done/failed`` and persist ``error`` /
``finished_at``. A ``failed`` job can be re-enqueued (idempotency only blocks
pending/running), so retries are possible.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, Optional

from sqlmodel import Session, select

from app.core.database import engine
from app.core import redis_client
from app.core.memory_service import queue_writeback_job
from app.models.db import MemoryWritebackJob, _utcnow

_log = logging.getLogger(__name__)

# Single logical queue name for memory-writeback work in this phase.
_WRITEBACK_QUEUE = "writeback"


def _provider() -> str:
    return (os.environ.get("JOB_QUEUE_PROVIDER") or "inprocess").strip().lower()


def enqueue(
    *, job_type: str, source_kind: str, source_ref: str, payload: Optional[Dict[str, Any]] = None
) -> Optional[MemoryWritebackJob]:
    """Idempotently enqueue a writeback job.

    Always writes/dedupes the durable row first (the truth source). When the
    provider is ``redis`` AND a new/pending row exists, also pushes a wake-up
    envelope onto the Redis queue. Returns the job row (existing or new), or None
    when ``job_type`` is invalid.
    """
    job = queue_writeback_job(
        source_kind=source_kind, source_ref=source_ref, job_type=job_type, payload=payload
    )
    if job is None:
        return None

    if _provider() == "redis":
        try:
            envelope = json.dumps({"id": job.id, "job_type": job.job_type}, ensure_ascii=False)
            redis_client.queue_push(_WRITEBACK_QUEUE, envelope)
        except Exception as exc:  # queue is a wake-up signal; table is the truth
            _log.warning("enqueue: redis push failed (%s); job %s left pending in DB", exc, job.id)
    return job


def dequeue() -> Optional[Dict[str, Any]]:
    """Pop one job envelope from the queue (redis provider only).

    Returns the parsed ``{id, job_type}`` envelope, or None when the queue is
    empty / inprocess. The worker (later batch) loads the full row by id.
    """
    if _provider() != "redis":
        return None
    raw = redis_client.queue_pop(_WRITEBACK_QUEUE)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        _log.warning("dequeue: malformed envelope dropped: %r", raw)
        return None


def ack(job_id: int) -> None:
    """Acknowledge processing completion for a job envelope.

    The Redis list backend removes items on pop, so ack is a no-op hook kept for
    interface stability (a future visibility-timeout queue would use it)."""
    redis_client.queue_ack(_WRITEBACK_QUEUE, str(job_id))


def nack(job_id: int, error: str = "") -> None:
    """Negative-ack: mark the job failed so it can be retried later."""
    mark_failed(job_id, error)


# ── writeback job state machine ─────────────────────────────────────────────

def _set_status(job_id: int, status: str, *, error: Optional[str] = None, finished: bool = False) -> Optional[MemoryWritebackJob]:
    with Session(engine) as s:
        job = s.get(MemoryWritebackJob, job_id)
        if job is None:
            return None
        job.status = status
        if error is not None:
            job.error = error
        if finished:
            job.finished_at = _utcnow()
        s.add(job)
        s.commit()
        s.refresh(job)
        return job


def mark_running(job_id: int) -> Optional[MemoryWritebackJob]:
    return _set_status(job_id, "running")


def mark_done(job_id: int) -> Optional[MemoryWritebackJob]:
    return _set_status(job_id, "done", finished=True)


def mark_failed(job_id: int, error: str = "") -> Optional[MemoryWritebackJob]:
    return _set_status(job_id, "failed", error=error[:2000], finished=True)
