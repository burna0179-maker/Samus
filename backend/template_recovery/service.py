"""Template-recovery orchestrator.

Takes a :class:`RecoveryRequest`, runs selector -> fallback, persists a row
to the ``samus_task_state`` table via the shared ``write_task_state`` helper,
and returns a :class:`RecoveryResponse`.

The whole path is deterministic and consumes ZERO LLM calls — no LLM client
is imported here or anywhere downstream.
"""

from __future__ import annotations

import logging

from backend.common.task_state import write_task_state

from .fallback import render_scaffold
from .models import RecoveryRequest, RecoveryResponse

_LOG = logging.getLogger("samus.template_recovery.service")

_SERVICE = "template_recovery"
_CAPABILITY = "plan_execution"
_DECISION_PATH = "template_recovery"


def recover(req: RecoveryRequest, *, task_id: str | None = None) -> RecoveryResponse:
    """Run deterministic recovery for one failed LLM-driven step.

    ``task_id`` identifies the task-state row; defaults to a stable id
    derived from the task kind when the caller does not supply one.
    Persistence is fail-soft (``write_task_state`` never raises) so a store
    outage never blocks the deterministic recovery itself.
    """
    result = render_scaffold(req.task_kind, req.context)

    response = RecoveryResponse(
        task_kind=req.task_kind,
        scaffold=result.scaffold,
        template_version=result.template_version,
        fallback_triggered=True,
        generic_fallback=result.generic_fallback,
        failure_reason=req.failure_reason,
    )

    resolved_task_id = task_id or f"template-recovery-{req.task_kind}"
    write_task_state(
        task_id=resolved_task_id,
        service=_SERVICE,
        status="completed",
        capability=_CAPABILITY,
        decision_path=_DECISION_PATH,
        fallback_triggered=True,
        task_kind=req.task_kind,
        template_version=result.template_version,
        generic_fallback=result.generic_fallback,
        llm_cost=0.0,
    )

    _LOG.info(
        "template_recovery served task_kind=%s version=%s generic=%s",
        req.task_kind,
        result.template_version,
        result.generic_fallback,
    )
    return response


__all__ = ["recover"]
