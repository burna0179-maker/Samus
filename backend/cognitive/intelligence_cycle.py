"""Strategic intelligence cycle — the autonomous two-report cadence + feedback seam.

This module makes the Strategic Daily Intelligence and Guidance Cycle directive
ACTUALLY RUN, instead of living only in a hand-invoked script. It provides:

  * :func:`run_pre_shift_briefing` — Samus composes its intelligence package from
    live production state, consults OpenAI for strategic guidance, and INGESTS
    that guidance into the durable guidance ledger (classify → assess → track).
  * :func:`run_end_of_day_review` — Samus scores the day's guidance
    effectiveness and composes the Operational Review, then (optionally) consults
    OpenAI for tomorrow's guidance, which is itself ingested.
  * :func:`active_guidance_context` — THE FEEDBACK SEAM. Returns a compact,
    priority-ordered summary of currently-accepted guidance so the cognitive
    loop's REASON stage is grounded in what the operator's advisor recommended.
    This is the connection that was missing: OpenAI guidance → ingested →
    accepted → steers Samus's autonomous 60s reasoning.

Both reports are designed to MINIMIZE external model consumption (the directive's
prime objective): Samus composes the data-heavy sections LOCALLY (zero LLM cost)
and consults OpenAI only for the genuinely-reasoning-shaped strategic questions,
asking for STRUCTURED JSON so ingestion needs no second parsing call.

The LLM call is injectable (``llm=``) so tests run fully offline. All public
functions are fail-safe: a degraded data source or an LLM error yields a partial
report, never an exception that could crash a scheduled job or a cognitive tick.
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import date
from typing import Any, Callable, Optional

from backend.common.config import get_settings
from backend.common.dates import iso_now

from .directives import strategic_intelligence_cycle
from .guidance import GuidanceLedger, ingest_guidance
from .guidance_models import GuidanceStatus

log = logging.getLogger(__name__)

# Revenue target context (mirrors the cashflow-push project state).
_REVENUE_TARGET_USD = 40000
_REVENUE_TARGET_DATE = date(2026, 7, 12)


# ---------------------------------------------------------------------------
# Production-state gathering (local, zero LLM cost).
# ---------------------------------------------------------------------------
def _business_today_date() -> date:
    """Pacific business date. Falls back to UTC ``date.today()`` on fault so a
    broken timezone lookup never crashes the briefing (the whole cycle is
    designed to degrade rather than raise)."""
    try:
        from backend.common.us_timezones import business_today
        return business_today()
    except Exception:  # noqa: BLE001
        return date.today()


def _business_today_iso() -> str:
    """Pacific business date as ISO string."""
    return _business_today_date().isoformat()


def gather_production_state() -> dict:
    """Snapshot the live production state for the intelligence reports.

    Best-effort: each source degrades to an error note rather than raising.

    ``date`` is the BUSINESS (Pacific) date, matching every other date-stamped
    artifact in the pipeline (call lists, ledgers, attestations). Previously
    used ``date.today()`` which is UTC-naive and reads a day AHEAD all
    evening PT — the briefing_id + primer path stamped for "tomorrow" while
    the attestation (which correctly uses ``business_today``) stamped for
    "today". Downstream file lookups keyed off ``state["date"]`` then missed.
    """
    state: dict[str, Any] = {
        "date": _business_today_iso(),
        "timestamp_utc": iso_now(),
    }
    snap: dict[str, Any] = {}
    try:
        from backend.finance.codb import load_registry, summarize, compute_runway

        reg = load_registry()
        summary = summarize(reg, iso_now())
        state["codb"] = {
            "total_monthly_burn": summary.total_monthly_burn_usd,
            "by_category": [
                {"category": c.category, "monthly_usd": c.total_monthly_usd, "items": c.item_count}
                for c in summary.by_category
            ],
            "by_criticality": summary.by_criticality,
        }

        # Observed bill signals from Gmail: actual vendor charges + payment
        # declines from the classified intake ledger. Cross-referenced against
        # the registry above so reasoning sees observed vs. estimated variance
        # (not just human-authored estimates). Fail-soft — a missing intake
        # ledger degrades to an empty observed block, not an exception.
        try:
            from backend.finance.observed_bills import summarize_observed_bills
            observed = summarize_observed_bills()
            state["codb"]["observed"] = observed.to_dict()
        except Exception as _obs_exc:  # noqa: BLE001
            state["codb"]["observed_error"] = f"{type(_obs_exc).__name__}: {_obs_exc}"[:120]
        # Real runway from the finance service, which holds the Stripe key.
        # This module runs in the gateway (no Stripe key), so we ask finance
        # over a signed request instead of computing on a $500 PLACEHOLDER.
        # Fail-soft: if finance is unreachable, fall back to the local
        # placeholder computation so a report is never blocked.
        # One /snapshot call yields runway AND live MRR (active Stripe
        # subscriptions), so the briefing's revenue phase reflects reality
        # instead of the hardcoded "pre-revenue" it shipped with.
        rj: dict[str, Any] = {}
        try:
            import os as _os

            from backend.common.http_client import signed_post_json_sync

            _fin = _os.environ.get("FINANCE_URL") or (
                get_settings().gateway_urls or {}
            ).get("finance")
            if _fin:
                _resp = signed_post_json_sync(_fin, "/snapshot", {}, timeout=15.0)
                if _resp.status_code == 200:
                    snap = _resp.json() or {}
                    rj = dict(snap.get("runway") or {})
        except Exception as exc:  # noqa: BLE001 — never block a report on finance
            state["runway_source_error"] = f"{type(exc).__name__}: {exc}"[:120]

        if rj.get("available_balance_usd") is not None:
            state["runway"] = {
                "available_balance_usd": rj.get("available_balance_usd"),
                "days_remaining": rj.get("days_of_runway"),
                "alert_triggered": rj.get("alert_triggered"),
                "daily_burn_usd": rj.get("daily_burn_usd"),
                "source": "finance_stripe_live",
                # Stripe available balance is structurally ~$0 (payouts
                # auto-sweep to the bank), so a 0-day runway here means
                # "no cash visible to Stripe", NOT "no cash". Flag it so
                # downstream reasoning doesn't treat it as insolvency.
                "caveat": (
                    "stripe_balance_only_excludes_bank"
                    if float(rj.get("available_balance_usd") or 0.0) == 0.0
                    else None
                ),
            }
        else:
            rw = compute_runway(500.0, summary.total_monthly_burn_usd, 60)
            state["runway"] = {
                "available_balance_usd": rw.available_balance_usd,
                "days_remaining": rw.days_of_runway,
                "alert_triggered": rw.alert_triggered,
                "daily_burn_usd": rw.daily_burn_usd,
                "source": "placeholder_500",
            }
    except Exception as exc:  # noqa: BLE001
        state["codb_error"] = str(exc)

    # Revenue phase is DERIVED from live MRR (snapshot above), never
    # hardcoded: $300/mo of real retainer revenue was previously reported
    # as "pre-revenue" because this literal never updated.
    _mrr_usd = float(snap.get("mrr_usd") or 0.0) if isinstance(snap, dict) else 0.0
    _subs_n = int(snap.get("active_subscriptions_count") or 0) if isinstance(snap, dict) else 0
    state["revenue"] = {
        "phase": (
            f"revenue — ${_mrr_usd:,.0f}/mo MRR ({_subs_n} active subs)"
            if _mrr_usd > 0
            else "pre-revenue"
        ),
        "mrr_usd": _mrr_usd,
        "active_subscriptions": _subs_n,
        "target_usd": _REVENUE_TARGET_USD,
        "target_date": _REVENUE_TARGET_DATE.isoformat(),
        "days_to_target": (_REVENUE_TARGET_DATE - _business_today_date()).days,
    }
    # The day's actual voice + email interactions, compressed to signal — the
    # production half of the picture (what HAPPENED), not just financial state.
    try:
        from .production_interactions import compile_day_interactions

        state["interactions"] = compile_day_interactions()
    except Exception as exc:  # noqa: BLE001 — a report is never blocked on this
        state["interactions_error"] = str(exc)
    return state


# ---------------------------------------------------------------------------
# Local composition of the two reports.
# ---------------------------------------------------------------------------
def compose_pre_shift_briefing(state: dict) -> str:
    """Samus composes the pre-shift intelligence package (no LLM)."""
    codb = state.get("codb", {})
    runway = state.get("runway", {})
    rev = state.get("revenue", {})
    lines = [
        "# SAMUS PRE-SHIFT STRATEGIC BRIEFING",
        f"Date: {state.get('date')} | Generated: {state.get('timestamp_utc')}",
        "",
        "## EXECUTIVE SUMMARY",
        (
            f"Burn ${codb.get('total_monthly_burn', 0):.2f}/mo. "
            f"MRR ${rev.get('mrr_usd', 0):,.0f}/mo. "
            f"Runway {runway.get('days_remaining', '?')} days "
            f"(alert={'TRIGGERED' if runway.get('alert_triggered') else 'clear'}"
            f"{'; stripe-balance-only, excludes bank' if runway.get('caveat') else ''}). "
            f"Phase {rev.get('phase')}. "
            f"Target ${rev.get('target_usd', 0):,} in {rev.get('days_to_target', '?')} days."
        ),
        "",
        "## COST BREAKDOWN",
    ]
    for cat in codb.get("by_category", []):
        pct = (cat["monthly_usd"] / codb["total_monthly_burn"] * 100) if codb.get("total_monthly_burn") else 0
        lines.append(f"- {cat['category']}: ${cat['monthly_usd']:.2f}/mo ({pct:.0f}%) [{cat['items']} items]")

    # Observed bills — real vendor charges from Gmail, cross-referenced
    # against the registry estimates above. Payment declines land here first.
    obs = codb.get("observed") or {}
    if obs and obs.get("signals_scanned"):
        from backend.finance.observed_bills import (
            ObservedBillsSummary,
            VendorObservation,
            observed_bills_briefing_lines,
        )
        # Rebuild dataclass instances from the dict-form embedded in state.
        try:
            vendors = [VendorObservation(**{
                **v,
                "registry_estimate_usd": v.get("registry_estimate_usd"),
                "variance_usd": v.get("variance_usd"),
            }) for v in obs.get("vendors", [])]
            reconstructed = ObservedBillsSummary(
                window_days=obs.get("window_days", 30),
                signals_scanned=obs.get("signals_scanned", 0),
                total_observed_usd=obs.get("total_observed_usd", 0.0),
                total_registry_estimate_usd=obs.get("total_registry_estimate_usd", 0.0),
                total_variance_usd=obs.get("total_variance_usd", 0.0),
                payment_declined_active=obs.get("payment_declined_active", 0),
                unmatched_vendors_count=obs.get("unmatched_vendors_count", 0),
                vendors=vendors,
                bank_revenue_usd=obs.get("bank_revenue_usd", 0.0),
                bank_transfer_usd=obs.get("bank_transfer_usd", 0.0),
                bank_personal_usd=obs.get("bank_personal_usd", 0.0),
                bank_txn_count=obs.get("bank_txn_count", 0),
                bank_net_usd=obs.get("bank_net_usd", 0.0),
                founder_borrow_usd=obs.get("founder_borrow_usd", 0.0),
                founder_deposits_usd=obs.get("founder_deposits_usd", 0.0),
                founder_cash_card_usd=obs.get("founder_cash_card_usd", 0.0),
            )
            lines.append("")
            lines.extend(observed_bills_briefing_lines(reconstructed))
        except Exception:  # noqa: BLE001 — briefing must never crash
            pass

    # Concept 1 — precedent seam. Inject a compact "have we seen this
    # runway/burn/phase situation before?" block keyed on today's numbers so
    # the operator (and the Framework session that reads this briefing) sees
    # prior beliefs + prior decisions on structurally similar day-starts. The
    # briefing NEVER fails on the precedent read.
    try:
        phase = str((state.get("revenue") or {}).get("phase") or "").strip()
        precedent_ctx = (
            f"pre-shift briefing runway={runway.get('days_remaining', '?')}d "
            f"phase={phase} burn={codb.get('total_monthly_burn', 0):.0f}"
        )
        precedent_block = active_precedent_context(precedent_ctx)
        if precedent_block:
            lines.append("")
            lines.append(precedent_block)
    except Exception:  # noqa: BLE001 — precedent is advisory, briefing never crashes
        pass
    return "\n".join(lines)


# Autonomous behaviors whose arm-state a Framework session needs at day-start.
_FRAMEWORK_FLAGS = [
    ("idle-production drive (self-pacing)", "idle_production_drive_enabled", "SAMUS_IDLE_PRODUCTION_DRIVE_ENABLED"),
    ("governed autonomous dial", "governed_autonomous_dial_enabled", "SAMUS_GOVERNED_AUTONOMOUS_DIAL_ENABLED"),
    ("auto-stake sweep", "auto_stake_enabled", "SAMUS_AUTO_STAKE_ENABLED"),
    ("cognitive loop", "cognitive_loop_enabled", "SAMUS_COGNITIVE_LOOP_ENABLED"),
    ("CODB reasoner", "codb_reasoner_enabled", "SAMUS_CODB_REASONER_ENABLED"),
    ("buying-signal route", "outreach_buying_signal_route_enabled", "SAMUS_OUTREACH_BUYING_SIGNAL_ROUTE_ENABLED"),
    ("open-no-click nudge", "outreach_open_no_click_nudge_enabled", "SAMUS_OUTREACH_OPEN_NO_CLICK_NUDGE_ENABLED"),
]


def _flag_on(settings_attr: str, env_name: str) -> bool:
    try:
        v = getattr(get_settings(), settings_attr, None)
        if v is not None:
            return bool(v)
    except Exception:  # noqa: BLE001
        pass
    import os as _os
    return _os.environ.get(env_name, "").strip().lower() in ("1", "true", "yes", "on")


def compose_framework_orientation(state: dict) -> str:
    """Day-start orientation for the FRAMEWORK AGENT (a Claude session) — the same
    pre-shift moment, oriented for the auditor rather than the operator. Snapshots
    which autonomous behaviors are ARMED today and directs the session to review
    Samus's capability map before acting, so it monitors without a re-brief."""
    armed, dormant = [], []
    for label, attr, env in _FRAMEWORK_FLAGS:
        (armed if _flag_on(attr, env) else dormant).append(label)
    return "\n".join([
        "## FRAMEWORK AGENT ORIENTATION (Claude session primer)",
        "You are the Framework Agent — audit + augment Samus in live production; don't hand-execute its sales work.",
        "",
        "REVIEW SAMUS'S CAPABILITIES BEFORE ACTING (so you can monitor without a re-brief):",
        "  1. Load the capability map — `reference_samus_capability_map.md` (START HERE for Samus in MEMORY.md).",
        "  2. Run its fresh-session monitoring checklist: /health, /api/crm/stats, /admin/control-ticks, /snapshot, codex _drafts.",
        "",
        f"ARMED today ({len(armed)}): {', '.join(armed) or 'none'}",
        f"DORMANT today ({len(dormant)}): {', '.join(dormant) or 'none'}",
        "",
        "TODAY'S JOB: verify each ARMED autonomous behavior is producing correctly AND within governance + financial",
        "constraints (min(goal-urgency, affordability)); cross-check the briefing's risks against live ledgers before",
        "recommending changes. Treat the briefing + guidance below as Samus's view — bring an independent read.",
        "",
        "MINIMIZE INTERFERENCE: Samus executes AND self-optimizes (esp. the adaptive outbound campaign) — watch the",
        "TRENDS, not individual calls; intervene ONLY on systemic / governance / financial issues its own loop won't",
        "self-correct. Default to non-intervention; feed systemic findings into the EOD triangulation, not live meddling.",
        "(Full stance: the /samus-pre-shift skill's ADAPTIVE_EXECUTION.md.)",
    ])


def compose_end_of_day_review(state: dict, effectiveness: dict) -> str:
    """Samus composes the End-of-Day Operational Review (no LLM)."""
    runway = state.get("runway", {})
    rev = state.get("revenue", {})
    lines = [
        "# SAMUS END-OF-DAY OPERATIONAL REVIEW",
        f"Date: {state.get('date')} | Generated: {state.get('timestamp_utc')}",
        "",
        "## EXECUTIVE SUMMARY",
        (
            f"Runway {runway.get('days_remaining', '?')} days. "
            f"Target ${rev.get('target_usd', 0):,} in {rev.get('days_to_target', '?')} days."
        ),
        "",
        "## GUIDANCE EFFECTIVENESS",
        f"- Total recommendations tracked: {effectiveness.get('total', 0)}",
        f"- By status: {effectiveness.get('by_status', {})}",
        f"- By tier: {effectiveness.get('by_tier', {})}",
        f"- Open (actionable): {effectiveness.get('open_count', 0)}",
        f"- Completed w/ score: {effectiveness.get('completed_count', 0)}",
        f"- Mean success score: {effectiveness.get('mean_success_score')}",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Strategic-reasoning prompts (the OpenAI-consulting parts).
# ---------------------------------------------------------------------------
_JSON_SHAPE = (
    "Return ONLY a JSON object (no prose outside it):\n"
    '{ "recommendations": [ { "category": "daily_goal|weekly_goal|monthly_goal|'
    "revenue_acceleration|operational_optimization|autonomy_advancement|"
    "resource_efficiency|governance_improvement|capability_expansion|risk_reduction\", "
    '"recommendation": "one concrete action", "rationale": "why now", '
    '"action_steps": ["step 1","step 2"], "feasibility": "low|medium|high", '
    '"expected_impact": "low|medium|high", "risk_level": "low|medium|high", '
    '"suggested_owner": "prospecting|outreach|seo|strategy|finance|gateway|cognition|governance", '
    '"source_question": "1" } ] }'
)


def _advisor_system() -> str:
    """System prompt framing OpenAI as Samus's strategic advisor."""
    return (
        "You are a senior strategic advisor to Samus, an autonomous AI commerce "
        "agent running the revenue engine for Hustleforge. Samus sends its daily "
        "intelligence and asks for guidance. Answer with specific, concrete, "
        "actionable recommendations — exact actions and expected outcomes, not "
        "categories or frameworks. Samus ingests your guidance into a tracked "
        "ledger and executes within governance constraints. Samus can run "
        "prospecting, SEO audits, proposals, voice calls, email campaigns, CRM, "
        "and finance — all on GCP Cloud Run. Output STRICT JSON only.\n\n"
        f"Operating doctrine:\n{strategic_intelligence_cycle()}"
    )


def _briefing_questions(state: dict) -> str:
    rev = state.get("revenue", {})
    runway = state.get("runway", {})
    return (
        f"With {runway.get('days_remaining', '?')} days of runway and a "
        f"${rev.get('target_usd', 0):,} target in {rev.get('days_to_target', '?')} days, advise on:\n"
        "1. Single highest-leverage action today.\n"
        "2. Three measurable milestones this week.\n"
        "3. Fastest path to first dollar via prospecting/outreach/SEO.\n"
        "4. Criteria to gate cognitive-loop autonomous execution.\n"
        "5. Zero-cost ways to preserve knowledge-graph capability (AuraDB deferred).\n\n"
        + _JSON_SHAPE
    )


def _review_questions(effectiveness: dict) -> str:
    return (
        "Reflect on today's guidance effectiveness and recommend actions for "
        "tomorrow. Effectiveness so far:\n"
        f"{json.dumps(effectiveness)}\n\n"
        "Provide tomorrow's highest-leverage recommendations.\n\n"
        + _JSON_SHAPE
    )


# ---------------------------------------------------------------------------
# Default LLM caller (injectable for tests).
# ---------------------------------------------------------------------------
def _default_llm(system: str, prompt: str) -> str:
    """One budget-gated OpenAI completion via the shared client. May raise."""
    from backend.common.llm_client import anthropic_messages

    text, _usage = anthropic_messages(
        workcell="cognition",
        api_key="",
        prompt=prompt,
        system=system,
        max_tokens=1500,
        timeout=90.0,
        security_label="cognition",
    )
    return text


LlmFn = Callable[[str, str], str]


# ---------------------------------------------------------------------------
# The two autonomous report runs.
# ---------------------------------------------------------------------------
def run_pre_shift_briefing(
    *,
    llm: Optional[LlmFn] = None,
    ledger: Optional[GuidanceLedger] = None,
) -> dict:
    """Compose the briefing, consult OpenAI, ingest the guidance. Fail-safe.

    Returns a dict: ``{briefing_id, briefing, raw_guidance, ingested, summary,
    error?}``. NEVER raises — an LLM failure yields ``ingested=0`` with an error
    note so a scheduled job records a clean, partial outcome.
    """
    led = ledger if ledger is not None else GuidanceLedger()
    state = gather_production_state()
    briefing = compose_pre_shift_briefing(state)
    briefing_id = f"briefing-{state['date']}-{uuid.uuid4().hex[:8]}"

    call = llm if llm is not None else _default_llm
    out: dict[str, Any] = {
        "briefing_id": briefing_id,
        "briefing": briefing,
        "kind": "pre_shift_briefing",
    }
    try:
        raw = call(_advisor_system(), f"{briefing}\n\n---\n\n{_briefing_questions(state)}")
        records = ingest_guidance(briefing_id, raw, ledger=led)
        out["raw_guidance"] = raw
        out["ingested"] = len(records)
    except Exception as exc:  # noqa: BLE001 — scheduled job must not crash
        log.warning("pre_shift_briefing: LLM/ingest failed: %s", exc)
        out["ingested"] = 0
        out["error"] = f"{type(exc).__name__}: {exc}"
    out["summary"] = led.effectiveness_summary()

    # DUAL-AUDIENCE PRIMER — the briefing primes Samus AND a Framework (Claude)
    # session. Persist a markdown primer combining Samus's briefing, the day's
    # accepted strategic guidance, and the Framework Agent orientation (armed-
    # capability snapshot + capability-review directive). The /samus-pre-shift
    # skill reads this to start a session grounded.
    orientation = compose_framework_orientation(state)
    out["framework_orientation"] = orientation
    try:
        from backend.common import storage

        guidance_ctx = active_guidance_context(ledger=led) or "(no accepted guidance yet)"
        primer = (
            f"{briefing}\n\n## TODAY'S STRATEGIC GUIDANCE (accepted)\n{guidance_ctx}\n\n"
            f"{orientation}\n"
        )
        pdir = storage.root() / "cognition"
        pdir.mkdir(parents=True, exist_ok=True)
        primer_path = pdir / f"pre_shift_briefing_{state.get('date')}.md"
        primer_path.write_text(primer, encoding="utf-8")
        out["primer_path"] = str(primer_path)
    except Exception as exc:  # noqa: BLE001 — never fail the briefing on the primer
        log.warning("pre_shift primer persist failed: %s", exc)

    # Operator's daily attestation (ADR-018): running the pre-shift IS the
    # operator's acknowledgement of the day's autonomous execution, so stamp
    # the durable per-business-day attestation the governed-dial consent basis
    # reads. Best-effort — the briefing never fails on this.
    try:
        from backend.common.preshift_attestation import write_attestation

        out["attestation_path"] = write_attestation(
            briefing_id=out.get("briefing_id", ""),
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("pre_shift attestation stamp failed: %s", exc)
    return out


def run_end_of_day_review(
    *,
    llm: Optional[LlmFn] = None,
    ledger: Optional[GuidanceLedger] = None,
    consult_openai: bool = True,
    triangulation_llm: Optional[LlmFn] = None,
    claude_llm: Optional[LlmFn] = None,
) -> dict:
    """Score guidance effectiveness, compose the review, optionally plan tomorrow.

    The data-heavy review is composed LOCALLY (zero cost). When ``consult_openai``
    is True, ONE additional completion asks for tomorrow's guidance, which is
    ingested. Fail-safe; never raises.
    """
    led = ledger if ledger is not None else GuidanceLedger()
    state = gather_production_state()
    effectiveness = led.effectiveness_summary()
    review = compose_end_of_day_review(state, effectiveness)
    review_id = f"review-{state['date']}-{uuid.uuid4().hex[:8]}"

    out: dict[str, Any] = {
        "review_id": review_id,
        "review": review,
        "effectiveness": effectiveness,
        "kind": "end_of_day_review",
        "ingested": 0,
    }

    # CODB investment reasoner — wire-not-arm (SAMUS_CODB_REASONER_ENABLED,
    # default OFF). When armed, rank which cost-of-doing-business scale-up to
    # buy next given current cash/MRR + the binding bottleneck, surface the
    # top picks as guidance records, and include them in the review output.
    # RECOMMEND-ONLY: this never spends. The section is additive — its absence
    # (flag off, or a reasoner fault) leaves the review unchanged.
    try:
        if getattr(get_settings(), "codb_reasoner_enabled", False):
            from .codb_reasoner import build_eod_section

            out["codb_investment_recommendations"] = build_eod_section(
                bottleneck_hint="cold-email-reach", ledger=led,
            )
    except Exception as exc:  # noqa: BLE001 — EOD must never crash on the reasoner
        log.warning("end_of_day_review: codb reasoner failed: %s", exc)

    if consult_openai:
        call = llm if llm is not None else _default_llm
        try:
            raw = call(_advisor_system(), f"{review}\n\n---\n\n{_review_questions(effectiveness)}")
            records = ingest_guidance(review_id, raw, ledger=led)
            out["raw_guidance"] = raw
            out["ingested"] = len(records)
        except Exception as exc:  # noqa: BLE001
            log.warning("end_of_day_review: LLM/ingest failed: %s", exc)
            out["error"] = f"{type(exc).__name__}: {exc}"

    # THREE-WAY TRIANGULATION (operator design). Three independent reads of the
    # day converge into a next-day gameplan; findings ≥2 agree on are high-
    # confidence. A=Samus's review, B=OpenAI advisor, C=local adversarial auditor.
    # The auditor + synthesis run on the LOCAL model (zero external cost); OpenAI
    # is only the single advisor call above. Fully fail-soft — never breaks EOD.
    try:
        _run_triangulation(out, review, state, review_id, led,
                           auditor_llm=triangulation_llm, claude_llm=claude_llm)
    except Exception as exc:  # noqa: BLE001
        log.warning("end_of_day_review: triangulation failed: %s", exc)
        out["triangulation_error"] = f"{type(exc).__name__}: {exc}"
    return out


_RELIABILITY_REASONS = {
    "degraded": "leg C is the local auditor AND triangulation fell back to deterministic keyword overlap",
    "partial": "one leg is degraded (leg C is the local auditor OR triangulation is not the LLM path)",
    "full": "3 legs healthy + LLM synthesis",
}


def _compute_synthesis_reliability(out: dict) -> tuple[str, str]:
    """Reduce leg-C source + triangulation method + any budget signal to a
    single-word reliability level for the EOD output. Fail-safe: any exception
    or unknown state returns ``("partial", reason)`` — the conservative default.
    See the ``synthesis_reliability`` field on ``run_end_of_day_review``'s
    result dict for how downstream consumes this."""
    try:
        leg_c = str(out.get("leg_c_source") or "")
        tri = out.get("triangulation") or {}
        method = str(tri.get("method") or "")
        err_lc = str(out.get("error") or "").lower()
        budget_hit = "budget_exceeded" in err_lc or "budget exceeded" in err_lc

        leg_c_degraded = leg_c not in ("framework_claude", "claude_api")
        synth_degraded = method not in ("llm",)

        if budget_hit or (leg_c_degraded and synth_degraded):
            return "degraded", _RELIABILITY_REASONS["degraded"]
        if leg_c_degraded or synth_degraded:
            return "partial", _RELIABILITY_REASONS["partial"]
        return "full", _RELIABILITY_REASONS["full"]
    except Exception:  # noqa: BLE001 — reliability computation must never crash EOD
        return "partial", _RELIABILITY_REASONS["partial"]


def compose_eod_review_markdown(review, reports, tri, state, review_id,
                                 reliability: str = "partial",
                                 reliability_reason: str = "") -> str:
    """The full, readable EOD record: the three independent reports (A/B/C), the
    triangulation, and the next-day gameplan — one durable artifact on disk.

    ``reliability`` / ``reliability_reason`` surface the same
    ``synthesis_reliability`` field the caller attaches to the result dict, so a
    human reader can't miss when the review was assembled under degraded legs.
    """
    corr = tri.get("corroborated") or []
    div = tri.get("divergent") or []
    gp = tri.get("gameplan") or {}
    lines = [
        f"# SAMUS END-OF-DAY REVIEW — {state.get('date')}",
        f"{review_id} | {iso_now()} | convergence method: {tri.get('method')} "
        f"| synthesis reliability: {reliability}",
        "",
    ]
    if reliability in ("partial", "degraded"):
        lines += [
            f"> WARNING synthesis reliability: {reliability} — "
            f"{reliability_reason or _RELIABILITY_REASONS.get(reliability, '')}",
            "",
        ]
    lines += [
        "## Report A — Samus (local review)",
        review or "(none)",
        "",
        "## Report B — OpenAI strategic advisor",
        (reports.get("B_openai") or "").strip() or "(not consulted / unavailable)",
        "",
        ("## Report C — Claude (independent Framework review)" if "C_claude" in reports
         else "## Report C — Local adversarial auditor (fallback; Claude report absent)"),
        (reports.get("C_claude") or reports.get("C_auditor") or "").strip()
        or "(absent — two-way triangulation)",
        "",
        "## Triangulation",
        "### Corroborated — high-confidence (>=2 reports agree)",
    ]
    lines += [f"- [T{c.get('tier', '?')}] {c.get('finding', '')} "
              f"(sources: {','.join(c.get('sources', []))})" for c in corr] or ["- (none)"]
    if not corr:
        lines.append("- (none)")
    lines += ["", "### Divergent — single-source (flagged for review)"]
    lines += [f"- {d.get('finding', '')} (source: {d.get('source', '')})" for d in div]
    if not div:
        lines.append("- (none)")
    lines += ["", "## NEXT-DAY GAMEPLAN"]
    for tier in ("tier1", "tier2", "tier3"):
        items = gp.get(tier) or []
        if items:
            lines.append(f"### {tier.upper()}")
            lines += [f"- {i}" for i in items]
    lines += ["", "## Today's interactions (compressed signal)", "```json",
              json.dumps(state.get("interactions", {}), indent=2), "```"]
    return "\n".join(lines)


def _run_triangulation(out, review, state, review_id, led, *, auditor_llm=None, claude_llm=None) -> None:
    """Leg C (local auditor) + converge A/B/C → gameplan; persist + track it."""
    from .triangulation import (
        build_auditor_report,
        build_claude_report,
        claude_available,
        read_framework_report,
        triangulate,
    )

    reports = {
        "A_samus": review,
        "B_openai": str(out.get("raw_guidance") or ""),
    }
    day_summary = f"{review}\n\n## Today's interactions\n{json.dumps(state.get('interactions', {}))}"
    # Leg C resolution, in priority order:
    #   1. a Framework Agent report a convened Claude session wrote this day
    #      (on-demand — free + a richer read than one API call);
    #   2. Claude via the Anthropic API (when the account is funded);
    #   3. the local adversarial auditor (always-available fallback).
    fw = read_framework_report(state.get("date", ""))
    claude_api = ""
    if not fw and (claude_llm is not None or claude_available()):
        claude_api = build_claude_report(day_summary, llm_fn=claude_llm)
    if fw:
        reports["C_claude"], out["leg_c_source"] = fw, "framework_claude"
    elif claude_api:
        reports["C_claude"], out["leg_c_source"] = claude_api, "claude_api"
    else:
        reports["C_auditor"] = build_auditor_report(day_summary, llm_fn=auditor_llm)
        out["leg_c_source"] = "local_auditor"

    tri = triangulate(reports, llm_fn=auditor_llm)
    out["triangulation"] = tri
    out["gameplan"] = tri.get("gameplan", {})

    # G4 [P2] closed 2026-07-08 — compute a single-word reliability level for
    # the assembled EOD output and surface it on the returned dict, the
    # markdown header, and (when degraded) on the corroborated recs routed
    # into the guidance ledger. Downstream consumers can then triage a
    # face-value-safe finding differently from a synthesized-under-degraded-
    # legs finding. Fail-safe: `_compute_synthesis_reliability` never raises.
    reliability, reliability_reason = _compute_synthesis_reliability(out)
    out["synthesis_reliability"] = reliability
    out["synthesis_reliability_reason"] = reliability_reason

    # Persist the gameplan as a durable, observable artifact.
    try:
        from backend.common import storage
        gp_dir = storage.root() / "cognition"
        gp_dir.mkdir(parents=True, exist_ok=True)
        # Fallback uses the same Pacific business date as gather_production_state
        # so gameplan / eod_review filenames stay consistent with call_list /
        # attestation filenames even if state["date"] is somehow missing.
        _fallback_date = _business_today_iso()
        (gp_dir / f"gameplan_{state.get('date', _fallback_date)}.json").write_text(
            json.dumps({"review_id": review_id, "ts": iso_now(),
                        "gameplan": out["gameplan"], "triangulation": tri,
                        "synthesis_reliability": reliability,
                        "synthesis_reliability_reason": reliability_reason},
                       indent=2), encoding="utf-8")
        # The full narrative EOD record (3 reports + triangulation + gameplan),
        # host-visible alongside the gameplan JSON.
        review_md = gp_dir / f"eod_review_{state.get('date', _fallback_date)}.md"
        review_md.write_text(
            compose_eod_review_markdown(review, reports, tri, state, review_id,
                                         reliability=reliability,
                                         reliability_reason=reliability_reason),
            encoding="utf-8")
        out["eod_review_path"] = str(review_md)
    except Exception as exc:  # noqa: BLE001
        log.warning("gameplan/eod-review persist failed: %s", exc)

    # Route the CORROBORATED (high-confidence) findings into the tracked guidance
    # ledger so their effectiveness is scored on the next cycle — the same
    # lifecycle the OpenAI guidance flows through. When synthesis reliability is
    # DEGRADED, we prefix the rationale with a durable, greppable marker so an
    # operator triaging these recs applies appropriate skepticism (preferred
    # over silently downgrading the tier — that would hide the signal).
    corr = tri.get("corroborated") or []
    if corr:
        _degraded_tag = ("[synthesis_reliability: degraded] "
                         if reliability == "degraded" else "")
        recs = [{
            "category": "risk_reduction" if int(c.get("tier", 2)) == 1 else "operational_optimization",
            "recommendation": str(c.get("finding", ""))[:400],
            "rationale": f"{_degraded_tag}corroborated by {','.join(c.get('sources', []))} "
                         f"({tri.get('method')})",
            "action_steps": [], "feasibility": "medium",
            "expected_impact": "high" if int(c.get("tier", 2)) == 1 else "medium",
            "risk_level": "medium", "suggested_owner": "gateway", "source_question": "triangulation",
        } for c in corr if c.get("finding")]
        try:
            out["gameplan_ingested"] = len(
                ingest_guidance(f"{review_id}-gameplan",
                                json.dumps({"recommendations": recs}), ledger=led))
        except Exception as exc:  # noqa: BLE001
            log.warning("gameplan ingest failed: %s", exc)


# ---------------------------------------------------------------------------
# THE FEEDBACK SEAM — accepted guidance steers the cognitive loop's REASON.
# ---------------------------------------------------------------------------
# Short cache so a 60s cadence doesn't full-scan the guidance ledger every tick
# (a real Firestore read cost). Never use 0.0 as the "never loaded" sentinel
# (house rule for monotonic time) — start one TTL in the past so the first call
# always refreshes.
_CONTEXT_TTL_SEC = 120.0
_context_cache: dict[str, Any] = {"text": "", "at": time.monotonic() - _CONTEXT_TTL_SEC}


def active_guidance_context(
    *,
    ledger: Optional[GuidanceLedger] = None,
    max_items: int = 8,
    _now: Optional[float] = None,
) -> str:
    """Compact, priority-ordered summary of ACCEPTED / IN-PROGRESS guidance.

    This is the seam the cognitive runner injects into the reasoner's
    system_prefix so every autonomous REASON call is grounded in what the
    operator's advisor recommended and the operator accepted. Returns "" when
    there is no accepted guidance (the loop then reasons on the directive alone).

    Cached for ``_CONTEXT_TTL_SEC`` so the 60s cadence does not re-scan the
    ledger every tick. Fail-safe: any error yields "" (degrade to directive-only).
    """
    now = _now if _now is not None else time.monotonic()
    if ledger is None and (now - _context_cache["at"]) < _CONTEXT_TTL_SEC:
        return _context_cache["text"]

    led = ledger if ledger is not None else GuidanceLedger()
    try:
        recs = led.all_latest()
    except Exception as exc:  # noqa: BLE001 — never break a cognitive tick
        log.warning("active_guidance_context: ledger read failed: %s", exc)
        return _context_cache["text"] if ledger is None else ""

    actionable = [
        r for r in recs
        if r.status in (GuidanceStatus.ACCEPTED.value, GuidanceStatus.IN_PROGRESS.value)
    ]
    actionable.sort(key=lambda r: (r.tier, -len(r.action_plan)))
    actionable = actionable[:max_items]

    if not actionable:
        text = ""
    else:
        lines = ["## Active Strategic Guidance (operator-accepted, by priority)"]
        for r in actionable:
            lines.append(f"- [T{r.tier}/{r.owner}] {r.recommendation}")
        text = "\n".join(lines)

    if ledger is None:
        _context_cache["text"] = text
        _context_cache["at"] = now
    return text


# ---------------------------------------------------------------------------
# PRECEDENT SEAM — Concept 1 institutional memory.
# ---------------------------------------------------------------------------
# When the reasoning loop faces a new context, we consult TWO precedent
# sources before novel synthesis:
#
#   1. belief_ledger.query_precedent(context) — "have we believed something
#      relevant to this before?"
#   2. codex.registry.search_decisions(context) — "have we RESOLVED a
#      structurally similar decision before (ADR-###)?"
#
# active_precedent_context() composes both into a compact system_prefix
# fragment mirroring active_guidance_context(). consult_precedent() returns
# a structured decision the reasoning loop can use to short-circuit when
# precedent is strong (high-confidence active belief + recent).
#
# Both are fail-safe: any error yields "" / a proceed-novel record.
_PRECEDENT_SHORTCIRCUIT_MIN_SCORE = 4.0
_PRECEDENT_SHORTCIRCUIT_MIN_CONFIDENCE = 0.7


def active_precedent_context(
    context: str,
    *,
    k_beliefs: int = 3,
    k_decisions: int = 3,
) -> str:
    """Compact precedent block for injection into the REASON system_prefix.

    Returns the concatenation of the top-``k_beliefs`` belief precedents
    (from :func:`backend.cognitive.belief_ledger.query_precedent`) and the
    top-``k_decisions`` past decisions (from
    :func:`backend.common.codex.registry.search_decisions`). Empty context or
    zero results yields "" so the loop reasons on directive+guidance alone.

    Fail-safe: import or lookup failures degrade to whatever partial content
    is available; nothing raises.
    """
    if not context or not context.strip():
        return ""

    belief_lines: list[str] = []
    try:
        from backend.cognitive.belief_ledger import query_precedent

        for m in query_precedent(context, k=k_beliefs):
            conf_pct = int(round(m.confidence * 100))
            belief_lines.append(
                f"- [{conf_pct}% conf, {m.matched_on}] {m.claim}"
                + (f" (situation={m.situation_key})" if m.situation_key else "")
            )
    except Exception as exc:  # noqa: BLE001
        log.warning("active_precedent_context: belief lookup failed: %s", exc)

    decision_lines: list[str] = []
    try:
        from backend.common.codex.registry import search_decisions

        for d in search_decisions(context, k=k_decisions):
            adr_tag = d.adr_id or "(unnumbered)"
            date_tag = f", {d.date_iso}" if d.date_iso else ""
            decision_lines.append(
                f"- [{adr_tag}{date_tag}, {d.source}] {d.title}"
            )
    except Exception as exc:  # noqa: BLE001
        log.warning("active_precedent_context: decision search failed: %s", exc)

    if not belief_lines and not decision_lines:
        return ""

    parts: list[str] = ["## Precedent (recall before synthesizing anew)"]
    if belief_lines:
        parts.append("### Prior beliefs on this situation")
        parts.extend(belief_lines)
    if decision_lines:
        parts.append("### Prior decisions on structurally similar situations")
        parts.extend(decision_lines)
    return "\n".join(parts)


def consult_precedent(context: str) -> dict[str, Any]:
    """Structured precedent lookup — the reasoning loop's early hook.

    Returns a dict with:
      ``mode``: ``"short_circuit"`` when precedent is strong enough that the
        loop should reuse the past resolution rather than synthesize anew;
        ``"proceed_novel"`` otherwise.
      ``beliefs``: top belief precedents (list[PrecedentMatch]).
      ``decisions``: top decision precedents (list[DecisionMatch]).
      ``leading_belief``: the belief whose score decided short-circuit (or None).
      ``rationale``: one-line human-readable reason for the mode.

    Short-circuit triggers only when at least one belief precedent has:
      - status active (query_precedent already filters contradicted),
      - score >= _PRECEDENT_SHORTCIRCUIT_MIN_SCORE (composite score after
        recency + confidence decay),
      - confidence >= _PRECEDENT_SHORTCIRCUIT_MIN_CONFIDENCE.

    That combination signals "we already believe the answer here, high
    confidence, recently verified" — the case where re-reasoning is waste.
    A merely-relevant decision doesn't short-circuit; decisions are precedent
    for reasoning, not answers themselves.

    Fail-safe: any error yields ``mode="proceed_novel"`` — the reasoner then
    proceeds as if precedent were empty.
    """
    empty: dict[str, Any] = {
        "mode": "proceed_novel",
        "beliefs": [],
        "decisions": [],
        "leading_belief": None,
        "rationale": "no context",
    }
    if not context or not context.strip():
        return empty

    beliefs: list[Any] = []
    decisions: list[Any] = []
    try:
        from backend.cognitive.belief_ledger import query_precedent

        beliefs = list(query_precedent(context, k=5))
    except Exception as exc:  # noqa: BLE001
        log.warning("consult_precedent: belief lookup failed: %s", exc)

    try:
        from backend.common.codex.registry import search_decisions

        decisions = list(search_decisions(context, k=5))
    except Exception as exc:  # noqa: BLE001
        log.warning("consult_precedent: decision search failed: %s", exc)

    leading: Any = None
    for b in beliefs:
        if (
            b.score >= _PRECEDENT_SHORTCIRCUIT_MIN_SCORE
            and b.confidence >= _PRECEDENT_SHORTCIRCUIT_MIN_CONFIDENCE
        ):
            leading = b
            break

    if leading is not None:
        return {
            "mode": "short_circuit",
            "beliefs": beliefs,
            "decisions": decisions,
            "leading_belief": leading,
            "rationale": (
                f"precedent belief {leading.belief_id!r} "
                f"score={leading.score:.2f} confidence={leading.confidence:.2f} "
                f"matched_on={leading.matched_on}"
            ),
        }
    return {
        "mode": "proceed_novel",
        "beliefs": beliefs,
        "decisions": decisions,
        "leading_belief": None,
        "rationale": (
            f"no belief precedent above threshold "
            f"(min_score={_PRECEDENT_SHORTCIRCUIT_MIN_SCORE}, "
            f"min_confidence={_PRECEDENT_SHORTCIRCUIT_MIN_CONFIDENCE})"
        ),
    }


__all__ = [
    "gather_production_state",
    "compose_pre_shift_briefing",
    "compose_end_of_day_review",
    "compose_eod_review_markdown",
    "compose_framework_orientation",
    "run_pre_shift_briefing",
    "run_end_of_day_review",
    "active_guidance_context",
    "active_precedent_context",
    "consult_precedent",
]
