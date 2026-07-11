"""Read-only domain perception + propose-only ACT artifact (build-plan Phase E).

This is the small, additive helper the :class:`backend.cognitive.cognitive_loop.
CognitiveLoop` uses to make its PERCEIVE stage read REAL domain state and its
ACT stage record a STRUCTURED PROPOSAL — **never** to call an effector or a
governance gate. It is the [SAFE] half of the autonomy layer's
perceive→act path: SENSE reads, ACT proposes-only.

Two collaborators, both duck-typed and OPTIONAL at the loop:

* a **domain provider** — a façade exposing read-only methods over the live
  CRM / decay / finance surfaces. The loop is handed one (or ``None``).
  ``None`` => PERCEIVE falls back to persona-only, exactly as the prior
  phase, so the bare loop and every existing test are unchanged. The
  production façade is :func:`build_live_domain_provider`; tests inject a
  STUB instead, so importing this module touches no backend.

* a **proposal sink** — where ACT appends its proposal artifact. Defaults to
  the JSONL ledger at ``state_path("cognition","proposals.jsonl")``.

Authoritative-gate boundary (build-plan): autonomy PROPOSES, gates DISPOSE.
NOTHING here imports or calls ``cash_engine.gate``, ``review_opportunity``,
``cash_engine.stages``, the outreach send path, or any ``finance`` mutation /
network call. The live provider only ever calls the read-only service
functions enumerated below, and ACT only ever appends a record.

Read-only domain surfaces consumed by :func:`build_live_domain_provider`
(every one is a documented read; each wrapped so a failure degrades to a
partial/empty perception and NEVER raises):

  * ``crm.service.list_follow_ups_due(today=)``          — follow-up call queue
  * ``crm.service.list_opportunities_pending_stake()``   — deals awaiting a stake
  * ``crm.service.list_operator_tasks(status="open")``   — open operator tasks
  * ``crm.service.list_opportunities()`` + ``get_call_state()``
        feeding ``cash_engine.decay.compute_decay_risk(...).crosses(thr)``
                                                          — decay-risk crossing
  * ``finance.service.get_runway()``                     — runway days / alert
  * ``finance.service.get_actions_summary()``            — finance action backlog
  * ``finance.service.get_declines_summary()``           — cash-distress signal

DORMANCY: this module has NO live importer. It is wired to a caller only by
the (later, operator-gated) Phase-F entry point.
"""

from __future__ import annotations

import logging
from typing import Any, Optional, Protocol

from backend.common.dates import iso_now
from backend.common.persistence import JsonlLedger
from backend.common.state_paths import state_path

log = logging.getLogger(__name__)

# Default decay-risk threshold for the "a deal is bleeding" perception signal.
# Mirrors the cash_engine signal_decay trigger's posture (proposal-stage deals
# crossing a high-ish risk). Advisory only — PERCEIVE never acts on it.
_DEFAULT_DECAY_THRESHOLD = 0.6
# Conservative scan ceilings so perception stays cheap and bounded.
_OPP_SCAN_LIMIT = 100
_DEFAULT_FOLLOWUP_LIMIT = 100
_DEFAULT_TASK_LIMIT = 50


# ---------------------------------------------------------------------------
# Provider protocol (duck-typed; the loop holds Optional[Any])
# ---------------------------------------------------------------------------
class DomainProvider(Protocol):
    """Read-only perception surface. All methods best-effort, never raise.

    Each returns a small JSON-able dict (or a primitive) describing one slice
    of live domain state. A STUB implementation in tests returns canned dicts;
    the production implementation (:func:`build_live_domain_provider`) reads the
    real services. A missing method simply drops that perception slice.
    """

    def follow_ups_due(self) -> dict: ...
    def opportunities_pending_stake(self) -> dict: ...
    def open_operator_tasks(self) -> dict: ...
    def decay_risk(self) -> dict: ...
    def finance_runway(self) -> dict: ...
    def finance_actions(self) -> dict: ...
    def cash_distress(self) -> dict: ...
    # Perception-grounding slices: the TOP actionable item WITH its ids so the
    # REASON prompt can name a concrete prospect and ACT can carry a prospect_id.
    def top_pending_stake(self) -> dict: ...
    def top_follow_up(self) -> dict: ...


# Ordered (perception-key -> provider-method) map. PERCEIVE walks this so a
# provider exposing only a subset of methods yields a partial perception, and
# adding a slice later is a one-line table edit.
_PERCEPTION_SLICES: tuple[tuple[str, str], ...] = (
    ("follow_ups_due", "follow_ups_due"),
    ("opportunities_pending_stake", "opportunities_pending_stake"),
    ("open_operator_tasks", "open_operator_tasks"),
    ("decay_risk", "decay_risk"),
    ("finance_runway", "finance_runway"),
    ("finance_actions", "finance_actions"),
    ("cash_distress", "cash_distress"),
    # Perception-grounding: the TOP actionable item WITH ids. Surfaced under
    # clear keys so REASON can render a concrete target and ACT can carry the
    # prospect_id into the proposal intent. Both stay read-only.
    ("top_pending_stake", "top_pending_stake"),
    ("top_follow_up", "top_follow_up"),
)


def gather_perception(
    provider: Optional[Any],
    *,
    threshold: float = _DEFAULT_DECAY_THRESHOLD,
) -> dict:
    """Aggregate the read-only domain perception from ``provider``.

    Best-effort by contract: every slice is wrapped so that a provider whose
    method is missing, returns junk, or RAISES (backend unavailable, DDB down,
    Stripe error) degrades to a recorded ``{"error": ...}`` stub for that slice
    and never aborts the whole perception. A ``None`` provider yields an empty
    perception ``{}`` (persona-only fallback). NEVER raises.

    Returns a dict whose top-level keys are the perception slices that were
    attempted, each mapping to that slice's small dict, plus a ``_meta`` key
    summarising availability so a caller (and the PERCEIVE StageResult) can see
    at a glance which surfaces were live.
    """
    if provider is None:
        return {}

    perception: dict[str, Any] = {}
    available: list[str] = []
    degraded: list[str] = []
    for key, method_name in _PERCEPTION_SLICES:
        fn = getattr(provider, method_name, None)
        if not callable(fn):
            # Provider simply doesn't expose this slice — skip silently.
            continue
        try:
            # decay_risk is the only slice that takes the tunable threshold;
            # call it positionally only when the provider accepts it.
            if method_name == "decay_risk":
                slice_val = _call_decay(fn, threshold)
            else:
                slice_val = fn()
            perception[key] = slice_val if isinstance(slice_val, dict) else {"value": slice_val}
            available.append(key)
        except Exception as exc:  # noqa: BLE001 — perception is fail-safe
            # A single source failing must not lose the rest of the perception.
            log.warning("domain perception slice %s failed: %s", key, exc)
            perception[key] = {"error": f"{type(exc).__name__}: {exc}"}
            degraded.append(key)

    perception["_meta"] = {
        "available": available,
        "degraded": degraded,
        "partial": bool(degraded) or len(available) < len(_PERCEPTION_SLICES),
    }
    return perception


def _call_decay(fn: Any, threshold: float) -> Any:
    """Call a decay_risk slice, passing the threshold when it is accepted."""
    try:
        return fn(threshold=threshold)
    except TypeError:
        # Provider's decay_risk takes no threshold kwarg — call bare.
        return fn()


# ---------------------------------------------------------------------------
# Production live provider — read-only façade over the real services
# ---------------------------------------------------------------------------
def build_live_domain_provider(
    *,
    decay_threshold: float = _DEFAULT_DECAY_THRESHOLD,
    follow_up_limit: int = _DEFAULT_FOLLOWUP_LIMIT,
    task_limit: int = _DEFAULT_TASK_LIMIT,
    opp_scan_limit: int = _OPP_SCAN_LIMIT,
) -> "LiveDomainProvider":
    """Construct the production :class:`LiveDomainProvider`.

    Imports of the heavy services are LAZY (inside the methods) so importing
    *this* module — which the dormancy test does transitively — never reaches
    the CRM/finance/decay backends. Only a Phase-F caller constructs this.
    """
    return LiveDomainProvider(
        decay_threshold=decay_threshold,
        follow_up_limit=follow_up_limit,
        task_limit=task_limit,
        opp_scan_limit=opp_scan_limit,
    )


class LiveDomainProvider:
    """Read-only perception façade over the live CRM / decay / finance reads.

    Every method is fail-safe: it returns a small dict and NEVER raises, so the
    aggregator's per-slice guard is belt-and-braces. Each method imports its
    service lazily and calls ONLY the read-only entrypoints documented in the
    module header — no gate, no effector, no write, no network beyond what the
    read itself performs.
    """

    def __init__(
        self,
        *,
        decay_threshold: float = _DEFAULT_DECAY_THRESHOLD,
        follow_up_limit: int = _DEFAULT_FOLLOWUP_LIMIT,
        task_limit: int = _DEFAULT_TASK_LIMIT,
        opp_scan_limit: int = _OPP_SCAN_LIMIT,
    ) -> None:
        self._decay_threshold = decay_threshold
        self._follow_up_limit = follow_up_limit
        self._task_limit = task_limit
        self._opp_scan_limit = opp_scan_limit

    # ---- CRM reads -----------------------------------------------------
    def follow_ups_due(self) -> dict:
        from backend.crm import service as crm

        today = iso_now()[:10]
        res = crm.list_follow_ups_due(today=today, limit=self._follow_up_limit)
        return {
            "count": getattr(res, "count", 0),
            "ddb_error": getattr(res, "ddb_error", None),
            "scan_truncated": getattr(res, "scan_truncated", False),
        }

    def opportunities_pending_stake(self) -> dict:
        from backend.crm import service as crm

        pending = crm.list_opportunities_pending_stake(limit=self._task_limit)
        return {"count": len(pending or [])}

    def top_pending_stake(self) -> dict:
        """The single highest-priority opportunity awaiting a stake, WITH ids.

        ``list_opportunities_pending_stake`` already returns the queue oldest
        first (the order the operator works it), so the first row is the top
        actionable deal. Read-only: this only reads the list; it does NOT stake,
        advance, or gate the opportunity. Returns ``{}`` when nothing is pending.
        """
        from backend.crm import service as crm

        pending = crm.list_opportunities_pending_stake(limit=self._task_limit)
        if not pending:
            return {}
        opp = pending[0]
        return {
            "prospect_id": getattr(opp, "prospect_id", "") or "",
            "opportunity_id": getattr(opp, "opportunity_id", "") or "",
            "name": getattr(opp, "name", "") or "",
            "stage": getattr(opp, "stage", "") or "",
            "deal_size_usd": getattr(opp, "deal_size_usd", 0.0),
        }

    def top_follow_up(self) -> dict:
        """The next follow-up call due, WITH its prospect_id.

        ``list_follow_ups_due`` returns the queue sorted by follow-up date then
        company, so the first row is the most-due call. Read-only: it only reads
        the follow-up queue; it places no call and logs no outcome. Returns
        ``{}`` when nothing is due.
        """
        from backend.crm import service as crm

        today = iso_now()[:10]
        res = crm.list_follow_ups_due(today=today, limit=self._follow_up_limit)
        follow_ups = getattr(res, "follow_ups", []) or []
        if not follow_ups:
            return {}
        fu = follow_ups[0]
        return {
            "prospect_id": getattr(fu, "prospect_id", "") or "",
            "company": getattr(fu, "company", "") or "",
            "channel": getattr(fu, "channel", "") or "",
            "follow_up_on": getattr(fu, "follow_up_on", "") or "",
            "days_waiting": getattr(fu, "days_waiting", 0),
        }

    def open_operator_tasks(self) -> dict:
        from backend.crm import service as crm

        res = crm.list_operator_tasks(status="open", limit=self._task_limit)
        return {
            "count": getattr(res, "count", 0),
            "ddb_error": getattr(res, "ddb_error", None),
        }

    def decay_risk(self, *, threshold: Optional[float] = None) -> dict:
        """Count open opportunities whose decay risk crosses ``threshold``.

        Reads the opportunity list + each prospect's CallState (both read-only)
        and runs the PURE :func:`compute_decay_risk` (no I/O) per deal. ``crosses``
        already excludes terminal deals. Purely observational — nothing here
        fires the signal_decay trigger or touches the cash_engine gate.
        """
        from backend.cash_engine.decay import compute_decay_risk
        from backend.crm import service as crm

        thr = self._decay_threshold if threshold is None else threshold
        res = crm.list_opportunities(limit=self._opp_scan_limit)
        opps = getattr(res, "opportunities", []) or []
        crossing = 0
        worst = 0.0
        assessed = 0
        for opp in opps:
            try:
                call_state = crm.get_call_state(getattr(opp, "prospect_id", "") or "")
                assessment = compute_decay_risk(opp, call_state=call_state)
                assessed += 1
                worst = max(worst, float(getattr(assessment, "decay_risk", 0.0)))
                if assessment.crosses(thr):
                    crossing += 1
            except Exception as exc:  # noqa: BLE001 — one bad deal can't sink the slice
                log.debug("decay assessment skipped for one opp: %s", exc)
        return {
            "assessed": assessed,
            "crossing": crossing,
            "worst_risk": round(worst, 4),
            "threshold": thr,
            "ddb_error": getattr(res, "ddb_error", None),
        }

    # ---- finance reads -------------------------------------------------
    def finance_runway(self) -> dict:
        from backend.finance import service as finance

        runway = finance.get_runway()
        return {
            "days_of_runway": getattr(runway, "days_of_runway", None),
            "alert_triggered": bool(getattr(runway, "alert_triggered", False)),
            "available_balance_usd": getattr(runway, "available_balance_usd", 0.0),
        }

    def finance_actions(self) -> dict:
        from backend.finance import service as finance

        summary = finance.get_actions_summary()
        return {
            "open_total": getattr(summary, "open_total", 0),
            "overdue_count": getattr(summary, "overdue_count", 0),
            "due_today_count": getattr(summary, "due_today_count", 0),
        }

    def cash_distress(self) -> dict:
        from backend.finance import service as finance

        summary = finance.get_declines_summary()
        return {
            "cash_distress": getattr(summary, "cash_distress", "ok"),
            "distress_reasons": list(getattr(summary, "distress_reasons", []) or []),
        }


# ---------------------------------------------------------------------------
# REASON grounding — render the perception state as compact prompt context.
# ---------------------------------------------------------------------------
def render_perception_context(perception: dict) -> str:
    """Render the read-only perception as a compact, readable CONTEXT block.

    Produces a few short lines naming the concrete business state — runway /
    cash distress + the SPECIFIC top opportunity-pending-stake and next
    follow-up WITH their prospect_ids — so the REASON LLM grounds its
    highest-leverage-action answer in real state instead of guessing. Returns
    an empty string for an empty/absent perception (the no-domain path), so the
    caller can cleanly fall back to the bare directive prompt. NEVER raises.
    """
    if not isinstance(perception, dict) or not perception:
        return ""
    lines: list[str] = []
    try:
        runway = perception.get("finance_runway") or {}
        if isinstance(runway, dict) and runway and "error" not in runway:
            days = runway.get("days_of_runway")
            alert = bool(runway.get("alert_triggered"))
            if days is not None or alert:
                lines.append(f"- Runway: {days} days" + (" [ALERT]" if alert else ""))
        distress = perception.get("cash_distress") or {}
        if isinstance(distress, dict):
            cd = distress.get("cash_distress")
            if cd and cd != "ok":
                lines.append(f"- Cash distress: {cd}")

        stake = perception.get("top_pending_stake") or {}
        if isinstance(stake, dict) and stake.get("prospect_id"):
            name = stake.get("name") or "(unnamed deal)"
            lines.append(
                f"- Top opportunity pending stake: {name} "
                f"(prospect_id={stake.get('prospect_id')}, "
                f"opportunity_id={stake.get('opportunity_id') or 'n/a'}, "
                f"stage={stake.get('stage') or 'n/a'})"
            )
        follow = perception.get("top_follow_up") or {}
        if isinstance(follow, dict) and follow.get("prospect_id"):
            company = follow.get("company") or "(unknown company)"
            lines.append(
                f"- Next follow-up due: {company} "
                f"(prospect_id={follow.get('prospect_id')}, "
                f"due={follow.get('follow_up_on') or 'n/a'}, "
                f"waiting={follow.get('days_waiting', 0)}d)"
            )

        # Counts as a one-line backdrop so REASON sees the queue depth.
        pending_ct = (perception.get("opportunities_pending_stake") or {}).get("count")
        follow_ct = (perception.get("follow_ups_due") or {}).get("count")
        tasks_ct = (perception.get("open_operator_tasks") or {}).get("count")
        decay_ct = (perception.get("decay_risk") or {}).get("crossing")
        backdrop = []
        if pending_ct:
            backdrop.append(f"{pending_ct} deals pending stake")
        if follow_ct:
            backdrop.append(f"{follow_ct} follow-ups due")
        if tasks_ct:
            backdrop.append(f"{tasks_ct} open tasks")
        if decay_ct:
            backdrop.append(f"{decay_ct} deals crossing decay risk")
        if backdrop:
            lines.append("- Queues: " + ", ".join(backdrop))
    except Exception as exc:  # noqa: BLE001 — rendering is best-effort
        log.warning("render_perception_context failed: %s", exc)

    if not lines:
        return ""
    return "Current business state:\n" + "\n".join(lines)


# ---------------------------------------------------------------------------
# Propose-only ACT — record a structured proposal artifact (NO effector)
# ---------------------------------------------------------------------------
def _proposals_ledger() -> JsonlLedger:
    """The JSONL sink for ACT proposals: state_path('cognition','proposals.jsonl')."""
    return JsonlLedger(state_path("cognition", "proposals.jsonl"))


def build_proposal(
    *,
    plan_token: str,
    channel: str,
    reply: str,
    perception: dict,
    reflect_score: float,
) -> dict:
    """Shape the structured PROPOSAL artifact from the cycle's outputs.

    A would-be commercial intent derived from perception + reasoning — for the
    operator / a (Phase-G, gated) promoter to dispose of. It is NOT an action:
    it carries ``status="proposed"`` and an explicit human-readable marker that
    it has NOT been actioned and must still pass every governance gate.

    ``intent`` is a best-effort, deterministic hint at what the next move would
    be, chosen from the live perception (a pending-stake deal -> a "stake/
    outreach" intent; a runway alert / cash distress -> a "finance attention"
    intent; otherwise a neutral "observe"). This is advisory metadata only.
    """
    intent = _derive_intent(perception)
    return {
        "ts": iso_now(),
        "kind": "cognition_proposal",
        "status": "proposed",  # never "approved"/"actioned"
        "actioned": False,  # load-bearing: ACT records, never acts
        "plan_token": plan_token or "",
        "channel": channel or "",
        "intent": intent,
        "reply_preview": (reply or "")[:240],
        "reflect_score": round(float(reflect_score), 4),
        "perception_summary": _summarise_perception(perception),
        "note": (
            "PROPOSAL ONLY — recorded by the cognitive loop's propose-only ACT "
            "stage. No effector was called; this must still pass every "
            "governance gate (cash_engine, codex, EFH, compliance, karma) "
            "before any live action."
        ),
    }


def _derive_intent(perception: dict) -> dict:
    """Pick a single advisory next-move intent from the perception (no side effects)."""
    if not isinstance(perception, dict) or not perception:
        return {"type": "observe", "reason": "no_perception"}

    # Prefer a SPECIFIC actionable prospect (carries a prospect_id) FIRST — even
    # under a runway alert / cash distress, the right move is to advance the
    # concrete deal (revenue is how the runway recovers), so a prospect-targeted
    # intent MUST win over the generic finance_attention fallback near the end.
    # (Fix 2026-06-25: finance_attention previously returned early here and
    # starved Phase G of a prospect_id whenever runway_alert was set — the live
    # zero-runway case — so every grounded tick skipped 'no_prospect_id'.)
    # The top_* slices carry the ids; the count slices are the fallback when no
    # specific row was surfaced.
    top_stake = perception.get("top_pending_stake") or {}
    top_stake_pid = str(top_stake.get("prospect_id") or "").strip()
    pending = perception.get("opportunities_pending_stake") or {}
    if top_stake_pid:
        intent = {
            "type": "stake_opportunity",
            "reason": "opportunities_pending_stake",
            "prospect_id": top_stake_pid,
            "count": pending.get("count"),
        }
        opp_id = str(top_stake.get("opportunity_id") or "").strip()
        if opp_id:
            intent["opportunity_id"] = opp_id
        if top_stake.get("name"):
            intent["name"] = top_stake.get("name")
        return intent
    if int(pending.get("count", 0) or 0) > 0:
        # Count is positive but no specific row was surfaced (e.g. the top_* slice
        # degraded) — keep the generic stake intent (no prospect_id; promoter SKIPs).
        return {
            "type": "stake_then_outreach",
            "reason": "opportunities_pending_stake",
            "count": pending.get("count"),
        }

    top_follow = perception.get("top_follow_up") or {}
    top_follow_pid = str(top_follow.get("prospect_id") or "").strip()
    follow = perception.get("follow_ups_due") or {}
    if top_follow_pid:
        return {
            "type": "follow_up",
            "reason": "follow_ups_due",
            "prospect_id": top_follow_pid,
            "count": follow.get("count"),
        }
    if int(follow.get("count", 0) or 0) > 0:
        return {
            "type": "follow_up",
            "reason": "follow_ups_due",
            "count": follow.get("count"),
        }

    decay = perception.get("decay_risk") or {}
    if int(decay.get("crossing", 0) or 0) > 0:
        return {
            "type": "decay_intervention",
            "reason": "decay_risk_crossing",
            "crossing": decay.get("crossing"),
        }

    # Fallback: no specific actionable prospect/queue surfaced, but the finances
    # need attention. Intentionally LAST among the signal branches so a concrete
    # prospect (above) is always targeted first.
    runway = perception.get("finance_runway") or {}
    distress = perception.get("cash_distress") or {}
    if runway.get("alert_triggered") or distress.get("cash_distress") in ("degraded", "critical"):
        return {
            "type": "finance_attention",
            "reason": "runway_alert_or_cash_distress",
            "runway_days": runway.get("days_of_runway"),
            "cash_distress": distress.get("cash_distress"),
        }

    return {"type": "observe", "reason": "no_actionable_signal"}


def _summarise_perception(perception: dict) -> dict:
    """A compact, JSON-able digest of the perception for the proposal record."""
    if not isinstance(perception, dict):
        return {}
    meta = perception.get("_meta") or {}
    return {
        "available": list(meta.get("available", [])),
        "degraded": list(meta.get("degraded", [])),
        "follow_ups_due": (perception.get("follow_ups_due") or {}).get("count"),
        "pending_stake": (perception.get("opportunities_pending_stake") or {}).get("count"),
        "open_tasks": (perception.get("open_operator_tasks") or {}).get("count"),
        "decay_crossing": (perception.get("decay_risk") or {}).get("crossing"),
        "runway_alert": (perception.get("finance_runway") or {}).get("alert_triggered"),
        "cash_distress": (perception.get("cash_distress") or {}).get("cash_distress"),
        # Carry the surfaced top-item prospect_ids into the proposal record so
        # the audit trail ties the proposal back to the concrete deal/call.
        "top_pending_stake_prospect_id": (perception.get("top_pending_stake") or {}).get(
            "prospect_id"
        ),
        "top_follow_up_prospect_id": (perception.get("top_follow_up") or {}).get("prospect_id"),
    }


def record_proposal(proposal: dict, *, sink: Optional[Any] = None) -> bool:
    """Append a proposal artifact to the JSONL sink. Best-effort; never raises.

    ``sink`` is a duck-typed ledger exposing ``append(dict)`` (tests inject a
    spy / a temp ledger). ``None`` uses the default
    ``state_path("cognition","proposals.jsonl")`` ledger. Returns True on a
    successful append, False on any I/O error (logged, swallowed) — recording a
    proposal must never break the cycle.
    """
    try:
        ledger = sink if sink is not None else _proposals_ledger()
        ledger.append(proposal)
        return True
    except Exception as exc:  # noqa: BLE001 — recording is best-effort
        log.warning("cognition proposal append failed: %s", exc)
        return False


__all__ = [
    "DomainProvider",
    "LiveDomainProvider",
    "build_live_domain_provider",
    "gather_perception",
    "render_perception_context",
    "build_proposal",
    "record_proposal",
]
