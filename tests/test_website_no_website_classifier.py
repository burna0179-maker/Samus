"""Tests for backend.website.no_website_classifier."""
import pytest

from backend.prospecting.models import ProspectRecord
from backend.website.no_website_classifier import (
    NoWebsiteClassification,
    WebsiteGap,
    WebsiteProspectTier,
    classify,
    surface,
)


def _rec(**kwargs) -> ProspectRecord:
    defaults = dict(
        prospect_id="p1",
        company_name="Acme Plumbing",
        phone="555-1234",
        industry="plumbing",
        city="Sacramento",
        state="CA",
        lead_score=75,
    )
    defaults.update(kwargs)
    return ProspectRecord(**defaults)


# ---------------------------------------------------------------------------
# classify — gap detection
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("status,expected_gap", [
    ("no_website", WebsiteGap.NO_WEBSITE),
    ("domain_unresolved", WebsiteGap.NO_WEBSITE),
    ("gone", WebsiteGap.GONE),
    ("parked", WebsiteGap.PARKED),
    ("social_only", WebsiteGap.SOCIAL_ONLY),
    ("empty", WebsiteGap.BROKEN),
    ("unreachable", WebsiteGap.BROKEN),
    ("unreachable_timeout", WebsiteGap.BROKEN),
    ("server_error", WebsiteGap.BROKEN),
])
def test_gap_from_status(status, expected_gap):
    rec = _rec(website_status=status)
    cls = classify(rec)
    assert cls is not None
    assert cls.gap == expected_gap


def test_live_website_returns_none():
    rec = _rec(website_status="live", website_url="https://acme.com")
    assert classify(rec) is None


def test_access_blocked_returns_none():
    rec = _rec(website_status="access_blocked", website_url="https://acme.com")
    assert classify(rec) is None


def test_empty_url_and_status_treated_as_no_website():
    rec = _rec(website_url="", website_status="")
    cls = classify(rec)
    assert cls is not None
    assert cls.gap == WebsiteGap.NO_WEBSITE


# ---------------------------------------------------------------------------
# classify — tier assignment
# ---------------------------------------------------------------------------

def test_high_value_tier_requires_score_and_contact():
    rec = _rec(website_status="no_website", lead_score=80, phone="555-0000")
    assert classify(rec).tier == WebsiteProspectTier.HIGH_VALUE


def test_high_score_no_contact_is_quick_win():
    rec = _rec(website_status="no_website", lead_score=80, phone="", owner_email="", contact_emails="")
    assert classify(rec).tier == WebsiteProspectTier.QUICK_WIN


def test_warm_score_is_quick_win():
    rec = _rec(website_status="no_website", lead_score=50, phone="555-0000")
    assert classify(rec).tier == WebsiteProspectTier.QUICK_WIN


def test_low_score_is_low_priority():
    rec = _rec(website_status="no_website", lead_score=30, phone="555-0000")
    assert classify(rec).tier == WebsiteProspectTier.LOW_PRIORITY


# ---------------------------------------------------------------------------
# classify — output shape
# ---------------------------------------------------------------------------

def test_classification_has_pitch_hook_and_brief():
    rec = _rec(website_status="no_website")
    cls = classify(rec)
    assert cls.pitch_hook
    assert "Acme Plumbing" in cls.pitch_hook
    assert cls.brief_draft.business_name == "Acme Plumbing"
    assert cls.brief_draft.contact_phone == "555-1234"
    assert cls.brief_draft.registered_state == "CA"


def test_broken_action_says_rebuild():
    rec = _rec(website_status="empty", lead_score=70, phone="x")
    cls = classify(rec)
    assert "rebuild" in cls.action


def test_revenue_floor():
    rec = _rec(website_status="no_website")
    assert classify(rec).estimated_revenue_usd == 800


# ---------------------------------------------------------------------------
# surface — ranking
# ---------------------------------------------------------------------------

def test_surface_filters_live_sites():
    records = [
        _rec(prospect_id="a", website_status="live", website_url="https://a.com"),
        _rec(prospect_id="b", website_status="no_website"),
    ]
    results = surface(records)
    assert len(results) == 1
    assert results[0][0].prospect_id == "b"


def test_surface_orders_high_value_first():
    records = [
        _rec(prospect_id="low", website_status="no_website", lead_score=20, phone=""),
        _rec(prospect_id="high", website_status="no_website", lead_score=80, phone="555"),
        _rec(prospect_id="mid", website_status="no_website", lead_score=50, phone="555"),
    ]
    ranked = surface(records)
    ids = [r.prospect_id for r, _ in ranked]
    assert ids[0] == "high"
    # low_priority should be last
    assert ids[-1] == "low"


def test_surface_same_tier_sorted_by_lead_score_desc():
    records = [
        _rec(prospect_id="a", website_status="no_website", lead_score=50, phone="555"),
        _rec(prospect_id="b", website_status="no_website", lead_score=60, phone="555"),
    ]
    ranked = surface(records)
    assert ranked[0][0].prospect_id == "b"


def test_surface_empty_list():
    assert surface([]) == []
