"""signal_decay trigger — portfolio_controller's decay sweep.

The portfolio_controller is enhanced (per the build plan) to do more than
rebalance token quotas: it computes a :class:`~backend.cash_engine.decay.DecayAssessment`
for each tracked Opportunity and, when the DecayRiskScore crosses the
configured threshold, it does NOT just update a record — it *forces the
generation of a RevenueTriggerRequest* and presents it at the Cash Engine
front door. The controller is, in other words, just another caller knocking
on ``/api/samus/review_opportunity``; here it knocks in-process by calling
the same ``review_opportunity`` handler directly (no pointless HTTP loopback
from the gateway to itself).

External-factor signal (market news / regulatory) is pluggable via
``external_factor_fn``; absent one, the contribution is 0.0 — the score is
honestly driven by stage x staleness until a real feed is wired, rather than
inventing market pressure.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from backend.cash_engine.decay import compute_decay_risk
from backend.cash_engine.models import RevenueTriggerRequest, RevenueTriggerResult
from backend.common.settings import get_settings

_LOG = logging.getLogger("samus.portfolio_controller.decay_scan")


@dataclass
class DecayScanResult:
    """Summary of one decay sweep."""

    scanned: int = 0
    crossed: int = 0
    enqueued: int = 0
    escalated: int = 0
    threshold: float = 0.0
    stall_days: float = 0.0
    results: list[RevenueTriggerResult] = field(default_factory=list)


def scan_for_decay_triggers(
    *,
    threshold: float | None = None,
    stall_days: float | None = None,
    limit: int = 100,
    now: datetime | None = None,
    crm: Any = None,
    review: Callable[..., RevenueTriggerResult] | None = None,
    external_factor_fn: Callable[[Any], float] | None = None,
) -> DecayScanResult:
    """Sweep open Opportunities and fire signal_decay reviews over threshold.

    ``crm`` (CRM service module) and ``review`` (the front-door handler) are
    injectable so the sweep is testable in-process. Every fired review goes
    through the full Codex Gate — a stalled deal with no Stake Sentence
    escalates rather than executes, exactly like any other caller.
    """
    settings = get_settings()
    thr = settings.cash_engine_decay_threshold if threshold is None else threshold
    window = settings.cash_engine_stall_days if stall_days is None else stall_days

    if crm is None:
        from backend.crm import service as crm  # local import: avoid cycle
    if review is None:
        from backend.cash_engine.service import review_opportunity as review

    out = DecayScanResult(threshold=thr, stall_days=window)

    if not settings.cash_engine_enabled:
        _LOG.info("decay_scan skipped: cash_engine disabled")
        return out

    try:
        listing = crm.list_opportunities(limit=limit)
        opportunities = list(getattr(listing, "opportunities", []) or [])
    except Exception as exc:  # noqa: BLE001 — a CRM read fault must not crash the tick
        _LOG.error("decay_scan list_opportunities failed: %s", exc)
        return out

    for opp in opportunities:
        out.scanned += 1
        prospect_id = str(getattr(opp, "prospect_id", "") or "")
        if not prospect_id:
            continue

        call_state = None
        try:
            call_state = crm.get_call_state(prospect_id)
        except Exception:  # noqa: BLE001 — call state is optional context
            call_state = None

        external = 0.0
        if external_factor_fn is not None:
            try:
                external = float(external_factor_fn(opp) or 0.0)
            except Exception:  # noqa: BLE001 — a bad feed must not break the sweep
                external = 0.0

        assessment = compute_decay_risk(
            opp,
            call_state=call_state,
            external_factor=external,
            stall_days=window,
            now=now,
        )
        if not assessment.crosses(thr):
            continue

        out.crossed += 1
        req = RevenueTriggerRequest(
            prospect_id=prospect_id,
            trigger_source="signal_decay",
            current_samus_state=(
                f"stage={assessment.stage} "
                f"staleness_days={assessment.staleness_days:.1f}"
            ),
            trigger_reason=(
                f"DecayRiskScore {assessment.decay_risk:.2f} "
                f">= threshold {thr:.2f}"
            ),
        )
        result = review(req, crm=crm, decay_risk=assessment.decay_risk)
        out.results.append(result)
        if result.status == "enqueued":
            out.enqueued += 1
        elif result.status == "escalated":
            out.escalated += 1

    _LOG.info(
        "decay_scan: scanned=%d crossed=%d enqueued=%d escalated=%d thr=%.2f",
        out.scanned, out.crossed, out.enqueued, out.escalated, thr,
    )
    return out


__all__ = ["DecayScanResult", "scan_for_decay_triggers"]
