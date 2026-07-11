"""Lead scoring + priority classification — continuous 0-100 scorer.

The scorer was recalibrated from coarse step-functions (which topped out at 52
and bunched every solid lead at exactly that) to four continuous 25-point
components: industry / rating / review-volume / SEO-opportunity.
"""

from __future__ import annotations


def _record(**fields):
    from backend.prospecting.models import ProspectRecord

    return ProspectRecord(**fields)


def test_score_industry_weight_ordering():
    """A Tier-A vertical outscores a low-weight one outscores an unknown."""
    from backend.prospecting.scorer import score_prospect

    tier_a = _record(industry="dentist")
    low = _record(industry="technology")
    unknown = _record(industry="taxidermy")
    assert score_prospect(tier_a) > score_prospect(low) > score_prospect(unknown)


def test_score_rises_with_rating():
    """Rating is continuous — a 4.9 clearly outscores a 4.0 (the old scorer
    gave both the same coarse +10)."""
    from backend.prospecting.scorer import score_prospect

    great = _record(industry="dentist", review_rating="4.9")
    ok = _record(industry="dentist", review_rating="4.0")
    weak = _record(industry="dentist", review_rating="3.2")
    assert score_prospect(great) > score_prospect(ok) > score_prospect(weak)


def test_score_rises_with_review_volume_log_scaled():
    """Review count is log-scaled — a 400-review business clearly beats a
    12-review one (the old scorer tied everything with 50+ reviews)."""
    from backend.prospecting.scorer import score_prospect

    many = _record(industry="dentist", review_count="400")
    some = _record(industry="dentist", review_count="40")
    few = _record(industry="dentist", review_count="12")
    none = _record(industry="dentist", review_count="0")
    assert score_prospect(many) > score_prospect(some) > score_prospect(few) > score_prospect(none)


def test_seo_opportunity_is_inverted():
    """Worse SEO = better lead. A prospect with seo_score 20 outscores an
    otherwise-identical one with seo_score 90."""
    from backend.prospecting.scorer import score_prospect

    poor_seo = _record(industry="dentist", seo_score=20)
    strong_seo = _record(industry="dentist", seo_score=90)
    assert score_prospect(poor_seo) > score_prospect(strong_seo)


def test_score_spreads_no_52_ceiling():
    """Two prospects differing only in rating must NOT tie — the old scorer
    bunched every solid lead at exactly 52, and a strong lead can now reach
    the hot tier (>=70), which the old formula (max 52) could never."""
    from backend.prospecting.scorer import score_prospect

    a = _record(industry="dentist", review_rating="4.9", review_count="300", seo_score=30)
    b = _record(industry="dentist", review_rating="4.1", review_count="300", seo_score=30)
    assert score_prospect(a) != score_prospect(b)
    assert score_prospect(a) >= 70


def test_score_bounded_0_to_100():
    from backend.prospecting.scorer import score_prospect

    maxed = _record(industry="dentist", review_rating="5.0", review_count="999", seo_score=0)
    floor = _record(industry="taxidermy", review_rating="0", review_count="0", seo_score=100)
    assert score_prospect(floor) >= 0
    assert score_prospect(maxed) <= 100
    assert score_prospect(maxed) > score_prospect(floor)


def test_review_points_saturate():
    """Beyond the saturation point, more reviews don't keep adding score."""
    from backend.prospecting.scorer import score_prospect

    at_sat = _record(industry="dentist", review_count="500")
    way_over = _record(industry="dentist", review_count="5000")
    assert score_prospect(at_sat) == score_prospect(way_over)


def test_access_blocked_site_gets_neutral_seo_credit():
    """A WAF-blocked site (seo unmeasurable) must not collect the full
    max-opportunity an seo_score 0 would otherwise grant — it gets neutral
    half-credit, so it scores BELOW a genuinely poor-SEO prospect and ABOVE a
    strong-SEO one (2026-05-21 false-positive sweep)."""
    from backend.prospecting.scorer import score_prospect

    blocked = _record(industry="dentist", seo_score=0, website_status="access_blocked")
    poor_seo = _record(industry="dentist", seo_score=10, website_status="live")
    strong_seo = _record(industry="dentist", seo_score=95, website_status="live")
    assert score_prospect(poor_seo) > score_prospect(blocked) > score_prospect(strong_seo)


def test_access_blocked_scores_below_a_genuinely_broken_site():
    """A genuinely broken/absent web presence keeps full SEO-opportunity credit
    (intended pitch signal); a WAF-blocked-but-healthy site does not."""
    from backend.prospecting.scorer import score_prospect

    broken = _record(industry="dentist", seo_score=0, website_status="no_website")
    blocked = _record(industry="dentist", seo_score=0, website_status="access_blocked")
    assert score_prospect(broken) > score_prospect(blocked)
    # The gap is the full-vs-neutral SEO credit: 25 - 12.5 = ~13 points.
    assert score_prospect(broken) - score_prospect(blocked) in (12, 13)


def test_all_soft_failure_statuses_get_neutral_seo_credit():
    """Every status where the SEO crawl could not complete — blocked, timed
    out, unreachable, 5xx, 4xx, empty — yields the same neutral SEO credit when
    seo_score is 0; none collects the full max-opportunity (2026-05-21 sweep)."""
    from backend.prospecting.scorer import score_prospect

    base = score_prospect(_record(industry="dentist", seo_score=0, website_status="access_blocked"))
    for status in ("unreachable_timeout", "unreachable", "server_error", "http_error", "empty"):
        same = score_prospect(_record(industry="dentist", seo_score=0, website_status=status))
        assert same == base, status


def test_genuine_no_website_keeps_full_seo_opportunity():
    """A positively-absent web presence (no site / parked / dead domain / 410)
    keeps full SEO-opportunity credit — that 0 genuinely IS the pitch — so it
    outscores a soft-failure prospect whose SEO is merely unknown."""
    from backend.prospecting.scorer import score_prospect

    soft_fail = score_prospect(
        _record(industry="dentist", seo_score=0, website_status="unreachable_timeout")
    )
    for status in ("no_website", "parked", "social_only", "domain_unresolved", "gone"):
        genuine = score_prospect(_record(industry="dentist", seo_score=0, website_status=status))
        assert genuine > soft_fail, status


def test_measured_seo_score_unaffected_by_status():
    """A real measured score (> 0) is scored normally regardless of status."""
    from backend.prospecting.scorer import score_prospect

    live = score_prospect(_record(industry="dentist", seo_score=40, website_status="live"))
    blocked = score_prospect(
        _record(industry="dentist", seo_score=40, website_status="access_blocked")
    )
    assert live == blocked


def test_classify_priority_thresholds():
    """Recalibrated for the continuous scorer: hot >=70, warm >=45, low <45."""
    from backend.prospecting.scorer import classify_priority

    assert classify_priority(100) == "hot"
    assert classify_priority(70) == "hot"
    assert classify_priority(69) == "warm"
    assert classify_priority(45) == "warm"
    assert classify_priority(44) == "low"
    assert classify_priority(0) == "low"
