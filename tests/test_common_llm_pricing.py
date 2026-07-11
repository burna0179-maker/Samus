"""llm_pricing — pricing table + cost calculator.

Control A reference data (token-cost-hardening 2026-05-18 brief).
Pure math, no I/O — these tests are the fastest in the suite.
"""

from __future__ import annotations

import pytest

from backend.common.llm_pricing import (
    PRICING_TABLE,
    UnknownModelPricing,
    compute_cost_usd,
    resolve_pricing,
)


# ---------------------------------------------------------------------------
# resolve_pricing — first-match-wins regex over PRICING_TABLE
# ---------------------------------------------------------------------------


def test_resolve_pricing_matches_haiku():
    p = resolve_pricing("claude-haiku-4-5-20251001")
    assert p.input_per_mtok == pytest.approx(0.80)
    assert p.output_per_mtok == pytest.approx(4.00)
    assert p.cache_write_per_mtok == pytest.approx(1.00)
    assert p.cache_read_per_mtok == pytest.approx(0.08)


def test_resolve_pricing_matches_sonnet():
    p = resolve_pricing("claude-sonnet-4-20250514")
    assert p.input_per_mtok == pytest.approx(3.00)
    assert p.output_per_mtok == pytest.approx(15.00)


def test_resolve_pricing_matches_opus():
    p = resolve_pricing("claude-opus-4-20250514")
    assert p.input_per_mtok == pytest.approx(15.00)
    assert p.output_per_mtok == pytest.approx(75.00)


def test_resolve_pricing_unknown_model_raises():
    with pytest.raises(UnknownModelPricing):
        resolve_pricing("gpt-4-turbo")


def test_resolve_pricing_empty_model_raises():
    with pytest.raises(UnknownModelPricing):
        resolve_pricing("")


def test_resolve_pricing_partial_haiku_prefix_does_not_match():
    """Regex is anchored at ^, so 'haiku' alone shouldn't match the Haiku row."""
    with pytest.raises(UnknownModelPricing):
        resolve_pricing("haiku-4")


# ---------------------------------------------------------------------------
# compute_cost_usd — Decimal-precise dollar math
# ---------------------------------------------------------------------------


def test_compute_cost_haiku_input_only():
    # 1,000,000 input tokens at $0.80/MTok = $0.80
    cost = compute_cost_usd(
        "claude-haiku-4-5",
        input_tokens=1_000_000,
        output_tokens=0,
    )
    assert cost == pytest.approx(0.80)


def test_compute_cost_haiku_input_and_output():
    # 100k input ($0.08) + 50k output ($0.20) = $0.28
    cost = compute_cost_usd(
        "claude-haiku-4-5",
        input_tokens=100_000,
        output_tokens=50_000,
    )
    assert cost == pytest.approx(0.28)


def test_compute_cost_opus_is_much_higher_than_haiku():
    """Sanity: Opus output is 75/4 = 18.75x Haiku output, input is 15/0.8 = 18.75x."""
    haiku = compute_cost_usd(
        "claude-haiku-4-5",
        input_tokens=10_000,
        output_tokens=10_000,
    )
    opus = compute_cost_usd(
        "claude-opus-4",
        input_tokens=10_000,
        output_tokens=10_000,
    )
    # input contribution = (15/0.8)x = 18.75x; output = (75/4)x = 18.75x
    assert opus == pytest.approx(haiku * 18.75)


def test_compute_cost_with_cache_read_is_cheaper():
    """100k cache_read should cost ~10% of 100k normal input on Haiku."""
    no_cache = compute_cost_usd(
        "claude-haiku-4-5",
        input_tokens=100_000,
        output_tokens=0,
    )
    all_cache = compute_cost_usd(
        "claude-haiku-4-5",
        input_tokens=0,
        output_tokens=0,
        cache_read_tokens=100_000,
    )
    # cache_read is $0.08/MTok = 10% of input's $0.80/MTok
    assert all_cache == pytest.approx(no_cache * 0.10)


def test_compute_cost_with_cache_write_is_more_expensive_than_input():
    """Cache write costs ~125% of regular input on the same token count."""
    normal = compute_cost_usd(
        "claude-haiku-4-5",
        input_tokens=100_000,
        output_tokens=0,
    )
    cache_write = compute_cost_usd(
        "claude-haiku-4-5",
        input_tokens=0,
        output_tokens=0,
        cache_write_tokens=100_000,
    )
    # Haiku cache_write $1.00/MTok vs input $0.80/MTok = 1.25x
    assert cache_write == pytest.approx(normal * 1.25)


def test_compute_cost_negative_tokens_clamped_to_zero():
    """Defensive: negative tokens shouldn't subtract from the bill."""
    cost = compute_cost_usd(
        "claude-haiku-4-5",
        input_tokens=-1000,
        output_tokens=-500,
    )
    assert cost == 0.0


def test_compute_cost_zero_tokens_is_zero():
    cost = compute_cost_usd(
        "claude-haiku-4-5",
        input_tokens=0,
        output_tokens=0,
    )
    assert cost == 0.0


def test_compute_cost_4_decimal_precision():
    """The brief specifies 4-decimal precision (1/100 of a cent)."""
    # 1 input token on Haiku at $0.80/MTok = $0.0000008 -> rounded to $0.0000
    cost = compute_cost_usd(
        "claude-haiku-4-5",
        input_tokens=1,
        output_tokens=0,
    )
    # Decimal quantize HALF_UP: 0.0000008 -> 0.0000
    assert cost == 0.0
    # 1250 input tokens = $0.001 exactly -> 0.0010
    cost = compute_cost_usd(
        "claude-haiku-4-5",
        input_tokens=1250,
        output_tokens=0,
    )
    assert cost == pytest.approx(0.001)


def test_compute_cost_unknown_model_raises():
    with pytest.raises(UnknownModelPricing):
        compute_cost_usd("gpt-5", input_tokens=100, output_tokens=100)


def test_compute_cost_combined_input_output_cache_all_summed():
    """All four token kinds add up linearly with no cross-term."""
    # Haiku: 1M input = $0.80, 1M output = $4.00, 1M write = $1.00, 1M read = $0.08
    cost = compute_cost_usd(
        "claude-haiku-4-5",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        cache_write_tokens=1_000_000,
        cache_read_tokens=1_000_000,
    )
    assert cost == pytest.approx(0.80 + 4.00 + 1.00 + 0.08)


def test_pricing_table_ordering_haiku_before_sonnet_before_opus():
    """First-match-wins regex requires the table to be in order of specificity.

    All three patterns start with ^claude-{family}- so order between them
    doesn't actually matter for current entries, but if we ever add a
    catchall ^claude- entry it MUST go last. This test pins the ordering
    so a future reorder doesn't silently shadow specific entries.
    """
    families = [p.model_pattern for p in PRICING_TABLE]
    assert families.index("^claude-haiku-4") < families.index("^claude-sonnet-4")
    assert families.index("^claude-sonnet-4") < families.index("^claude-opus-4")
