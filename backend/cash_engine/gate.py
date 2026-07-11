"""The Codex Gate — what the front door throws up before anything executes.

This is the heart of "automation into highly efficient delegation": the gate
does not decide whether the deal is good, it decides whether Samus is
*allowed to proceed without the operator*. Two checks, both fail-closed:

  1. Stake Sentence present?  The prospect's Opportunity must carry an
     operator-authored Stake Sentence. Read from CRM server-side — never
     from the request payload — so the human's signature is the unlock, not
     a caller's assertion. Absent -> escalate (the deal is already visible on
     the console's pending_stake queue).
  2. Codex clear?  The proposed action is run through
     :func:`backend.common.codex.validator.check_action`. A blocked verdict
     names the rule and auto-drafts an ADR stub. If the Codex registry is
     unavailable we refuse rather than guess (fail-closed).

The gate intentionally validates an ``opportunity_write``-kind action, not a
full ``outreach_send``: at the door we have the Stake Sentence but not the
final subject/body/legitimacy signal, and pre-asserting those would block
legitimate reviews. The unconditional banned-phrase (G2) scan still runs over
the Stake Sentence here as defence-in-depth; the downstream sequence
re-validates an ``outreach_send`` at the actual send handoff.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from backend.common.codex.exceptions import CodexUnavailable
from backend.common.codex.models import ProposedAction
from backend.common.codex.validator import check_action

from .models import RevenueTriggerRequest

_LOG = logging.getLogger("samus.cash_engine.gate")

__all__ = ["GateOutcome", "evaluate_gate"]


@dataclass(frozen=True)
class GateOutcome:
    """Result of throwing up the Codex Gate for one review request."""

    allowed: bool
    opportunity_id: str
    stake_present: bool
    required_protocol: str | None = None
    violated_rule_id: str | None = None
    reason: str | None = None
    drafted_adr_path: str | None = None


def _blocked(
    *,
    opportunity_id: str = "",
    stake_present: bool = False,
    required_protocol: str | None = None,
    violated_rule_id: str | None = None,
    reason: str | None = None,
    drafted_adr_path: str | None = None,
) -> GateOutcome:
    return GateOutcome(
        allowed=False,
        opportunity_id=opportunity_id,
        stake_present=stake_present,
        required_protocol=required_protocol,
        violated_rule_id=violated_rule_id,
        reason=reason,
        drafted_adr_path=drafted_adr_path,
    )


def _run_meaning_advisory(
    req: RevenueTriggerRequest, *, opportunity_id: str, stake: str,
) -> None:
    """Emit the MFH advisory meaning-anchor assessment as a decision.made event.

    Advisory only — the meaning anchors are quorum-class, so this attaches a
    flag for attention (or an all-clear) to the unified stream and NEVER blocks
    the review. Fully fail-soft: any error is swallowed so governance telemetry
    can never break the revenue gate.
    """
    try:
        from backend.common.business_events import DECISION_MADE, emit_business_event
        from backend.governance.mfh_evaluator import evaluate_meaning

        assessment = evaluate_meaning({
            "kind": "cash_engine_review",
            "stake_sentence": stake,
            "trigger_source": req.trigger_source,
            "trigger_reason": getattr(req, "trigger_reason", "") or "",
        })
        emit_business_event(
            DECISION_MADE,
            workcell="cash_engine",
            prospect_id=req.prospect_id,
            opportunity_id=opportunity_id,
            metadata={
                "decision": "meaning_advisory",
                "meaning_flagged": bool(assessment.get("flagged")),
                "meaning_anchors": assessment.get("anchors") or [],
                "meaning_notes": assessment.get("notes") or "",
            },
        )
        if assessment.get("flagged"):
            _LOG.info(
                "cash_engine MFH advisory flag prospect=%s opp=%s anchors=%s",
                req.prospect_id, opportunity_id, assessment.get("anchors"),
            )
    except Exception as exc:  # noqa: BLE001 — advisory telemetry never blocks
        _LOG.warning("cash_engine meaning advisory failed (proceeding): %s", exc)


def evaluate_gate(
    req: RevenueTriggerRequest,
    *,
    crm: Any = None,
) -> GateOutcome:
    """Run the two-check Codex Gate for ``req``.

    ``crm`` is the CRM service module (injectable for tests); it must expose
    ``get_opportunity_for_prospect(prospect_id) -> Opportunity | None``.
    """
    if crm is None:
        from backend.crm import service as crm  # local import: avoid cycle

    opp = crm.get_opportunity_for_prospect(req.prospect_id)
    if opp is None:
        # Nothing to review — there is no deal to attach a Stake Sentence to.
        # This is an escalation, not an error: an operator (or the lead->opp
        # conversion path) must mint the Opportunity first.
        return _blocked(
            required_protocol="opportunity",
            reason=(
                f"no Opportunity exists for prospect {req.prospect_id!r}; "
                "cannot review until a deal is opened"
            ),
        )

    opportunity_id = str(getattr(opp, "opportunity_id", "") or "")
    stake = str(getattr(opp, "stake_sentence", "") or "").strip()
    if not stake:
        # THE escalation surface. The deal already shows on the operator
        # console's pending_stake queue; the door just confirms the engine
        # will not move it until the one sentence is signed.
        return _blocked(
            opportunity_id=opportunity_id,
            stake_present=False,
            required_protocol="stake_sentence",
            reason=(
                "Opportunity has no operator-authored Stake Sentence; "
                "awaiting the operator's signature before any revenue action"
            ),
        )

    # Advisory meaning-anchor pass (HOTL T5, MFH). Softer sibling of EFH: the
    # meaning anchors are quorum-class, so a heuristic concern is FLAGGED (on
    # the decision.made event) for operator/quorum attention, never blocks the
    # review. Best-effort — a governance hiccup must not wedge the gate.
    _run_meaning_advisory(req, opportunity_id=opportunity_id, stake=stake)

    action = ProposedAction(
        service="gateway",
        capability="cash_engine_review",
        action_kind="opportunity_write",
        payload={
            "prospect_id": req.prospect_id,
            "opportunity_id": opportunity_id,
            "stake_sentence": stake,
            "trigger_source": req.trigger_source,
        },
        proposed_by="cash_engine",
    )
    try:
        verdict = check_action(action)
    except CodexUnavailable as exc:
        # The Codex layer is the gate; if it cannot answer we refuse to
        # proceed. Revenue never moves on an un-validated path.
        return _blocked(
            opportunity_id=opportunity_id,
            stake_present=True,
            required_protocol="codex",
            reason=f"Codex registry unavailable; refusing to proceed (fail-closed): {exc}",
        )

    if not verdict.allowed:
        return _blocked(
            opportunity_id=opportunity_id,
            stake_present=True,
            violated_rule_id=verdict.violated_rule_id,
            reason=verdict.reason,
            drafted_adr_path=verdict.drafted_adr_path,
        )

    # Karma gate (earned progressive autonomy) — opt-in; a no-op ALLOW when
    # disabled or when the actor is innocent/well-behaved. A known-bad actor
    # (penalized below the required AccessTier) is escalated to the operator
    # via the existing required_protocol="karma" path (service.py buckets any
    # non-opportunity block as "escalated"). Never breaks the gate.
    try:
        from backend.governance.karma import engine as karma
        decision = karma.check("opportunity_write")
        if not decision.allowed:
            return _blocked(
                opportunity_id=opportunity_id,
                stake_present=True,
                required_protocol="karma",
                reason=decision.reason,
            )
    except Exception as exc:  # noqa: BLE001 — karma must never break the gate
        _LOG.warning("karma gate check errored (proceeding): %s", exc)

    return GateOutcome(
        allowed=True,
        opportunity_id=opportunity_id,
        stake_present=True,
    )
