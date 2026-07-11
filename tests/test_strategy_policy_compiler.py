"""Tests for backend.strategy.policy_compiler — the Closer Policy Compiler.

Covers tier boundaries at 0.85 / 0.65, every vertical's base policy, the
unknown-vertical generic fallback, derived-field logic, and determinism
(same input -> byte-identical profile). Pure-logic — no LLM, no monkeypatch.
"""
from __future__ import annotations

import pytest

from backend.strategy.policy_compiler import (
    GENERIC_VERTICAL_POLICY,
    POLICY_FAMILIES,
    REWARD_DENSITY_HIGH_THRESHOLD,
    REWARD_DENSITY_MEDIUM_THRESHOLD,
    VERTICAL_POLICIES,
    CloserExecutionProfile,
    build_execution_profile,
)


# ---------------------------------------------------------------------------
# Tier boundaries — reward_density at 0.85 / 0.65 (strict ``>``)
# ---------------------------------------------------------------------------

def test_tier_high_above_085():
    """reward_density strictly above 0.85 -> high / 6h / full_audit."""
    p = build_execution_profile(0.5, 0.90, 0.2, 0.5, "hvac")
    assert p.outreach_intensity == "high"
    assert p.followup_interval_hours == 6
    assert p.proposal_depth == "full_audit"


def test_tier_boundary_at_085_is_medium():
    """Exactly 0.85 is NOT high (strict >) -> medium tier."""
    assert REWARD_DENSITY_HIGH_THRESHOLD == 0.85
    p = build_execution_profile(0.5, 0.85, 0.2, 0.5, "hvac")
    assert p.outreach_intensity == "medium"
    assert p.followup_interval_hours == 24
    assert p.proposal_depth == "standard"


def test_tier_medium_above_065():
    """reward_density above 0.65 but not above 0.85 -> medium / 24h / standard."""
    p = build_execution_profile(0.5, 0.70, 0.2, 0.5, "dentist")
    assert p.outreach_intensity == "medium"
    assert p.followup_interval_hours == 24
    assert p.proposal_depth == "standard"


def test_tier_boundary_at_065_is_low():
    """Exactly 0.65 is NOT medium (strict >) -> low tier."""
    assert REWARD_DENSITY_MEDIUM_THRESHOLD == 0.65
    p = build_execution_profile(0.5, 0.65, 0.2, 0.5, "dentist")
    assert p.outreach_intensity == "low"
    assert p.followup_interval_hours == 72
    assert p.proposal_depth == "template"


def test_tier_low_below_065():
    """reward_density at or below 0.65 -> low / 72h / template."""
    p = build_execution_profile(0.5, 0.10, 0.2, 0.5, "plumber")
    assert p.outreach_intensity == "low"
    assert p.followup_interval_hours == 72
    assert p.proposal_depth == "template"


# ---------------------------------------------------------------------------
# Per-vertical base policies
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("vertical", ["hvac", "dentist", "plumber", "real_estate"])
def test_each_vertical_uses_its_base_policy(vertical):
    """Each known vertical pulls channel_priority + template_family from VERTICAL_POLICIES."""
    base = VERTICAL_POLICIES[vertical]
    p = build_execution_profile(0.6, 0.9, 0.2, 0.5, vertical)
    assert p.vertical == vertical
    assert p.channel_priority == list(base.channel_priority)
    assert p.template_family == base.template_family


def test_hvac_is_phone_first():
    """HVAC is emergency-driven -> phone-first channel priority."""
    p = build_execution_profile(0.6, 0.9, 0.2, 0.5, "hvac")
    assert p.channel_priority[0] == "phone"
    assert p.template_family == "hvac_emergency"


def test_dentist_is_email_first():
    """Dental is appointment-driven -> email-first channel priority."""
    p = build_execution_profile(0.6, 0.9, 0.2, 0.5, "dentist")
    assert p.channel_priority[0] == "email"
    assert p.template_family == "dental_appointment"


# ---------------------------------------------------------------------------
# Unknown-vertical generic fallback — never raises
# ---------------------------------------------------------------------------

def test_unknown_vertical_uses_generic_fallback():
    """An unknown vertical falls back to GENERIC_VERTICAL_POLICY, never raises."""
    p = build_execution_profile(0.6, 0.9, 0.2, 0.5, "spaceship_repair")
    assert p.vertical == "spaceship_repair"
    assert p.channel_priority == list(GENERIC_VERTICAL_POLICY.channel_priority)
    assert p.template_family == GENERIC_VERTICAL_POLICY.template_family


def test_empty_vertical_does_not_raise():
    """An empty-string vertical is handled gracefully via the generic fallback."""
    p = build_execution_profile(0.6, 0.9, 0.2, 0.5, "")
    assert p.template_family == GENERIC_VERTICAL_POLICY.template_family


# ---------------------------------------------------------------------------
# Derived fields — confidence / escalation / retry / allocation / token budget
# ---------------------------------------------------------------------------

def test_confidence_blends_forecast_and_enrichment():
    """confidence_score = 0.6*forecast + 0.4*enrichment, clamped to [0,1]."""
    p = build_execution_profile(1.0, 0.5, 0.2, 0.5, "hvac")
    # 0.6*1.0 + 0.4*0.5 = 0.8
    assert p.confidence_score == pytest.approx(0.8)


def test_retry_policy_conservative_on_high_regret():
    """High regret-per-token -> conservative retry posture."""
    p = build_execution_profile(0.5, 0.9, 0.9, 0.5, "hvac")
    assert p.retry_policy == "conservative"


def test_retry_policy_aggressive_on_low_regret():
    """Low regret-per-token -> aggressive retry posture."""
    p = build_execution_profile(0.5, 0.9, 0.0, 0.5, "hvac")
    assert p.retry_policy == "aggressive"


def test_retry_policy_standard_in_mid_band():
    """Mid-band regret-per-token -> standard retry posture."""
    p = build_execution_profile(0.5, 0.9, 0.3, 0.5, "hvac")
    assert p.retry_policy == "standard"


def test_escalation_threshold_rises_with_regret():
    """A higher regret-per-token raises the escalation bar."""
    low = build_execution_profile(0.5, 0.9, 0.0, 0.5, "hvac")
    high = build_execution_profile(0.5, 0.9, 1.0, 0.5, "hvac")
    assert high.escalation_threshold > low.escalation_threshold
    assert 0.0 <= low.escalation_threshold <= 1.0
    assert 0.0 <= high.escalation_threshold <= 1.0


def test_allocation_weight_in_unit_range():
    """allocation_weight stays clamped within [0,1]."""
    p = build_execution_profile(5.0, 5.0, 0.2, 0.5, "hvac")
    assert 0.0 <= p.allocation_weight <= 1.0


def test_token_budget_is_small_and_bounded():
    """max_token_budget_usd is a small per-vertical ceiling well under $1."""
    p = build_execution_profile(1.0, 1.0, 0.2, 1.0, "hvac")
    assert 0.0 < p.max_token_budget_usd < 1.0


# ---------------------------------------------------------------------------
# Determinism — same input -> identical profile
# ---------------------------------------------------------------------------

def test_determinism_same_input_identical_profile():
    """Same inputs always yield a byte-identical CloserExecutionProfile."""
    args = (0.73, 0.88, 0.34, 0.61, "plumber")
    a = build_execution_profile(*args)
    b = build_execution_profile(*args)
    assert a == b
    assert isinstance(a, CloserExecutionProfile)


def test_determinism_across_all_verticals():
    """Determinism holds for every vertical and the generic fallback."""
    for vertical in ["hvac", "dentist", "plumber", "real_estate", "unknown_v"]:
        a = build_execution_profile(0.6, 0.7, 0.2, 0.5, vertical)
        b = build_execution_profile(0.6, 0.7, 0.2, 0.5, vertical)
        assert a == b


# ---------------------------------------------------------------------------
# POLICY_FAMILIES — derived from VERTICAL_POLICIES (single source of truth)
# ---------------------------------------------------------------------------

def test_policy_families_match_vertical_policies():
    """POLICY_FAMILIES is derived from VERTICAL_POLICIES — no duplication."""
    for vertical, base in VERTICAL_POLICIES.items():
        assert POLICY_FAMILIES[vertical] == base.policy_families


def test_hvac_policy_families_match_spec():
    """HVAC families match the spec example exactly."""
    assert POLICY_FAMILIES["hvac"] == (
        "aggressive_local",
        "seo_gap_heavy",
        "fast_quote_mode",
        "emergency_service_pitch",
    )
