"""Tests for the hierarchical (industry -> policy family) bandit.

Covers composite-key updates, UCB1 exploration-then-exploitation in
``select_best_policy``, ``get_policy_bandit_stats`` isolation, ``reset_bandit``
clearing hierarchical arms, and backward-compatibility of the flat bandit.
Bandit state is reset between tests for isolation.
"""

from __future__ import annotations

import pytest

import backend.strategy.portfolio_manager as pm
from backend.strategy.policy_compiler import POLICY_FAMILIES


def _reset():
    """Reset module-level bandit state for test isolation."""
    pm.reset_bandit()


# ---------------------------------------------------------------------------
# Composite-key updates
# ---------------------------------------------------------------------------


def test_update_policy_bandit_composes_key():
    """update_policy_bandit stores an ``industry::policy_family`` composite arm."""
    _reset()
    pm.update_policy_bandit("hvac", "fast_quote_mode", 1.0)
    stats = pm.get_bandit_stats()
    assert "hvac::fast_quote_mode" in stats
    assert stats["hvac::fast_quote_mode"]["trials"] == 1
    assert stats["hvac::fast_quote_mode"]["wins"] == pytest.approx(1.0)
    _reset()


def test_update_policy_bandit_accumulates():
    """Repeated updates accumulate wins and trials on the composite arm."""
    _reset()
    pm.update_policy_bandit("dentist", "reputation_repair", 1.0)
    pm.update_policy_bandit("dentist", "reputation_repair", 0.5)
    stats = pm.get_bandit_stats()
    arm = stats["dentist::reputation_repair"]
    assert arm["trials"] == 2
    assert arm["wins"] == pytest.approx(1.5)
    _reset()


def test_update_policy_bandit_reward_signal_flows_through():
    """A RewardSignal passed to update_policy_bandit is density-weighted."""
    _reset()
    from backend.strategy.reward_density import RewardSignal, compute_reward_density

    sig = RewardSignal(
        outcome=1.0,
        owner_email=True,
        social_facebook=True,
        social_instagram=False,
        seo_score=0.2,
        contactability=0.8,
        infrastructure_health=0.7,
    )
    pm.update_policy_bandit("plumber", "emergency_dispatch", 0.0, reward_signal=sig)
    stats = pm.get_bandit_stats()
    assert stats["plumber::emergency_dispatch"]["wins"] == pytest.approx(
        compute_reward_density(sig)
    )
    _reset()


# ---------------------------------------------------------------------------
# select_best_policy — explore each family once, then exploit
# ---------------------------------------------------------------------------


def test_select_best_policy_explores_each_family_once():
    """select_best_policy returns each unseen family once (UCB1 inf) before repeating."""
    _reset()
    families = POLICY_FAMILIES["hvac"]
    seen: set[str] = set()
    for _ in range(len(families)):
        choice = pm.select_best_policy("hvac")
        assert choice in families
        assert choice not in seen, "an already-explored family was re-selected too early"
        seen.add(choice)
        # Record a neutral trial so this arm is no longer unseen.
        pm.update_policy_bandit("hvac", choice, 0.0)
    assert seen == set(families)
    _reset()


def test_select_best_policy_exploits_after_exploration():
    """Once every family is explored, the highest-reward family is chosen."""
    _reset()
    families = POLICY_FAMILIES["dentist"]
    winner = families[1]
    for fam in families:
        reward = 5.0 if fam == winner else 0.0
        pm.update_policy_bandit("dentist", fam, reward)
    # All families now seen; UCB1 should exploit the clear winner.
    assert pm.select_best_policy("dentist") == winner
    _reset()


def test_select_best_policy_unknown_industry_uses_generic():
    """An unknown industry selects from the generic family set, never raises."""
    _reset()
    from backend.strategy.policy_compiler import GENERIC_VERTICAL_POLICY

    choice = pm.select_best_policy("mystery_vertical")
    assert choice in GENERIC_VERTICAL_POLICY.policy_families
    _reset()


def test_select_best_policy_cold_start_yields_default_family():
    """A cold-start bandit (zero trials for the industry) returns a valid
    policy family — families[0] via UCB1's explore-everything-once rule — and
    never raises. This is the prospecting "decide" step's cold-start guarantee.
    """
    _reset()
    families = POLICY_FAMILIES["hvac"]
    # No update_policy_bandit calls -> every arm has trials==0 -> _ucb1_score
    # returns inf for all -> the first family wins the >best_score comparison.
    choice = pm.select_best_policy("hvac")
    assert choice == families[0]
    assert choice in families
    _reset()


# ---------------------------------------------------------------------------
# get_policy_bandit_stats — isolation
# ---------------------------------------------------------------------------


def test_get_policy_bandit_stats_isolates_industry():
    """get_policy_bandit_stats returns only that industry's composite arms."""
    _reset()
    pm.update_policy_bandit("hvac", "fast_quote_mode", 1.0)
    pm.update_policy_bandit("dentist", "new_patient_offer", 1.0)
    pm.update_bandit("flat_arm", 1.0)  # flat arm — must be excluded

    hvac_stats = pm.get_policy_bandit_stats("hvac")
    assert set(hvac_stats) == {"hvac::fast_quote_mode"}
    assert "dentist::new_patient_offer" not in hvac_stats
    assert "flat_arm" not in hvac_stats
    _reset()


def test_get_policy_bandit_stats_empty_for_unused_industry():
    """An industry with no recorded arms yields an empty stats dict."""
    _reset()
    pm.update_policy_bandit("hvac", "fast_quote_mode", 1.0)
    assert pm.get_policy_bandit_stats("real_estate") == {}
    _reset()


# ---------------------------------------------------------------------------
# reset_bandit clears hierarchical arms
# ---------------------------------------------------------------------------


def test_reset_bandit_clears_hierarchical_arms():
    """reset_bandit clears composite arms — they live in the same _BANDIT_STATS."""
    _reset()
    pm.update_policy_bandit("hvac", "fast_quote_mode", 1.0)
    pm.update_bandit("flat_arm", 1.0)
    assert pm.get_bandit_stats()  # populated

    pm.reset_bandit()
    assert pm.get_bandit_stats() == {}
    assert pm.get_policy_bandit_stats("hvac") == {}


# ---------------------------------------------------------------------------
# Backward-compat — flat bandit unaffected
# ---------------------------------------------------------------------------


def test_flat_update_bandit_still_works():
    """The flat update_bandit path is byte-identical — unaffected by hierarchy."""
    _reset()
    pm.update_bandit("accelerate", 1.0)
    pm.update_bandit("accelerate", 0.0)
    stats = pm.get_bandit_stats()
    assert stats["accelerate"]["trials"] == 2
    assert stats["accelerate"]["wins"] == pytest.approx(1.0)
    _reset()


def test_flat_and_hierarchical_arms_coexist():
    """Flat arms and composite arms share _BANDIT_STATS without collision."""
    _reset()
    pm.update_bandit("accelerate", 1.0)
    pm.update_policy_bandit("hvac", "fast_quote_mode", 1.0)
    stats = pm.get_bandit_stats()
    assert "accelerate" in stats
    assert "hvac::fast_quote_mode" in stats
    assert len(stats) == 2
    _reset()


def test_policy_families_alias_matches_compiler():
    """portfolio_manager.POLICY_FAMILIES aliases the compiler's source of truth."""
    assert pm.POLICY_FAMILIES == POLICY_FAMILIES
