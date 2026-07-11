"""Tests for `backend.strategy.capability_marketplace`.

Covers the registry surface (publish / withdraw / list_providers /
all_capabilities), the weighted `compose_score` formula, and the
`select_best` constraint-filtering + tie-breaking rules.
"""

from __future__ import annotations

import pytest

from backend.strategy.capability_marketplace import (
    CapabilityListing,
    CapabilityMarketplace,
    SelectionWeights,
    compose_score,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _listing(
    capability_id: str = "trend_forecasting",
    provider_agent: str = "research_agent_7",
    cost: int = 10,
    performance_score: float = 0.91,
    latency_ms: int = 800,
    tags: tuple[str, ...] = (),
) -> CapabilityListing:
    return CapabilityListing(
        capability_id=capability_id,
        provider_agent=provider_agent,
        cost=cost,
        performance_score=performance_score,
        latency_ms=latency_ms,
        tags=tags,
    )


# ---------------------------------------------------------------------------
# Registry surface
# ---------------------------------------------------------------------------
def test_publish_then_list_providers_returns_listing() -> None:
    mp = CapabilityMarketplace()
    listing = _listing()
    mp.publish(listing)

    out = mp.list_providers("trend_forecasting")
    assert out == [listing]


def test_publish_same_pair_twice_replaces_first() -> None:
    mp = CapabilityMarketplace()
    first = _listing(cost=10, performance_score=0.5)
    second = _listing(cost=20, performance_score=0.9)
    mp.publish(first)
    mp.publish(second)

    out = mp.list_providers("trend_forecasting")
    assert len(out) == 1
    assert out[0].cost == 20
    assert out[0].performance_score == 0.9


def test_different_providers_for_same_capability_coexist() -> None:
    mp = CapabilityMarketplace()
    a = _listing(provider_agent="research_agent_7")
    b = _listing(provider_agent="research_agent_99")
    mp.publish(a)
    mp.publish(b)

    out = mp.list_providers("trend_forecasting")
    assert len(out) == 2
    assert {l.provider_agent for l in out} == {"research_agent_7", "research_agent_99"}


def test_withdraw_existing_returns_true_second_call_returns_false() -> None:
    mp = CapabilityMarketplace()
    mp.publish(_listing())
    assert mp.withdraw("trend_forecasting", "research_agent_7") is True
    assert mp.withdraw("trend_forecasting", "research_agent_7") is False
    assert mp.list_providers("trend_forecasting") == []


def test_all_capabilities_returns_sorted_distinct_ids() -> None:
    mp = CapabilityMarketplace()
    mp.publish(_listing(capability_id="trend_forecasting", provider_agent="p1"))
    mp.publish(_listing(capability_id="anomaly_detection", provider_agent="p1"))
    mp.publish(_listing(capability_id="trend_forecasting", provider_agent="p2"))
    mp.publish(_listing(capability_id="report_generation", provider_agent="p3"))

    assert mp.all_capabilities() == [
        "anomaly_detection",
        "report_generation",
        "trend_forecasting",
    ]


# ---------------------------------------------------------------------------
# select_best — happy paths + filters
# ---------------------------------------------------------------------------
def test_select_best_single_candidate_returns_that_candidate() -> None:
    mp = CapabilityMarketplace()
    only = _listing()
    mp.publish(only)
    assert mp.select_best("trend_forecasting") == only


def test_select_best_two_candidates_picks_higher_performance_default_weights() -> None:
    mp = CapabilityMarketplace()
    weak = _listing(provider_agent="p_weak", performance_score=0.4)
    strong = _listing(provider_agent="p_strong", performance_score=0.95)
    mp.publish(weak)
    mp.publish(strong)

    chosen = mp.select_best("trend_forecasting")
    # Same cost + latency + tags → performance dominates → strong wins.
    assert chosen == strong


def test_select_best_honors_max_cost_filter() -> None:
    mp = CapabilityMarketplace()
    cheap_low_perf = _listing(provider_agent="p_cheap", cost=5, performance_score=0.3)
    pricey_top_perf = _listing(provider_agent="p_pricey", cost=100, performance_score=0.99)
    mp.publish(cheap_low_perf)
    mp.publish(pricey_top_perf)

    chosen = mp.select_best("trend_forecasting", max_cost=20)
    # Pricey listing must be filtered out even though its score is higher.
    assert chosen == cheap_low_perf


def test_select_best_honors_max_latency_filter() -> None:
    mp = CapabilityMarketplace()
    fast_low_perf = _listing(provider_agent="p_fast", latency_ms=100, performance_score=0.4)
    slow_top_perf = _listing(provider_agent="p_slow", latency_ms=5000, performance_score=0.99)
    mp.publish(fast_low_perf)
    mp.publish(slow_top_perf)

    chosen = mp.select_best("trend_forecasting", max_latency_ms=500)
    assert chosen == fast_low_perf


def test_select_best_no_matches_returns_none() -> None:
    mp = CapabilityMarketplace()
    mp.publish(_listing(cost=1000))
    # Nothing offers this capability_id at all.
    assert mp.select_best("nonexistent_capability") is None
    # And nothing survives the max_cost filter.
    assert mp.select_best("trend_forecasting", max_cost=10) is None


def test_select_best_required_tags_is_score_term_not_filter() -> None:
    """A candidate missing required tags is still considered — it just
    loses the tag_fit slice of the score."""
    mp = CapabilityMarketplace()
    with_tags = _listing(
        provider_agent="p_tagged",
        performance_score=0.5,
        tags=("realtime", "vetted"),
    )
    without_tags = _listing(
        provider_agent="p_untagged",
        performance_score=0.5,
        tags=(),
    )
    mp.publish(with_tags)
    mp.publish(without_tags)

    chosen = mp.select_best("trend_forecasting", required_tags=("realtime", "vetted"))
    # Both candidates are considered; tagged listing wins on tag_fit.
    assert chosen == with_tags

    # And if the only candidate has no tags, it is still selectable —
    # required_tags does not exclude it.
    mp2 = CapabilityMarketplace()
    mp2.publish(without_tags)
    assert mp2.select_best("trend_forecasting", required_tags=("realtime",)) == without_tags


# ---------------------------------------------------------------------------
# compose_score — edge cases
# ---------------------------------------------------------------------------
def test_compose_score_cost_norm_zero_collapses_cost_term_to_full_weight() -> None:
    weights = SelectionWeights()
    listing = _listing(cost=0, performance_score=0.0, latency_ms=0)
    score = compose_score(
        listing,
        required_tags=(),
        weights=weights,
        cost_norm=0,
        latency_norm=0,
    )
    # performance=0 contributes 0
    # cost_norm=0  → cost_term = 1.0  → adds weights.cost (0.25)
    # latency_norm=0 → latency_term = 1.0 → adds weights.latency (0.15)
    # required_tags=() → tag_term = 1.0 → adds weights.tag_fit (0.10)
    expected = weights.cost + weights.latency + weights.tag_fit
    assert score == pytest.approx(expected)


def test_compose_score_required_tags_empty_yields_full_tag_fit_weight() -> None:
    weights = SelectionWeights()
    listing = _listing(performance_score=0.0, cost=0, latency_ms=0)
    score = compose_score(
        listing,
        required_tags=(),
        weights=weights,
        cost_norm=10,
        latency_norm=1000,
    )
    # Tag-fit term should be exactly weights.tag_fit when no tags required.
    # (Other inverted terms are also 1.0 since cost/latency are 0.)
    assert score == pytest.approx(weights.cost + weights.latency + weights.tag_fit)


def test_compose_score_returns_value_in_unit_interval() -> None:
    weights = SelectionWeights()
    listing = _listing(performance_score=0.7, cost=5, latency_ms=300, tags=("a",))
    score = compose_score(
        listing,
        required_tags=("a", "b"),
        weights=weights,
        cost_norm=10,
        latency_norm=1000,
    )
    assert 0.0 <= score <= 1.0


def test_compose_score_clamps_oversize_performance_into_unit_interval() -> None:
    """Defensive: a misbehaving caller passing performance_score=1.5
    must not push the composite above 1.0."""
    weights = SelectionWeights()
    listing = _listing(performance_score=1.5, cost=0, latency_ms=0)
    score = compose_score(
        listing,
        required_tags=(),
        weights=weights,
        cost_norm=0,
        latency_norm=0,
    )
    assert score <= 1.0


# ---------------------------------------------------------------------------
# SelectionWeights validation
# ---------------------------------------------------------------------------
def test_selection_weights_under_one_raises() -> None:
    with pytest.raises(ValueError):
        SelectionWeights(performance=0.5, cost=0.25, latency=0.15, tag_fit=0.09)


def test_selection_weights_over_one_raises() -> None:
    with pytest.raises(ValueError):
        SelectionWeights(performance=0.5, cost=0.25, latency=0.15, tag_fit=0.11)


def test_selection_weights_defaults_sum_to_one() -> None:
    # Sanity: the defaults themselves must be valid.
    w = SelectionWeights()
    assert w.performance + w.cost + w.latency + w.tag_fit == pytest.approx(1.0)
