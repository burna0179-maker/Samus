"""Deal scoring helpers — Phase 3.

Ports the tier classifier + probability math from
``recovery/deal_scoring_agent.py`` (which used a 0.0-1.0 float score) onto
the 0-100 ``intent_score`` integer scale that the intake form + lead
summarizer already produce. Same four tiers, same monotonic thresholds.

Pure functions only — no I/O, no DDB. Used by :mod:`backend.crm.service`
``create_opportunity`` to auto-fill tier / close_probability / deal_size
from a freshly converted lead's signals.

Scoring v2 (HOTL Tranche 2 — Economic Brain) adds, still pure at the core:

* **urgency** — time-in-stage exponential decay multiplier (per-stage
  half-life, default ~14d) applied to close_probability;
* **full cost** — LLM token cost + voice (Vapi) cost + per-send email cost;
* **confidence** — sample-size credibility (0-1) from bandit trial counts;
* **priority** — ``(EV × probability × urgency) / (time_estimate × max(cost,
  floor))`` packaged as :class:`PriorityScore` via :func:`priority_score`.

The convenience readers (:func:`estimate_voice_cost_usd`,
:func:`bandit_trials_for`) do guarded I/O and degrade to zero — the math
itself stays injectable/pure for hand-computed test fixtures.
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from typing import Any, Literal, Mapping


Tier = Literal["low", "warm", "hot", "priority"]


# ---------------------------------------------------------------------------
# Tier classification (intent_score 0-100 -> tier label)
# ---------------------------------------------------------------------------
# Thresholds mirror the recovery file's float bands, rescaled x100 and split
# into four operator-facing buckets (recovery had: cold/nurture/warm/hot —
# we collapse cold+nurture into "low" and lift the top band to "priority"
# so the operator queue UI gets a clean Top-N highlight).
#
#   <40 .......... low      (deprioritize; nurture cadence)
#   40-69 ........ warm     (operator should follow up this week)
#   70-89 ........ hot      (priority queue — same-day follow-up)
#   >=90 ......... priority (interrupt; book a call now)

def classify_tier(intent_score: int) -> Tier:
    """Map a 0-100 intent_score to one of four operator-facing tiers."""
    s = int(intent_score or 0)
    if s >= 90:
        return "priority"
    if s >= 70:
        return "hot"
    if s >= 40:
        return "warm"
    return "low"


def tier_close_probability(tier: Tier) -> float:
    """Tier -> baseline close probability (0.0 - 1.0).

    These are deliberately conservative; the pipeline FSM's
    STAGE_PROBABILITIES take over once a deal advances past ``new``.
    """
    return {
        "low":      0.05,
        "warm":     0.15,
        "hot":      0.35,
        "priority": 0.55,
    }[tier]


# ---------------------------------------------------------------------------
# Deal size estimation (intake budget enum -> expected annual USD)
# ---------------------------------------------------------------------------
# Midpoint of the band x 12 months. The lower-bound bands ("under_$150")
# use $100 as the implicit floor; the open-top "$5000+" band uses $7500 as
# a conservative anchor (we'd rather under-promise pipeline value).

_BUDGET_MIDPOINTS_USD: dict[str, float] = {
    "":           0.0,
    "under_$150": 100.0,
    "$150-$500":  325.0,
    "$500-$2000": 1250.0,
    "$2000-$5000": 3500.0,
    "$5000+":     7500.0,
}


_UNKNOWN_BUDGET_FLOOR_USD: float = 1200.0


def estimate_deal_size_usd(monthly_budget: str, service_interest: list[str]) -> float:
    """Project annual deal size from the intake form's monthly_budget enum.

    ``service_interest`` is currently informational only — used to bump
    the estimate by 15% for multi-service interest (proxy for stickier,
    higher-ticket engagements). Single-service or empty lists return the
    raw midpoint x 12.
    """
    midpoint = _BUDGET_MIDPOINTS_USD.get(monthly_budget or "", 0.0)
    annual = midpoint * 12.0
    services = [s for s in (service_interest or []) if s and s != "not_sure"]
    if len(services) >= 2:
        annual *= 1.15
    return round(annual, 2)


def effective_deal_size_usd(deal_size_usd: float) -> float:
    """Return a non-zero EV for ranking when budget is unknown.

    The stored 0.0 sentinel means "budget not provided" and is preserved
    in ``Opportunity.deal_size_usd`` for proposal generation (which
    converts 0.0 → None → "budget unknown" in the intake payload).
    For ranking, scoring, and ROI calculations a zero EV poisons
    every downstream formula.  This function substitutes the floor of the
    lowest real budget band ($100/mo × 12 = $1,200/yr) so unknown-budget
    opportunities still participate in priority ordering.
    """
    if deal_size_usd:
        return deal_size_usd
    return _UNKNOWN_BUDGET_FLOOR_USD


# ---------------------------------------------------------------------------
# Composite scorer (used by service.create_opportunity)
# ---------------------------------------------------------------------------

def score_opportunity_from_lead(lead_summary: dict) -> tuple[str, float, float]:
    """Return ``(tier, close_probability, deal_size_usd)`` from a lead dict.

    Accepts the same shape persisted to ``samus_onboarding_leads`` plus an
    optional ``intent_score`` (0-100) that downstream summarization fills
    in. Missing keys are treated as zero / empty — never raises.
    """
    intent_score = int(lead_summary.get("intent_score") or 0)
    monthly_budget = str(lead_summary.get("monthly_budget") or "")
    services_raw = lead_summary.get("service_interest") or []
    services = list(services_raw) if isinstance(services_raw, list) else []

    tier = classify_tier(intent_score)
    close_probability = tier_close_probability(tier)
    deal_size = estimate_deal_size_usd(monthly_budget, services)
    return tier, close_probability, deal_size


# ---------------------------------------------------------------------------
# Scoring v2 — urgency / full cost / confidence / priority
# ---------------------------------------------------------------------------

# Per-stage urgency half-life in days: after this many days sitting in the
# stage, the urgency multiplier has decayed to 0.5. Later stages decay faster
# (a stalled negotiation goes cold quicker than a stalled fresh lead).
STAGE_URGENCY_HALF_LIFE_DAYS: dict[str, float] = {
    "new":                 14.0,
    "qualified":           14.0,
    "proposal":            10.0,
    "negotiation":         7.0,
    "closed_won":          14.0,   # terminal — urgency is moot but defined
    "closed_won_retainer": 14.0,
    "closed_lost":         14.0,
}
DEFAULT_URGENCY_HALF_LIFE_DAYS: float = 14.0

# Urgency never decays to literal zero — an ancient deal still ranks, just
# far below fresh work.
URGENCY_FLOOR: float = 0.05

# Per-send marginal email cost (SendGrid + compose overhead), USD. A named
# constant so ROI roll-ups and bids price sends identically.
EMAIL_SEND_COST_USD: float = 0.002

# Cost floor for the priority denominator — a zero-cost action must not
# produce an infinite priority.
PRIORITY_COST_FLOOR_USD: float = 0.01

# Rough operator/agent time per stage-appropriate next action, hours.
STAGE_TIME_ESTIMATE_HRS: dict[str, float] = {
    "new":                 0.5,
    "qualified":           1.0,
    "proposal":            3.0,
    "negotiation":         2.0,
    "closed_won":          1.0,
    "closed_won_retainer": 1.0,
    "closed_lost":         0.5,
}
DEFAULT_TIME_ESTIMATE_HRS: float = 1.0

# Credibility prior: confidence = trials / (trials + K). K trials -> 0.5.
CONFIDENCE_PRIOR_TRIALS: float = 5.0


def urgency_multiplier(
    days_in_stage: float,
    stage: str = "",
    *,
    half_life_days: float | None = None,
) -> float:
    """Exponential time-in-stage decay: ``2 ** (-days / half_life)``.

    ``half_life_days`` overrides the per-stage default. Clamped to
    [URGENCY_FLOOR, 1.0]; negative ages are treated as zero. Pure.
    """
    hl = half_life_days if half_life_days is not None else \
        STAGE_URGENCY_HALF_LIFE_DAYS.get(stage, DEFAULT_URGENCY_HALF_LIFE_DAYS)
    if hl <= 0:
        return 1.0
    days = max(0.0, float(days_in_stage or 0.0))
    return max(URGENCY_FLOOR, min(1.0, 2.0 ** (-days / hl)))


def confidence_from_trials(
    trials: int, *, prior: float = CONFIDENCE_PRIOR_TRIALS,
) -> float:
    """Sample-size credibility in [0, 1): ``trials / (trials + prior)``.

    0 trials -> 0.0; ``prior`` trials -> 0.5; asymptotically 1.0. Pure.
    """
    t = max(0, int(trials or 0))
    return t / (t + float(prior))


def full_cost_usd(
    opportunity: Mapping[str, Any],
    *,
    voice_cost_usd: float = 0.0,
    email_sends: int = 0,
) -> float:
    """Aggregate cost-to-date: LLM tokens + voice + per-send email. Pure.

    ``voice_cost_usd`` is injected (see :func:`estimate_voice_cost_usd` for
    the convenience reader) so tests can hand-compute exactly.
    """
    token = float(opportunity.get("token_cost_usd") or 0.0)
    email = max(0, int(email_sends or 0)) * EMAIL_SEND_COST_USD
    return round(token + max(0.0, float(voice_cost_usd or 0.0)) + email, 6)


# -- guarded convenience readers (I/O; degrade to zero) ----------------------

def estimate_voice_cost_usd(attempt_count: int) -> float:
    """Estimate voice spend for a prospect: avg Vapi cost/call x attempts.

    Reads the voice cost tracker's 30d ledger aggregate; any failure — or no
    voice data at all — degrades to 0.0 (never raises).
    """
    attempts = max(0, int(attempt_count or 0))
    if attempts == 0:
        return 0.0
    try:
        from backend.voice.voice_cost_tracker import aggregate_vapi_costs
        avg = float(aggregate_vapi_costs(days=30).avg_cost_per_call or 0.0)
    except Exception:  # noqa: BLE001 — voice data is optional input
        return 0.0
    return round(avg * attempts, 6)


def bandit_trials_for(industry: str, policy_family: str = "") -> int:
    """Total bandit trials backing this industry (or one specific arm).

    Reads the durable strategy bandit through
    ``portfolio_manager.get_policy_bandit_stats``. With ``policy_family`` the
    count is that composite arm's trials; without it, the sum across the
    industry's arms. Missing data / store failure degrades to 0.
    """
    if not industry:
        return 0
    try:
        from backend.strategy.portfolio_manager import (
            HIERARCHICAL_ARM_SEP,
            get_policy_bandit_stats,
        )
        stats = get_policy_bandit_stats(industry)
    except Exception:  # noqa: BLE001 — bandit is optional input
        return 0
    if policy_family:
        key = f"{industry}{HIERARCHICAL_ARM_SEP}{policy_family}"
        entry = stats.get(key)
        return int(entry["trials"]) if entry else 0
    return sum(int(row.get("trials", 0)) for row in stats.values())


# -- priority ----------------------------------------------------------------

@dataclass
class PriorityScore:
    """Decomposed priority for one opportunity (all inputs preserved).

    ``priority = (ev_usd * probability * urgency)
                 / (time_estimate_hrs * max(cost_usd, floor))``
    """

    ev_usd: float
    probability: float
    urgency: float
    time_estimate_hrs: float
    cost_usd: float
    confidence: float
    priority: float

    def to_dict(self) -> dict[str, float]:
        return {
            "ev_usd": self.ev_usd,
            "probability": self.probability,
            "urgency": self.urgency,
            "time_estimate_hrs": self.time_estimate_hrs,
            "cost_usd": self.cost_usd,
            "confidence": self.confidence,
            "priority": self.priority,
        }


def compute_priority(
    *,
    ev_usd: float,
    probability: float,
    urgency: float,
    time_estimate_hrs: float,
    cost_usd: float,
    cost_floor_usd: float = PRIORITY_COST_FLOOR_USD,
) -> float:
    """The bare priority formula. Pure; shared by scoring + the arbiter."""
    hours = max(0.1, float(time_estimate_hrs or 0.0))
    cost = max(float(cost_floor_usd), float(cost_usd or 0.0))
    numerator = max(0.0, float(ev_usd or 0.0)) \
        * max(0.0, min(1.0, float(probability or 0.0))) \
        * max(0.0, min(1.0, float(urgency or 0.0)))
    return round(numerator / (hours * cost), 6)


def _parse_iso_ts(ts: str) -> _dt.datetime | None:
    raw = (ts or "").strip()
    if not raw:
        return None
    try:
        parsed = _dt.datetime.fromisoformat(
            raw[:-1] + "+00:00" if raw.endswith("Z") else raw,
        )
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=_dt.timezone.utc)


def priority_score(
    opportunity: Mapping[str, Any],
    *,
    now: _dt.datetime | None = None,
    half_life_days: float | None = None,
    time_estimate_hrs: float | None = None,
    voice_cost_usd: float | None = None,
    email_sends: int = 0,
    bandit_trials: int | None = None,
) -> PriorityScore:
    """Score one opportunity dict into a :class:`PriorityScore`.

    Accepts the persisted ``samus_opportunities`` row shape (or an
    ``Opportunity.model_dump()``). Injectable knobs keep the math pure for
    tests; when ``voice_cost_usd`` / ``bandit_trials`` are None the guarded
    readers fill them in (degrading to 0 when data sources are absent).

    Time-in-stage uses ``stage_entered_at`` when present (future field),
    falling back to ``updated_at`` then ``created_at`` — the CRM writes
    ``updated_at`` on every stage advance, so it is the honest available
    proxy for stage entry.
    """
    stage = str(opportunity.get("stage") or "new")
    ev = effective_deal_size_usd(float(opportunity.get("deal_size_usd") or 0.0))
    probability = float(opportunity.get("close_probability") or 0.0)

    now_dt = now or _dt.datetime.now(_dt.timezone.utc)
    entered = (
        _parse_iso_ts(str(opportunity.get("stage_entered_at") or ""))
        or _parse_iso_ts(str(opportunity.get("updated_at") or ""))
        or _parse_iso_ts(str(opportunity.get("created_at") or ""))
    )
    days_in_stage = 0.0
    if entered is not None:
        days_in_stage = max(0.0, (now_dt - entered).total_seconds() / 86400.0)
    urgency = urgency_multiplier(days_in_stage, stage, half_life_days=half_life_days)

    if voice_cost_usd is None:
        voice_cost_usd = estimate_voice_cost_usd(
            int(opportunity.get("voice_attempt_count") or 0),
        )
    cost = full_cost_usd(
        opportunity, voice_cost_usd=voice_cost_usd, email_sends=email_sends,
    )

    if bandit_trials is None:
        bandit_trials = bandit_trials_for(
            str(opportunity.get("industry") or ""),
            str(opportunity.get("policy_family") or ""),
        )
    confidence = confidence_from_trials(bandit_trials)

    hours = time_estimate_hrs if time_estimate_hrs is not None else \
        STAGE_TIME_ESTIMATE_HRS.get(stage, DEFAULT_TIME_ESTIMATE_HRS)

    priority = compute_priority(
        ev_usd=ev,
        probability=probability,
        urgency=urgency,
        time_estimate_hrs=hours,
        cost_usd=cost,
    )
    return PriorityScore(
        ev_usd=round(ev, 2),
        probability=round(probability, 4),
        urgency=round(urgency, 6),
        time_estimate_hrs=float(hours),
        cost_usd=cost,
        confidence=round(confidence, 6),
        priority=priority,
    )
