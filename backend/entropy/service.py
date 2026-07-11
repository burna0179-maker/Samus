"""Service layer for the entropy workcell.

Wraps :mod:`backend.entropy.monitor`: runs the monitor and persists the
outcome to the ``samus_task_state`` table via the shared
:func:`write_task_state` helper. ``app.py`` calls :func:`scan` only.
"""

from __future__ import annotations

import logging

from backend.common.task_state import write_task_state

from .models import EntropyScanRequest, EntropyScanResponse
from .monitor import run_monitor

_LOG = logging.getLogger("samus.entropy.service")

_SERVICE = "entropy"
_CAPABILITY = "plan_execution"
_DECISION_PATH = "entropy_scan"


def scan(task_id: str, req: EntropyScanRequest) -> EntropyScanResponse:
    """Run an entropy scan, persist task state, return the structured response.

    Persistence is fail-soft — a store outage never fails the scan; the
    ``persisted`` flag reports whether the row was written.
    """
    report = run_monitor(
        queue_variance=req.queue_variance,
        error_velocity=req.error_velocity,
        task_retry_rate=req.task_retry_rate,
        llm_failure_ratio=req.llm_failure_ratio,
        efficiency_ema=req.efficiency_ema,
        token_success_ratio=req.token_success_ratio,
        template_success_rate=req.template_success_rate,
    )

    persisted = write_task_state(
        task_id=task_id,
        service=_SERVICE,
        capability=_CAPABILITY,
        decision_path=_DECISION_PATH,
        entropy_score=report.entropy_score,
        stable=report.stable,
        countermeasures=list(report.countermeasures),
    )

    _LOG.info(
        "entropy_scan task=%s score=%.4f stable=%s countermeasures=%d persisted=%s",
        task_id,
        report.entropy_score,
        report.stable,
        len(report.countermeasures),
        persisted,
    )

    return EntropyScanResponse(
        task_id=task_id,
        entropy_score=report.entropy_score,
        countermeasures=list(report.countermeasures),
        stable=report.stable,
        persisted=persisted,
    )


__all__ = ["scan"]
