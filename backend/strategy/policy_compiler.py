"""Closer Policy Compiler — deterministic execution-policy surface.

A live ``autonomous_closer`` would add instability (call-state orchestration,
autonomous negotiation, escalation liability, token churn). Instead the
strategy layer emits a fully-deterministic *execution policy* and a future
closer — and CRM / outreach / voice today — consume that policy.

The expensive, intelligent part is portfolio policy *generation*; closer
*execution* is cheap and stateless. Compile the policy once, deterministically;
execute it many times.

Pure-logic — deterministic, no I/O, no network, ZERO LLM calls. Same inputs
always produce a byte-identical :class:`CloserExecutionProfile`.

Public surface
--------------
:func:`build_execution_profile` is the documented public interface that CRM,
outreach and voice consume. :func:`backend.strategy.predictive_allocator.
closer_mode_for` stays as an internal tier helper only.

Family ownership
----------------
This module owns the per-vertical *policy family* names via
:data:`VERTICAL_POLICIES` (each base policy's ``policy_families`` field) and
re-exports them as :data:`POLICY_FAMILIES`. The hierarchical bandit in
:mod:`backend.strategy.portfolio_manager` imports that map rather than
duplicating the family strings — one source of truth.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "CloserExecutionProfile",
    "VerticalBasePolicy",
    "build_execution_profile",
    "VERTICAL_POLICIES",
    "POLICY_FAMILIES",
    "GENERIC_VERTICAL_POLICY",
    "REWARD_DENSITY_HIGH_THRESHOLD",
    "REWARD_DENSITY_MEDIUM_THRESHOLD",
    "FOLLOWUP_HOURS_HIGH",
    "FOLLOWUP_HOURS_MEDIUM",
    "FOLLOWUP_HOURS_LOW",
    "CONFIDENCE_FORECAST_WEIGHT",
    "CONFIDENCE_ENRICHMENT_WEIGHT",
    "REGRET_CONSERVATIVE_THRESHOLD",
    "REGRET_AGGRESSIVE_THRESHOLD",
    "ESCALATION_BASE",
    "ESCALATION_REGRET_GAIN",
    "ALLOCATION_FORECAST_WEIGHT",
    "ALLOCATION_DENSITY_WEIGHT",
    "MAX_TOKEN_BUDGET_CEILING_USD",
    "MAX_TOKEN_BUDGET_FLOOR_USD",
]

# --- tier thresholds (reward_density) — no magic numbers in the logic -------
REWARD_DENSITY_HIGH_THRESHOLD: float = 0.85
REWARD_DENSITY_MEDIUM_THRESHOLD: float = 0.65

# --- followup cadence per tier (hours) -------------------------------------
FOLLOWUP_HOURS_HIGH: int = 6
FOLLOWUP_HOURS_MEDIUM: int = 24
FOLLOWUP_HOURS_LOW: int = 72

# --- confidence_score blend (forecast_score + enrichment_confidence) -------
CONFIDENCE_FORECAST_WEIGHT: float = 0.6
CONFIDENCE_ENRICHMENT_WEIGHT: float = 0.4

# --- regret_per_token bands → retry_policy / escalation_threshold ----------
# High regret-per-token ⇒ the bandit is spending poorly ⇒ be more conservative
# (raise the escalation bar, dial retries back).
REGRET_CONSERVATIVE_THRESHOLD: float = 0.5
REGRET_AGGRESSIVE_THRESHOLD: float = 0.1
# escalation_threshold = base + gain*clamped(regret_per_token), clamped 0..1.
ESCALATION_BASE: float = 0.5
ESCALATION_REGRET_GAIN: float = 0.4

# --- allocation_weight blend (forecast_score + reward_density) -------------
ALLOCATION_FORECAST_WEIGHT: float = 0.5
ALLOCATION_DENSITY_WEIGHT: float = 0.5

# --- max_token_budget_usd — small per-vertical ceiling. The agent runs a
# $1/day global cap, so a single vertical's LLM budget stays tiny.
MAX_TOKEN_BUDGET_CEILING_USD: float = 0.25
MAX_TOKEN_BUDGET_FLOOR_USD: float = 0.02


def _clamp01(value: float) -> float:
    """Clamp a value into [0.0, 1.0]; defensive against out-of-range inputs."""
    if value < 0.0:
        return 0.0
    if value > 1.0:
        return 1.0
    return value


# ---------------------------------------------------------------------------
# Vertical base policies — vertical-specific defaults the tier logic adapts.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VerticalBasePolicy:
    """Vertical-specific defaults consumed by :func:`build_execution_profile`.

    The tier logic (driven by ``reward_density``) layers cadence / intensity /
    proposal depth ON TOP of these vertical-specific fields.

    ``base_cadence_hours``    — vertical's natural followup rhythm assumption.
    ``objection_density``     — [0,1] how objection-heavy the vertical runs;
                                higher ⇒ a more conservative retry posture.
    ``policy_families``       — the bandit's policy-family arms for this
                                vertical (the single source of truth the
                                hierarchical bandit references).
    """

    channel_priority: tuple[str, ...]
    template_family: str
    base_cadence_hours: int
    objection_density: float
    policy_families: tuple[str, ...]


# Each vertical: a distinct go-to-market shape.
#   HVAC      — emergency-driven, phone-first, fast quote.
#   dentist   — appointment-driven, email-first, slower nurture.
#   plumber   — urgent-repair-driven, phone/SMS-first, very fast.
#   real_estate — relationship-driven, multi-touch, email/phone balanced.
VERTICAL_POLICIES: dict[str, VerticalBasePolicy] = {
    "hvac": VerticalBasePolicy(
        channel_priority=("phone", "sms", "email"),
        template_family="hvac_emergency",
        base_cadence_hours=12,
        objection_density=0.35,
        policy_families=(
            "aggressive_local",
            "seo_gap_heavy",
            "fast_quote_mode",
            "emergency_service_pitch",
        ),
    ),
    "dentist": VerticalBasePolicy(
        channel_priority=("email", "phone", "sms"),
        template_family="dental_appointment",
        base_cadence_hours=48,
        objection_density=0.50,
        policy_families=(
            "appointment_nurture",
            "reputation_repair",
            "new_patient_offer",
            "insurance_gap_pitch",
        ),
    ),
    "plumber": VerticalBasePolicy(
        channel_priority=("phone", "sms", "email"),
        template_family="plumbing_urgent",
        base_cadence_hours=8,
        objection_density=0.30,
        policy_families=(
            "emergency_dispatch",
            "fast_quote_mode",
            "service_area_blitz",
            "maintenance_plan_pitch",
        ),
    ),
    "real_estate": VerticalBasePolicy(
        channel_priority=("phone", "email", "sms"),
        template_family="realestate_relationship",
        base_cadence_hours=36,
        objection_density=0.45,
        policy_families=(
            "listing_velocity",
            "seo_gap_heavy",
            "referral_network_pitch",
            "market_report_nurture",
        ),
    ),
}

# Safe generic fallback for an unknown vertical — never raise on lookup.
GENERIC_VERTICAL_POLICY: VerticalBasePolicy = VerticalBasePolicy(
    channel_priority=("phone", "email", "sms"),
    template_family="generic_outreach",
    base_cadence_hours=24,
    objection_density=0.40,
    policy_families=(
        "standard_outreach",
        "seo_gap_heavy",
        "value_offer_pitch",
        "nurture_sequence",
    ),
)

# Re-export of the per-vertical family names — the bandit references THIS map
# (see module docstring "Family ownership"). One source of truth, no copies.
POLICY_FAMILIES: dict[str, tuple[str, ...]] = {
    vertical: policy.policy_families for vertical, policy in VERTICAL_POLICIES.items()
}


@dataclass
class CloserExecutionProfile:
    """Deterministic execution policy compiled for one vertical.

    Consumed by CRM / outreach / voice (and a future closer). Carries no
    live state — it is a *policy surface*, compiled once and executed many
    times. Every field is derived deterministically from the inputs.
    """

    vertical: str
    allocation_weight: float  # 0..1 — portfolio share this vertical earns
    outreach_intensity: str  # "high" | "medium" | "low"
    followup_interval_hours: int
    escalation_threshold: float  # 0..1
    max_token_budget_usd: float  # per-vertical LLM ceiling — kept small
    proposal_depth: str  # "full_audit" | "standard" | "template"
    channel_priority: list[str]  # ordered channel preference
    template_family: str
    retry_policy: str  # "aggressive" | "standard" | "conservative"
    confidence_score: float  # 0..1


def _tier_fields(reward_density: float) -> tuple[str, int, str]:
    """Map reward_density to (outreach_intensity, followup_hours, proposal_depth).

    Tier boundaries are strict ``>`` so the threshold value itself falls into
    the lower tier (0.85 → medium, 0.65 → low).
    """
    if reward_density > REWARD_DENSITY_HIGH_THRESHOLD:
        return "high", FOLLOWUP_HOURS_HIGH, "full_audit"
    if reward_density > REWARD_DENSITY_MEDIUM_THRESHOLD:
        return "medium", FOLLOWUP_HOURS_MEDIUM, "standard"
    return "low", FOLLOWUP_HOURS_LOW, "template"


def _retry_policy_for(regret_per_token: float) -> str:
    """Derive retry posture from regret-per-token.

    High regret-per-token ⇒ the bandit is exploring poorly ⇒ pull retries
    back to ``conservative``; low regret-per-token ⇒ retries are cheap and
    productive ⇒ ``aggressive``.
    """
    if regret_per_token > REGRET_CONSERVATIVE_THRESHOLD:
        return "conservative"
    if regret_per_token < REGRET_AGGRESSIVE_THRESHOLD:
        return "aggressive"
    return "standard"


def build_execution_profile(
    forecast_score: float,
    reward_density: float,
    regret_per_token: float,
    enrichment_confidence: float,
    vertical: str,
) -> CloserExecutionProfile:
    """Compile a deterministic :class:`CloserExecutionProfile` for ``vertical``.

    PUBLIC interface — the policy surface CRM / outreach / voice consume.

    Tier-driven fields (``outreach_intensity`` / ``followup_interval_hours`` /
    ``proposal_depth``) come from ``reward_density`` via :func:`_tier_fields`.
    Vertical-specific fields (``channel_priority`` / ``template_family``) come
    from :data:`VERTICAL_POLICIES`; an unknown vertical falls back to
    :data:`GENERIC_VERTICAL_POLICY` — this never raises.

    Derived fields:
      - ``confidence_score``     — weighted blend of ``forecast_score`` and
        ``enrichment_confidence`` (both clamped to [0,1]).
      - ``escalation_threshold`` — rises with ``regret_per_token`` (a poorly
        spending bandit gets a higher escalation bar).
      - ``retry_policy``         — band of ``regret_per_token``.
      - ``allocation_weight``    — weighted blend of ``forecast_score`` and
        ``reward_density``, clamped to [0,1].
      - ``max_token_budget_usd`` — small ceiling scaled by confidence,
        between the floor and ceiling constants.

    Same inputs always yield a byte-identical profile. ZERO LLM calls.
    """
    base = VERTICAL_POLICIES.get(vertical, GENERIC_VERTICAL_POLICY)

    outreach_intensity, followup_interval_hours, proposal_depth = _tier_fields(reward_density)

    confidence_score = _clamp01(
        _clamp01(forecast_score) * CONFIDENCE_FORECAST_WEIGHT
        + _clamp01(enrichment_confidence) * CONFIDENCE_ENRICHMENT_WEIGHT
    )

    # High regret-per-token raises the escalation bar (don't escalate a
    # vertical the bandit is already wasting budget on).
    escalation_threshold = _clamp01(
        ESCALATION_BASE + ESCALATION_REGRET_GAIN * _clamp01(regret_per_token)
    )
    retry_policy = _retry_policy_for(regret_per_token)

    allocation_weight = _clamp01(
        _clamp01(forecast_score) * ALLOCATION_FORECAST_WEIGHT
        + _clamp01(reward_density) * ALLOCATION_DENSITY_WEIGHT
    )

    # Small per-vertical LLM ceiling — scaled by confidence between the
    # floor and ceiling. The agent's $1/day global cap dominates regardless.
    token_span = MAX_TOKEN_BUDGET_CEILING_USD - MAX_TOKEN_BUDGET_FLOOR_USD
    max_token_budget_usd = round(MAX_TOKEN_BUDGET_FLOOR_USD + token_span * confidence_score, 4)

    return CloserExecutionProfile(
        vertical=vertical,
        allocation_weight=round(allocation_weight, 6),
        outreach_intensity=outreach_intensity,
        followup_interval_hours=followup_interval_hours,
        escalation_threshold=round(escalation_threshold, 6),
        max_token_budget_usd=max_token_budget_usd,
        proposal_depth=proposal_depth,
        channel_priority=list(base.channel_priority),
        template_family=base.template_family,
        retry_policy=retry_policy,
        confidence_score=round(confidence_score, 6),
    )
