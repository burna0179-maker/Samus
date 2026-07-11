"""Enqueue seam for the Cash Engine — SQS when configured, JSONL when not.

The downstream sequence (audit -> proposal -> contact -> outreach -> ...) is
consumed off the ``samus-cash-engine-jobs`` queue. In production that is an
SQS queue, wired exactly like every other workcell via
``sqs_dispatch.QUEUE_URLS``. Until ``SQS_CASH_ENGINE_QUEUE_URL`` (and the
roster entry in ``settings.bootstrap_settings``) land, there is no queue URL
for the cash_engine service, so ``enqueue_cash_job`` falls back to appending
the job to a local JSONL "mock queue" under the state root. That is the
logic-first path from the build plan: the full flow is provable end-to-end —
decay -> front door -> gate -> enqueue -> job visible in the mock queue —
with no AWS dependency. Swapping to real SQS is a config change, not a code
change.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from backend.common.dates import iso_now
from backend.common.settings import get_settings
from backend.common.state_paths import state_path
from backend.gateway import sqs_dispatch

_LOG = logging.getLogger("samus.cash_engine.queue")

_CASH_DIR = "cash_engine"
_JOBS_FILE = "jobs.jsonl"        # mock queue (downstream sequence jobs)
_REVIEWS_FILE = "reviews.jsonl"  # audit ledger of every front-door verdict

__all__ = [
    "enqueue_cash_job",
    "read_mock_jobs",
    "record_review",
    "read_review_ledger",
    "append_jsonl",
]


def _cash_path(filename: str) -> Path:
    return state_path(_CASH_DIR, filename)


def append_jsonl(path: Path, record: dict[str, Any]) -> bool:
    """Append one JSON record as a line. Best-effort; returns success.

    Creates the parent directory on first write. A write failure is logged
    and reported as False but never raised — telemetry/mock-queue I/O must
    not break the request path.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        return True
    except OSError as exc:
        _LOG.error("cash_engine jsonl append failed (%s): %s", path, exc)
        return False


def enqueue_cash_job(
    *,
    task_id: str,
    payload: dict[str, Any],
    action: str = "cash_engine_step",
    metadata: dict[str, Any] | None = None,
    trace_id: str | None = None,
    idempotency_key: str | None = None,
    service: str | None = None,
) -> dict[str, Any]:
    """Enqueue a downstream cash-engine job.

    Sends to the per-service SQS queue when one is configured for the
    cash_engine service; otherwise appends to the local JSONL mock queue.
    Returns a dict carrying ``queued``, ``task_id``, and a ``queue`` tag
    (``"sqs:<service>"`` or ``"mock:jsonl"``) so the caller can report where
    the job landed.
    """
    svc = service or get_settings().cash_engine_queue_service
    queue_url = sqs_dispatch.QUEUE_URLS.get(svc)
    if queue_url:
        result = sqs_dispatch.enqueue_dispatch(
            svc,
            task_id=task_id,
            action=action,
            payload=payload,
            metadata=metadata or {},
            trace_id=trace_id,
            idempotency_key=idempotency_key,
        )
        result["queue"] = f"sqs:{svc}"
        return result

    # Mock-first fallback: durable, append-only JSONL the next-phase worker
    # (and tests) drain. No AWS, no network — the seam is intentionally
    # identical in shape to the SQS result.
    record = {
        "task_id": task_id,
        "service": svc,
        "action": action,
        "trace_id": trace_id,
        "idempotency_key": idempotency_key,
        "payload": payload,
        "metadata": metadata or {},
        "enqueued_at": iso_now(),
    }
    path = _cash_path(_JOBS_FILE)
    queued = append_jsonl(path, record)
    return {
        "queued": queued,
        "queue": "mock:jsonl",
        "service": svc,
        "task_id": task_id,
        "path": str(path),
    }


def read_mock_jobs(limit: int | None = None) -> list[dict[str, Any]]:
    """Read the local JSONL mock queue, oldest first. [] when absent."""
    return _read_jsonl(_cash_path(_JOBS_FILE), limit=limit)


def record_review(record: dict[str, Any]) -> bool:
    """Append one front-door verdict to the review audit ledger."""
    stamped = {"ts": iso_now(), **record}
    return append_jsonl(_cash_path(_REVIEWS_FILE), stamped)


def read_review_ledger(limit: int | None = None) -> list[dict[str, Any]]:
    """Read the front-door review ledger, oldest first. [] when absent."""
    return _read_jsonl(_cash_path(_REVIEWS_FILE), limit=limit)


def _read_jsonl(path: Path, *, limit: int | None = None) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError as exc:
        _LOG.error("cash_engine jsonl read failed (%s): %s", path, exc)
        return []
    if limit is not None and limit >= 0:
        return out[-limit:]
    return out
