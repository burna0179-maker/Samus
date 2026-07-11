"""Tests for backend.prospecting.intelligence.

Covers: analyze_business, score_opportunity, map_products, determine_pitch_angle.
All functions are pure; no I/O or mocking required.
"""

from __future__ import annotations

import pytest

from backend.prospecting.intelligence import (
    analyze_business,
    determine_pitch_angle,
    map_products,
    score_opportunity,
)


# ---------------------------------------------------------------------------
# analyze_business
# ---------------------------------------------------------------------------


def test_analyze_business_full_input():
    """All keys present — signals match expected values."""
    data = {
        "website_url": "https://example.com",
        "website_features": ["cta", "booking"],
        "platform": "wordpress",
        "tech_stack": ["cloudflare", "react"],
        "review_count": 42,
        "rating": 4.7,
        "ads_detected": True,
        "competitor_count": 8,
    }
    signals = analyze_business(data)

    assert signals["has_website"] is True
    assert signals["has_cta"] is True
    assert signals["has_booking"] is True
    assert signals["platform"] == "wordpress"
    assert signals["tech_stack"] == ["cloudflare", "react"]
    assert signals["review_count"] == 42
    assert signals["rating"] == pytest.approx(4.7)
    assert signals["ads_detected"] is True
    assert signals["competitor_count"] == 8


def test_analyze_business_tolerates_missing_keys():
    """Empty dict — all signals fall back to safe defaults."""
    signals = analyze_business({})

    assert signals["has_website"] is False
    assert signals["has_cta"] is False
    assert signals["has_booking"] is False
    assert signals["platform"] == "unknown"
    assert signals["tech_stack"] == []
    assert signals["review_count"] == 0
    assert signals["rating"] == pytest.approx(0.0)
    assert signals["ads_detected"] is False
    assert signals["competitor_count"] == 0


def test_analyze_business_clamps_rating():
    """Rating is clamped to 0-5 regardless of input."""
    signals_high = analyze_business({"rating": 99.9})
    assert signals_high["rating"] == pytest.approx(5.0)

    signals_low = analyze_business({"rating": -3.0})
    assert signals_low["rating"] == pytest.approx(0.0)

    signals_normal = analyze_business({"rating": 3.5})
    assert signals_normal["rating"] == pytest.approx(3.5)


def test_analyze_business_platform_inferred_from_html_signals():
    """Platform is inferred from html signals when no explicit platform key."""
    data = {"website_html_signals": ["wp-content/themes/twenty", "wp-json"]}
    signals = analyze_business(data)
    assert signals["platform"] == "wordpress"


def test_analyze_business_feature_variants_detected():
    """Both alias spellings activate has_cta and has_booking."""
    data_aliases = {
        "website_url": "https://example.com",
        "website_features": ["call_to_action", "calendar"],
    }
    signals = analyze_business(data_aliases)
    assert signals["has_cta"] is True
    assert signals["has_booking"] is True


# ---------------------------------------------------------------------------
# score_opportunity
# ---------------------------------------------------------------------------


def test_score_opportunity_axes_in_0_to_100_range():
    """All axes are clamped between 0 and 100 for any valid signal set."""
    # Worst-case: all features present, many reviews, good rating
    signals_rich = {
        "has_website": True,
        "has_cta": True,
        "has_booking": True,
        "platform": "wordpress",
        "review_count": 200,
        "rating": 4.9,
        "ads_detected": True,
        "competitor_count": 20,
    }
    scores = score_opportunity(signals_rich)
    for axis, value in scores.items():
        assert 0 <= value <= 100, f"axis {axis} out of range: {value}"

    # Best-case opportunity: no presence at all
    signals_empty = {
        "has_website": False,
        "has_cta": False,
        "has_booking": False,
        "platform": "unknown",
        "review_count": 0,
        "rating": 0.0,
        "ads_detected": False,
        "competitor_count": 0,
    }
    scores2 = score_opportunity(signals_empty)
    for axis, value in scores2.items():
        assert 0 <= value <= 100, f"axis {axis} out of range: {value}"


def test_score_opportunity_website_axis_logic():
    """Website axis follows the four-tier logic from the spec."""
    # No website -> 100
    s = score_opportunity({"has_website": False, "has_cta": False, "has_booking": False})
    assert s["website"] == 100

    # Website, no CTA -> 60
    s = score_opportunity({"has_website": True, "has_cta": False, "has_booking": False})
    assert s["website"] == 60

    # Website + CTA, no booking -> 30
    s = score_opportunity({"has_website": True, "has_cta": True, "has_booking": False})
    assert s["website"] == 30

    # Website + CTA + booking -> 10
    s = score_opportunity({"has_website": True, "has_cta": True, "has_booking": True})
    assert s["website"] == 10


def test_score_opportunity_reputation_axis_logic():
    """Reputation axis tracks review_count and rating thresholds."""
    # < 5 reviews -> 90
    assert score_opportunity({"review_count": 4, "rating": 4.8})["reputation"] == 90

    # rating < 4.0 -> 60
    assert score_opportunity({"review_count": 10, "rating": 3.5})["reputation"] == 60

    # 4.0 <= rating < 4.5 -> 30
    assert score_opportunity({"review_count": 10, "rating": 4.2})["reputation"] == 30

    # rating >= 4.5 -> 10
    assert score_opportunity({"review_count": 10, "rating": 4.8})["reputation"] == 10


# ---------------------------------------------------------------------------
# map_products
# ---------------------------------------------------------------------------


def test_map_products_primary_and_secondary_selection():
    """Primary is highest-scoring axis; secondary is second if score >= 50."""
    scores = {
        "website": 90,
        "seo": 70,
        "ads": 40,
        "automation": 30,
        "reputation": 20,
    }
    result = map_products(scores)
    assert result["primary"] == "website_build"
    assert result["secondary"] == "seo_package"


def test_map_products_secondary_none_when_below_threshold():
    """Secondary is None when the second-ranked axis scores below 50."""
    scores = {
        "website": 80,
        "seo": 49,
        "ads": 30,
        "automation": 10,
        "reputation": 5,
    }
    result = map_products(scores)
    assert result["primary"] == "website_build"
    assert result["secondary"] is None


def test_map_products_tie_breaks_by_axis_order():
    """Equal scores resolve by axis declaration order (website > seo > ...)."""
    scores = {
        "website": 60,
        "seo": 60,
        "ads": 60,
        "automation": 60,
        "reputation": 60,
    }
    result = map_products(scores)
    # website appears first in axis order
    assert result["primary"] == "website_build"
    # seo appears second
    assert result["secondary"] == "seo_package"


# ---------------------------------------------------------------------------
# determine_pitch_angle
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "signals,scores,expected_angle",
    [
        # trust_gap: no website
        (
            {"has_website": False, "has_cta": False, "has_booking": False},
            {"reputation": 50, "seo": 40, "ads": 40, "automation": 40},
            "trust_gap",
        ),
        # trust_gap: reputation >= 70
        (
            {"has_website": True, "has_cta": True, "has_booking": True},
            {"reputation": 90, "seo": 20, "ads": 20, "automation": 20},
            "trust_gap",
        ),
        # conversion_leak: website + no CTA + no booking
        (
            {"has_website": True, "has_cta": False, "has_booking": False},
            {"reputation": 30, "seo": 40, "ads": 40, "automation": 40},
            "conversion_leak",
        ),
        # time_leak: automation >= 70
        (
            {"has_website": True, "has_cta": True, "has_booking": False},
            {"reputation": 10, "seo": 40, "ads": 40, "automation": 80},
            "time_leak",
        ),
        # visibility_gap: seo >= 70
        (
            {"has_website": True, "has_cta": True, "has_booking": True},
            {"reputation": 10, "seo": 80, "ads": 40, "automation": 20},
            "visibility_gap",
        ),
        # visibility_gap: ads >= 70
        (
            {"has_website": True, "has_cta": True, "has_booking": True},
            {"reputation": 10, "seo": 40, "ads": 80, "automation": 20},
            "visibility_gap",
        ),
        # general_growth: none of the above
        (
            {"has_website": True, "has_cta": True, "has_booking": True},
            {"reputation": 10, "seo": 40, "ads": 40, "automation": 20},
            "general_growth",
        ),
    ],
)
def test_determine_pitch_angle_covers_each_category(signals, scores, expected_angle):
    assert determine_pitch_angle(signals, scores) == expected_angle
