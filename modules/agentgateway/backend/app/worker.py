"""Async writeback worker (Batch C).

Consumes the job queue produced by :mod:`app.core.job_queue` and drives each job
through the writeback state machine: ``pending → running → done/failed``. The
extraction logic itself (extract_profile / extract_episodic / merge_semantic /
compress_summary) is Batch D — here the handlers are placeholders so the loop,
state machine and failure/retry path are exercisable end-to-end now.

Run standalone::

    python -m app.worker

With ``JOB_QUEUE_PROVIDER=redis`` the loop pulls envelopes off the Redis queue;
with ``inprocess`` there is nothing to pull (jobs only sit in the DB as pending),
so the loop idles — use the redis provider to actually drain work.
"""

from __future__ import annotations

import logging
import os
import signal
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Dict, Optional

from app.core import job_queue
from app.models.db import MEMORY_JOB_TYPES

_log = logging.getLogger(__name__)

# job_type → handler. A handler raises on failure (→ mark_failed, retryable).
# Batch D replaces these placeholders with real extraction/merge/compression.
Handler = Callable[[int, dict], None]


def _placeholder(job_type: str) -> Handler:
    def _handle(job_id: int, payload: dict) -> None:
        _log.info(
            "worker: job %s (%s) accepted by placeholder handler — extraction is "
            "implemented in Batch D; marking done as a no-op", job_id, job_type,
        )
    return _handle


# Registered handlers. Every known writeback job_type gets a placeholder so the
# loop marks them done rather than failing; unknown types are failed (not done).
_HANDLERS: Dict[str, Handler] = {jt: _placeholder(jt) for jt in MEMORY_JOB_TYPES}


def register_handler(job_type: str, handler: Handler) -> None:
    """Register/replace the handler for a job_type (Batch D wires real ones)."""
    _HANDLERS[job_type] = handler


def process_one(envelope: dict) -> bool:
    """Process a single dequeued job envelope ``{id, job_type}``.

    Drives the state machine: mark_running → handler → mark_done; on handler
    error mark_failed (the job stays retryable since dedupe only blocks
    pending/running). An unknown job_type is marked failed, never done. Returns
    True when the job ended ``done``, False otherwise.
    """
    job_id = envelope.get("id")
    job_type = envelope.get("job_type") or ""
    if job_id is None:
        _log.warning("worker: envelope without id dropped: %r", envelope)
        return False

    handler = _HANDLERS.get(job_type)
    if handler is None:
        job_queue.mark_failed(job_id, f"no handler registered for job_type={job_type!r}")
        _log.warning("worker: job %s has unregistered job_type %r → failed", job_id, job_type)
        return False

    job = job_queue.mark_running(job_id)
    payload = {}
    if job is not None and job.payload_json:
        import json
        try:
            payload = json.loads(job.payload_json) or {}
        except (json.JSONDecodeError, TypeError):
            payload = {}

    try:
        handler(job_id, payload)
    except Exception as exc:  # any handler failure is retryable
        job_queue.mark_failed(job_id, f"{type(exc).__name__}: {exc}")
        _log.warning("worker: job %s (%s) failed: %s", job_id, job_type, exc)
        return False

    job_queue.mark_done(job_id)
    return True


class _Stop:
    """Cooperative stop flag toggled by SIGTERM/SIGINT."""

    def __init__(self) -> None:
        self.stopped = False

    def request(self, *_a) -> None:
        self.stopped = True


def run_worker(poll_interval: float = 1.0, max_iterations: Optional[int] = None) -> None:
    """Main worker loop: dequeue and process jobs until stopped.

    ``WORKER_CONCURRENCY`` controls the thread pool size. ``max_iterations``
    bounds the loop for tests (None = run forever). Idles ``poll_interval`` when
    the queue is empty.
    """
    concurrency = max(1, int(os.environ.get("WORKER_CONCURRENCY", "1") or "1"))
    stop = _Stop()
    signal.signal(signal.SIGTERM, stop.request)
    signal.signal(signal.SIGINT, stop.request)

    _log.info("worker: starting (concurrency=%s, provider=%s)", concurrency, job_queue._provider())
    iterations = 0
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        while not stop.stopped:
            if max_iterations is not None and iterations >= max_iterations:
                break
            iterations += 1
            env = job_queue.dequeue()
            if env is None:
                if max_iterations is not None:
                    # Bounded (test) mode: don't sleep through the budget.
                    continue
                time.sleep(poll_interval)
                continue
            pool.submit(process_one, env)
    _log.info("worker: stopped after %s iterations", iterations)


if __name__ == "__main__":  # pragma: no cover - manual entry point
    logging.basicConfig(level=logging.INFO)
    # Replace the placeholder handlers with the real Batch D writeback handlers.
    from app.core import writeback_handlers
    writeback_handlers.register_all()
    run_worker()
