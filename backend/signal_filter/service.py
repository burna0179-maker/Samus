"""signal_filter workcell service orchestrator.

One capability: take an inbound prospect, run the deterministic pipeline
``enrichment → scoring → queue gate``, persist the admission decision to the
``samus_task_state`` table, and return a structured
:class:`~backend.signal_filter.models.EvaluateResponse`.

Zero LLM calls — the workcell exists to *reduce* token spend by rejecting
low-probability prospects before they become real TaskEnvelope jobs.
"""

from __future__ import annotations

import logging
from uuid import uuid4

from backend.common.dates import iso_now
from backend.common.task_state import write_task_state

from .enrichment import enrich
from .models import EvaluateResponse, ProspectInput, SignalScores
from .queue_gate import ADMISSION_THRESHOLD, should_enqueue, weighted_score
from .scoring import signals_from_enrichment

_LOG = logging.getLogger("samus.signal_filter.service")


def _resolve_task_id(prospect: ProspectInput) -> str:
    """Pick a stable task id: prospect_id, else place_id, else a fresh uuid."""
    return prospect.prospect_id.strip() or prospect.place_id.strip() or f"sf-{uuid4().hex[:20]}"


def evaluate_prospect(prospect: ProspectInput) -> EvaluateResponse:
    """Run the full pre-qualification pipeline for one prospect.

    ``enrichment → scoring → queue gate``, then a fail-soft persistence of
    the decision to ``samus_task_state``. Never raises on a persistence
    failure — the admission decision is returned regardless.
    """
    task_id = _resolve_task_id(prospect)

    enrichment = enrich(prospect.model_dump())
    signal = signals_from_enrichment(enrichment)
    score = weighted_score(signal)
    admitted = should_enqueue(signal)

    decision_path = "admitted" if admitted else "rejected"
    _LOG.info(
        "signal_filter decision task=%s score=%.4f path=%s",
        task_id,
        score,
        decision_path,
    )

    # Fail-soft task-state persistence — observability, never on the critical
    # path. The helper returns False (and logs) on any store failure.
    write_task_state(
        task_id=task_id,
        service="signal_filter",
        status="completed",
        capability="plan_execution",
        decision_path=decision_path,
        weighted_score=score,
        admitted=admitted,
    )

    return EvaluateResponse(
        prospect_id=task_id,
        business_name=prospect.business_name,
        website_url=prospect.website_url,
        admitted=admitted,
        weighted_score=score,
        threshold=ADMISSION_THRESHOLD,
        decision_path=decision_path,
        signals=SignalScores(**signal.as_dict()),
        enrichment=enrichment,
        ts=iso_now(),
    )


__all__ = ["evaluate_prospect"]
