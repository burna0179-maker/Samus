"""``review_opportunity`` — the single logic entry behind the front door.

Both ingress paths converge here:
  * the HTTP route ``POST /api/samus/review_opportunity`` (external / operator
    / peer callers), and
  * the in-process ``signal_decay`` trigger
    (:mod:`backend.portfolio_controller.decay_scan`).

The contract is "HALT, don't execute": throw up the Codex Gate, and only on a
clean pass mint an internal envelope and enqueue the downstream sequence. A
blocked gate escalates with a structured verdict — it never silently drops
and never executes a revenue action on un-validated authority. Every outcome
is written to the review ledger for the operator's audit trail.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Callable

from backend.common import correlation
from backend.common.decision_record import record_decision
from backend.common.settings import get_settings

from . import queue as cash_queue
from .gate import evaluate_gate
from .models import RevenueTriggerRequest, RevenueTriggerResult

_LOG = logging.getLogger("samus.cash_engine.service")

# The stage the minted job enters: the Stake Sentence gate has cleared, so
# the downstream worker begins at the Gap Report audit.
_INITIAL_STAGE = "audit"


def review_opportunity(
    req: RevenueTriggerRequest,
    *,
    crm: Any = None,
    enqueue: Callable[..., dict[str, Any]] | None = None,
    decay_risk: float | None = None,
) -> RevenueTriggerResult:
    """Run one opportunity review through the Codex Gate.

    ``crm`` (CRM service module) and ``enqueue`` (the job sink) are injectable
    so the flow is testable in-process with no AWS. ``decay_risk`` is threaded
    through from the signal_decay trigger purely for the audit trail.
    """
    settings = get_settings()
    if not settings.cash_engine_enabled:
        result = RevenueTriggerResult(
            accepted=False,
            status="disabled",
            prospect_id=req.prospect_id,
            reason="cash_engine is disabled (SAMUS_CASH_ENGINE_ENABLED=false)",
            decay_risk=decay_risk,
        )
        _ledger(req, result)
        return result

    outcome = evaluate_gate(req, crm=crm)

    if not outcome.allowed:
        # "opportunity" missing means there is literally no deal to act on —
        # that is an invalid request, not an operator-actionable escalation.
        # Every other block (stake_sentence, codex rule) IS an escalation the
        # operator resolves.
        status = "invalid" if outcome.required_protocol == "opportunity" else "escalated"
        result = RevenueTriggerResult(
            accepted=False,
            status=status,
            prospect_id=req.prospect_id,
            opportunity_id=outcome.opportunity_id,
            required_protocol=outcome.required_protocol,
            violated_rule_id=outcome.violated_rule_id,
            reason=outcome.reason,
            drafted_adr_path=outcome.drafted_adr_path,
            decay_risk=decay_risk,
        )
        _LOG.info(
            "cash_engine review BLOCKED prospect=%s opp=%s status=%s protocol=%s rule=%s",
            req.prospect_id,
            outcome.opportunity_id,
            status,
            outcome.required_protocol,
            outcome.violated_rule_id,
        )
        _ledger(req, result)
        return result

    # Gate clean -> mint the internal envelope + enqueue the sequence.
    task_id = f"ce-{uuid.uuid4().hex[:12]}"
    trace_id = correlation.get_trace_id() or correlation.new_trace_id()
    payload: dict[str, Any] = {
        "prospect_id": req.prospect_id,
        "opportunity_id": outcome.opportunity_id,
        "trigger_source": req.trigger_source,
        "trigger_reason": req.trigger_reason,
        "current_samus_state": req.current_samus_state,
        "gap_report_artifact_id": req.gap_report_artifact_id,
        "stake_present": True,
        "stage": _INITIAL_STAGE,
        "history": [],
    }
    metadata: dict[str, Any] = {
        "action": "cash_engine_step",
        "trace_id": trace_id,
        "idempotency_key": f"cash:{req.prospect_id}:{req.trigger_source}",
        "source": "cash_engine",
    }

    enqueue_fn = enqueue or cash_queue.enqueue_cash_job
    try:
        q = enqueue_fn(
            task_id=task_id,
            payload=payload,
            metadata=metadata,
            trace_id=trace_id,
            idempotency_key=metadata["idempotency_key"],
        )
    except Exception as exc:  # noqa: BLE001 — surface enqueue failure structurally
        _LOG.error("cash_engine enqueue failed prospect=%s: %s", req.prospect_id, exc)
        result = RevenueTriggerResult(
            accepted=False,
            status="invalid",
            prospect_id=req.prospect_id,
            opportunity_id=outcome.opportunity_id,
            reason=f"enqueue_failed: {exc}",
            decay_risk=decay_risk,
        )
        _ledger(req, result)
        return result

    result = RevenueTriggerResult(
        accepted=True,
        status="enqueued",
        prospect_id=req.prospect_id,
        opportunity_id=outcome.opportunity_id,
        task_id=task_id,
        queue=str(q.get("queue", "")),
        decay_risk=decay_risk,
    )
    _LOG.info(
        "cash_engine review ENQUEUED prospect=%s opp=%s task=%s queue=%s",
        req.prospect_id,
        outcome.opportunity_id,
        task_id,
        result.queue,
    )
    _ledger(req, result)
    return result


def _ledger(req: RevenueTriggerRequest, result: RevenueTriggerResult) -> None:
    """Best-effort append of one review verdict to the audit ledger."""
    cash_queue.record_review(
        {
            "prospect_id": req.prospect_id,
            "opportunity_id": result.opportunity_id,
            "trigger_source": req.trigger_source,
            "trigger_reason": req.trigger_reason,
            "current_samus_state": req.current_samus_state,
            "accepted": result.accepted,
            "status": result.status,
            "task_id": result.task_id,
            "queue": result.queue,
            "required_protocol": result.required_protocol,
            "violated_rule_id": result.violated_rule_id,
            "reason": result.reason,
            "decay_risk": result.decay_risk,
        }
    )
    _record_front_door_decision(req, result)


def _record_front_door_decision(
    req: RevenueTriggerRequest,
    result: RevenueTriggerResult,
) -> None:
    """Join the front-door admit/reject verdict to the unified decision spine.

    ADDITIVE telemetry only — the verdict is already in the review ledger. This
    mirrors it as a ``decision.made`` so the highest-impact revenue decision
    (admit a prospect to the cash engine vs escalate/reject it) appears in the
    journey view alongside planner + arbiter decisions, with a rationale +
    risk. ``_ledger`` runs on EVERY outcome (disabled / blocked / enqueue-fail /
    enqueued), so this one chokepoint covers them all. Fail-soft — never breaks
    the front door.
    """
    try:
        status = result.status or "unknown"
        if status == "escalated":
            risk, why = "high", (result.reason or "gate blocked")
            expected = "operator resolves the block; deal holds"
        elif result.accepted:  # enqueued
            risk, why = "normal", "gate clean; admitted to the revenue engine"
            expected = "deal walks the staged sequence (audit -> deliver)"
        else:  # invalid / disabled
            risk, why = "normal", (result.reason or status)
            expected = "no action taken"
        record_decision(
            actor="cash_engine_gate",
            why=why,
            workcell="cash_engine",
            risk_level=risk,
            expected_outcome=expected,
            data_used=[
                f"trigger_source={req.trigger_source}",
                f"decay_risk={result.decay_risk}",
            ],
            prospect_id=req.prospect_id or None,
            opportunity_id=result.opportunity_id or None,
            extra={
                "decision_kind": f"front_door_{status}",
                "required_protocol": result.required_protocol or "",
                "violated_rule_id": result.violated_rule_id or "",
            },
        )
    except Exception as exc:  # noqa: BLE001 — telemetry must never break the front door
        _LOG.debug("cash_engine front-door decision-record skipped: %s", exc)


__all__ = ["review_opportunity"]
