"""CODB investment reasoner — rank which cost-of-doing-business scale-up to buy next.

This converts a recurring HUMAN reasoning step into a reusable system capability:
when a deal closes and cash frees up, Samus should be able to say, in plain
English, *"cold-email reach is the binding constraint; available cash now covers
Apollo Basic $65/mo -> ~25x reach -> recommend upgrade."*

Seam
----
This module sits exactly on the finance <-> cognition boundary:

  * It READS financials (available cash / MRR) through the SAME accessors the
    rest of the system already uses — the finance ``/runway`` + ``/snapshot``
    surface first, falling back to :mod:`backend.common.llm_budget_scaler`'s
    Stripe MRR fetch. No new Stripe client, no duplicated finance math.
  * It READS the declarative investment catalog from ``codb_registry.yaml``
    (``codb_investments`` section) via the finance CODB loader.
  * It WRITES its output through the existing :class:`GuidanceLedger`, so a
    recommendation is a first-class, tracked, operator-reviewable record — the
    same lifecycle every other strategic recommendation flows through.

Governance
----------
RECOMMEND-ONLY. This module computes affordability + ROI and emits guidance
records. It NEVER calls a purchase/checkout/spend path, never mutates a budget
cap, never touches the cash engine. Actual spend is an operator action taken on
the guidance record. There is no code path here that moves money.

Everything is fail-safe: a degraded finance source or a missing catalog yields
a partial/empty result, never an exception that could crash a scheduled EOD job
or a webhook thread.
"""
from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass, field
from typing import Any, List, Optional

from backend.common.dates import iso_now

log = logging.getLogger(__name__)

# Fraction of available headroom we refuse to commit to a recurring upgrade —
# a safety reserve so the reasoner never recommends spending the last dollar of
# cushion. Read from env so the operator can tune posture without a redeploy.
_SAFETY_RESERVE_PCT = float(os.getenv("SAMUS_CODB_SAFETY_RESERVE_PCT", "0.25"))

# When only MRR is available (no live cash balance), treat this fraction of one
# month's MRR as the spendable headroom for a recurring upgrade. Conservative:
# a recurring $/mo commitment should be a small slice of recurring revenue.
_MRR_HEADROOM_PCT = float(os.getenv("SAMUS_CODB_MRR_HEADROOM_PCT", "0.10"))


# ---------------------------------------------------------------------------
# Financials accessor — reuse existing finance surfaces (no new Stripe client).
# ---------------------------------------------------------------------------
@dataclass
class Financials:
    """The money inputs the reasoner reasons over.

    ``available_cash_usd`` is the live cash cushion (Stripe available balance)
    when we can get it; ``mrr_usd`` is monthly recurring revenue. ``headroom_usd``
    is the derived spendable amount for a recurring upgrade after the safety
    reserve. ``source`` records where the numbers came from for the rationale.
    """

    available_cash_usd: float = 0.0
    mrr_usd: float = 0.0
    headroom_usd: float = 0.0
    source: str = "unknown"

    def to_dict(self) -> dict:
        return {
            "available_cash_usd": round(self.available_cash_usd, 2),
            "mrr_usd": round(self.mrr_usd, 2),
            "headroom_usd": round(self.headroom_usd, 2),
            "source": self.source,
        }


def _compute_headroom(available_cash_usd: float, mrr_usd: float) -> float:
    """Spendable headroom for a recurring upgrade.

    Prefer live cash minus a safety reserve. If cash is unknown/zero but MRR is
    known, fall back to a conservative slice of one month's MRR (a recurring
    commitment should be affordable out of recurring revenue). Never negative.
    """
    if available_cash_usd > 0:
        return round(max(0.0, available_cash_usd * (1.0 - _SAFETY_RESERVE_PCT)), 2)
    if mrr_usd > 0:
        return round(max(0.0, mrr_usd * _MRR_HEADROOM_PCT), 2)
    return 0.0


def read_financials() -> Financials:
    """Best-effort snapshot of cash + MRR, reusing existing finance accessors.

    Resolution order (each step fail-soft):
      1. Finance service ``/runway`` + ``/snapshot`` over a signed request —
         the SAME path :func:`intelligence_cycle.gather_production_state` uses.
         This is the only place holding the Stripe key in production.
      2. In-process finance ``get_snapshot`` (when running INSIDE the finance
         workcell, which does hold the key).
      3. :mod:`backend.common.llm_budget_scaler` MRR fetch (cached ~1h) — the
         same MRR accessor the LLM budget scaler already uses.

    Never raises. On total failure returns zeros with ``source="unavailable"``.
    """
    cash: Optional[float] = None
    mrr: Optional[float] = None
    source = "unavailable"

    # (1) finance service over signed HTTP (gateway/cognition runtime).
    try:
        from backend.common.config import get_settings
        from backend.common.http_client import signed_post_json_sync

        fin_url = os.environ.get("FINANCE_URL") or (
            get_settings().gateway_urls or {}
        ).get("finance")
        if fin_url:
            resp = signed_post_json_sync(fin_url, "/runway", {}, timeout=8.0)
            if resp.status_code == 200:
                rj = resp.json()
                if rj.get("available_balance_usd") is not None:
                    cash = float(rj["available_balance_usd"])
                    source = "finance_service_runway"
            # MRR lives on /snapshot, not /runway.
            sresp = signed_post_json_sync(fin_url, "/snapshot", {}, timeout=8.0)
            if sresp.status_code == 200:
                sj = sresp.json()
                if sj.get("mrr_usd") is not None:
                    mrr = float(sj["mrr_usd"])
                    if cash is None and sj.get("balance"):
                        # derive available cash from the embedded balance block
                        try:
                            avail = sj["balance"].get("available") or []
                            cents = sum(
                                l.get("amount", 0) for l in avail
                                if str(l.get("currency", "")).lower() == "usd"
                            )
                            cash = round(cents / 100.0, 2)
                        except Exception:  # noqa: BLE001
                            pass
                    source = "finance_service_snapshot"
    except Exception as exc:  # noqa: BLE001 — never block on finance transport
        log.debug("codb_reasoner: finance service unreachable: %s", exc)

    # (2) in-process finance snapshot (running inside the finance workcell).
    if cash is None or mrr is None:
        try:
            from backend.finance.models import SnapshotRequest
            from backend.finance.service import get_snapshot

            snap = get_snapshot(SnapshotRequest())
            if mrr is None:
                mrr = float(snap.mrr_usd or 0.0)
            if cash is None and snap.balance is not None:
                cash = float(snap.balance.available_usd_dollars())
            source = "finance_inprocess_snapshot" if source == "unavailable" else source
        except Exception as exc:  # noqa: BLE001
            log.debug("codb_reasoner: in-process finance snapshot failed: %s", exc)

    # (3) llm_budget_scaler MRR fetch — the shared MRR accessor (cached ~1h).
    if mrr is None:
        try:
            from backend.common import llm_budget_scaler

            mrr = float(llm_budget_scaler._refresh_if_stale())
            if source == "unavailable":
                source = "llm_budget_scaler_mrr"
        except Exception as exc:  # noqa: BLE001
            log.debug("codb_reasoner: llm_budget_scaler MRR fetch failed: %s", exc)

    cash_f = float(cash or 0.0)
    mrr_f = float(mrr or 0.0)
    return Financials(
        available_cash_usd=cash_f,
        mrr_usd=mrr_f,
        headroom_usd=_compute_headroom(cash_f, mrr_f),
        source=source,
    )


# ---------------------------------------------------------------------------
# The recommendation shape.
# ---------------------------------------------------------------------------
@dataclass
class InvestmentRecommendation:
    """One ranked, affordable CODB scale-up recommendation."""

    option_id: str
    name: str
    monthly_cost_usd: float
    one_time_cost_usd: float
    effective_cost_usd: float
    bottleneck: str
    capability_gain: float
    roi_per_dollar: float          # capability_gain / effective_cost
    bottleneck_match: bool         # relieves the current binding constraint
    rationale: str                 # plain-English "why this, why now"
    numbers: dict = field(default_factory=dict)  # the figures used

    def to_dict(self) -> dict:
        return {
            "option_id": self.option_id,
            "name": self.name,
            "monthly_cost_usd": self.monthly_cost_usd,
            "one_time_cost_usd": self.one_time_cost_usd,
            "effective_cost_usd": self.effective_cost_usd,
            "bottleneck": self.bottleneck,
            "capability_gain": self.capability_gain,
            "roi_per_dollar": round(self.roi_per_dollar, 4),
            "bottleneck_match": self.bottleneck_match,
            "rationale": self.rationale,
            "numbers": self.numbers,
        }


# Bonus multiplier applied to ROI when an option relieves the CURRENT binding
# constraint (the bottleneck_hint). This is what makes the reasoner
# bottleneck-AWARE: an option that unblocks the thing we're actually stuck on
# outranks a marginally cheaper option that relieves a non-binding constraint.
_BOTTLENECK_MATCH_BONUS = float(os.getenv("SAMUS_CODB_BOTTLENECK_BONUS", "3.0"))


def _load_options() -> List[Any]:
    """Load the declarative investment catalog from the CODB registry.

    Fail-soft: returns [] if the registry can't be read or has no investments.
    """
    try:
        from backend.finance.codb import load_registry

        return list(load_registry().codb_investments)
    except Exception as exc:  # noqa: BLE001
        log.warning("codb_reasoner: failed to load investment catalog: %s", exc)
        return []


def _build_rationale(opt: Any, fin: Financials, is_match: bool) -> str:
    """Plain-English 'why this, why now' string with the numbers inline."""
    cost = opt.effective_cost_usd()
    cost_phrase = (
        f"${opt.monthly_cost_usd:.0f}/mo" if opt.monthly_cost_usd > 0
        else f"${opt.one_time_cost_usd:.0f} one-time"
    )
    cash_phrase = (
        f"available headroom ${fin.headroom_usd:.0f} "
        f"(cash ${fin.available_cash_usd:.0f}, MRR ${fin.mrr_usd:.0f}/mo)"
    )
    lead = (
        f"{opt.bottleneck.replace('-', ' ')} is the binding constraint; "
        if is_match else ""
    )
    gain = (
        f"~{opt.capability_gain:.0f}x capability gain toward {opt.bottleneck}"
        if opt.capability_gain and opt.capability_gain != 1.0
        else f"relieves {opt.bottleneck}"
    )
    state = (opt.current_state or "").strip().replace("\n", " ")
    tail = f" Current state: {state}" if state else ""
    return (
        f"{lead}{cash_phrase} covers {opt.name} ({cost_phrase}) -> {gain}. "
        f"Recommend upgrade (cost ${cost:.0f} <= headroom ${fin.headroom_usd:.0f})."
        f"{tail}"
    )


def recommend_codb_investments(
    financials: Optional[Financials] = None,
    *,
    bottleneck_hint: Optional[str] = None,
    options: Optional[List[Any]] = None,
) -> List[InvestmentRecommendation]:
    """Rank AFFORDABLE CODB scale-ups by capability-gain-per-dollar.

    Steps:
      1. Resolve financials (arg or :func:`read_financials`).
      2. Filter to options whose ``effective_cost`` <= available headroom.
      3. Score each: ROI = capability_gain / effective_cost, with a bonus
         multiplier when the option relieves ``bottleneck_hint`` (the current
         binding constraint) — making the reasoner bottleneck-aware.
      4. Return sorted desc by (bottleneck_match, score), each with a
         plain-English rationale and the numbers used.

    RECOMMEND-ONLY: pure computation. No side effects, no I/O beyond the
    optional financials read. Never spends.
    """
    fin = financials if financials is not None else read_financials()
    opts = options if options is not None else _load_options()
    hint = (bottleneck_hint or "").strip().lower()

    out: List[InvestmentRecommendation] = []
    for opt in opts:
        cost = opt.effective_cost_usd()
        # Affordability gate: a zero-cost/config-only option is always
        # affordable; anything with a cost must fit inside the headroom.
        if cost > 0 and cost > fin.headroom_usd:
            continue
        if cost <= 0:
            continue  # nothing to recommend buying if there's no cost signal

        is_match = bool(hint) and opt.bottleneck.strip().lower() == hint
        base_roi = opt.capability_gain / cost if cost > 0 else 0.0
        score = base_roi * (_BOTTLENECK_MATCH_BONUS if is_match else 1.0)

        rec = InvestmentRecommendation(
            option_id=opt.id,
            name=opt.name,
            monthly_cost_usd=round(opt.monthly_cost_usd, 2),
            one_time_cost_usd=round(opt.one_time_cost_usd, 2),
            effective_cost_usd=cost,
            bottleneck=opt.bottleneck,
            capability_gain=opt.capability_gain,
            roi_per_dollar=score,
            bottleneck_match=is_match,
            rationale=_build_rationale(opt, fin, is_match),
            numbers={
                **fin.to_dict(),
                "effective_cost_usd": cost,
                "capability_gain": opt.capability_gain,
                "base_roi_per_dollar": round(base_roi, 4),
                "bottleneck_match_bonus": _BOTTLENECK_MATCH_BONUS if is_match else 1.0,
            },
        )
        out.append(rec)

    # bottleneck-matching options first, then by score desc, then cheapest.
    out.sort(key=lambda r: (r.bottleneck_match, r.roi_per_dollar, -r.effective_cost_usd),
             reverse=True)
    return out


# ---------------------------------------------------------------------------
# Surfacing — emit recommendations as guidance-ledger records.
# ---------------------------------------------------------------------------
def emit_recommendations_to_guidance(
    recommendations: List[InvestmentRecommendation],
    *,
    briefing_id: Optional[str] = None,
    ledger: Optional[Any] = None,
    top_n: int = 3,
) -> List[str]:
    """Write the top-N recommendations to the guidance ledger as records.

    Reuses the existing :func:`ingest_guidance` pipeline so a CODB
    recommendation is a first-class, tracked, operator-reviewable guidance
    record (PROPOSED status — never auto-accepted, never auto-executed). This
    is the "wire-not-arm" surface: it SURFACES the recommendation; the operator
    accepts + spends. Returns the created recommendation_ids. Never raises.
    """
    if not recommendations:
        return []
    try:
        from .guidance import GuidanceLedger, ingest_guidance
    except Exception as exc:  # noqa: BLE001
        log.warning("codb_reasoner: guidance import failed: %s", exc)
        return []

    led = ledger if ledger is not None else GuidanceLedger()
    bid = briefing_id or f"codb-reasoner-{uuid.uuid4().hex[:8]}"

    items = []
    for rec in recommendations[:top_n]:
        cost_phrase = (
            f"${rec.monthly_cost_usd:.0f}/mo" if rec.monthly_cost_usd > 0
            else f"${rec.one_time_cost_usd:.0f} one-time"
        )
        items.append({
            "category": "resource_efficiency",
            "recommendation": (
                f"Scale CODB: upgrade to {rec.name} ({cost_phrase}) to relieve "
                f"the {rec.bottleneck} bottleneck (operator approves spend)."
            ),
            "rationale": rec.rationale,
            "expected_impact": "high" if rec.bottleneck_match else "medium",
            "feasibility": "high",
            "risk_level": "low",
            "suggested_owner": "finance",
            "action_steps": [
                f"Operator: approve + purchase {rec.name} ({cost_phrase}).",
                f"Confirm the {rec.bottleneck} constraint is relieved after upgrade.",
            ],
            "source_question": "codb_investment_reasoner",
        })

    try:
        records = ingest_guidance(bid, {"recommendations": items}, ledger=led)
        return [r.recommendation_id for r in records]
    except Exception as exc:  # noqa: BLE001
        log.warning("codb_reasoner: guidance emission failed: %s", exc)
        return []


# ---------------------------------------------------------------------------
# Public entry points — EOD section + deal-closed stimulus.
# ---------------------------------------------------------------------------
def build_eod_section(
    *,
    financials: Optional[Financials] = None,
    bottleneck_hint: Optional[str] = None,
    ledger: Optional[Any] = None,
    emit: bool = True,
    top_n: int = 3,
) -> dict:
    """Compose the ``codb_investment_recommendations`` block for the EOD review.

    When ``emit`` is True the top recommendations are ALSO written to the
    guidance ledger. Returns a JSON-able dict; never raises.
    """
    try:
        fin = financials if financials is not None else read_financials()
        recs = recommend_codb_investments(fin, bottleneck_hint=bottleneck_hint)
        emitted: List[str] = []
        if emit and recs:
            emitted = emit_recommendations_to_guidance(
                recs, ledger=ledger, top_n=top_n
            )
        # Observed bill signals — the reasoner + operator both see actual
        # vendor charges from Gmail alongside the estimated CODB, so an
        # investment decision can be judged against real burn and any
        # active payment declines are a top-of-mind data point.
        observed: dict = {}
        try:
            from backend.finance.observed_bills import summarize_observed_bills
            observed = summarize_observed_bills().to_dict()
        except Exception as _obs_exc:  # noqa: BLE001
            log.debug("codb_reasoner: observed_bills read failed: %s", _obs_exc)

        # Concept 1 — consult precedent for this CODB investment situation.
        # Attaches a compact precedent block to the EOD section so the human
        # reviewer sees "we already recommended X for this bottleneck last
        # month" rather than treating every recommendation as fresh.
        precedent_summary: dict = {"mode": "proceed_novel", "beliefs": [],
                                   "decisions": []}
        try:
            from backend.cognitive.intelligence_cycle import consult_precedent

            hint = bottleneck_hint or "unspecified"
            p = consult_precedent(f"codb investment bottleneck {hint}")
            precedent_summary = {
                "mode": p.get("mode", "proceed_novel"),
                "rationale": p.get("rationale", ""),
                "belief_ids": [getattr(b, "belief_id", "")
                               for b in (p.get("beliefs") or [])
                               if getattr(b, "belief_id", "")],
                "decision_ids": [getattr(d, "adr_id", "") or ""
                                 for d in (p.get("decisions") or [])],
            }
        except Exception as _p_exc:  # noqa: BLE001
            log.debug("codb_reasoner: precedent unavailable: %s", _p_exc)

        return {
            "financials": fin.to_dict(),
            "bottleneck_hint": bottleneck_hint,
            "count": len(recs),
            "recommendations": [r.to_dict() for r in recs[:top_n]],
            "top": recs[0].to_dict() if recs else None,
            "emitted_recommendation_ids": emitted,
            "observed_bills": observed,
            "precedent": precedent_summary,
        }
    except Exception as exc:  # noqa: BLE001 — EOD must never crash
        log.warning("codb_reasoner: build_eod_section failed: %s", exc)
        return {"error": f"{type(exc).__name__}: {exc}", "recommendations": []}


def on_deal_closed(
    *,
    mrr_delta_usd: float = 0.0,
    amount_usd: float = 0.0,
    bottleneck_hint: Optional[str] = "cold-email-reach",
    ledger: Optional[Any] = None,
    top_n: int = 3,
) -> dict:
    """Financial-stimulus entry point: a deal closed / MRR increased.

    Called (best-effort) when finance records a closed deal so Samus surfaces a
    fresh CODB investment recommendation while there is cash to act on. Honours
    the ``codb_reasoner_enabled`` flag — a no-op when disarmed (wire-not-arm).
    RECOMMEND-ONLY: emits guidance records, never spends. Never raises.
    """
    try:
        from backend.common.config import get_settings

        if not getattr(get_settings(), "codb_reasoner_enabled", False):
            return {"enabled": False, "emitted_recommendation_ids": []}
    except Exception as exc:  # noqa: BLE001
        log.debug("codb_reasoner: settings read failed, treating as disabled: %s", exc)
        return {"enabled": False, "emitted_recommendation_ids": []}

    fin = read_financials()
    recs = recommend_codb_investments(fin, bottleneck_hint=bottleneck_hint)
    bid = f"deal-closed-{iso_now()[:10]}-{uuid.uuid4().hex[:8]}"
    emitted = emit_recommendations_to_guidance(
        recs, briefing_id=bid, ledger=ledger, top_n=top_n
    )
    log.info(
        "codb_reasoner: deal-closed stimulus mrr_delta=%.2f amount=%.2f "
        "-> %d recs, %d emitted",
        mrr_delta_usd, amount_usd, len(recs), len(emitted),
    )
    return {
        "enabled": True,
        "financials": fin.to_dict(),
        "count": len(recs),
        "top": recs[0].to_dict() if recs else None,
        "emitted_recommendation_ids": emitted,
    }


__all__ = [
    "Financials",
    "InvestmentRecommendation",
    "read_financials",
    "recommend_codb_investments",
    "emit_recommendations_to_guidance",
    "build_eod_section",
    "on_deal_closed",
]
