"""Tests for the deterministic call-sheet intelligence (Vapi-style heuristics).

``derive_callsheet_intel`` maps a prospect's observed discovery signals to a
dominant business gap, a pain hypothesis, a HustleForge offer, a prospect-
specific pitch, and the qualifying questions the operator should ask.
"""

from __future__ import annotations

from backend.prospecting.callsheet_intel import derive_callsheet_intel
from backend.prospecting.models import ProspectRecord


def _p(**overrides) -> ProspectRecord:
    base = dict(company_name="Acme Co", industry="dentist", city="Yuba City")
    base.update(overrides)
    return ProspectRecord(**base)


def test_empty_record_falls_back_to_manual_ops():
    """An un-enriched prospect resolves to the universal lead-gen angle and
    never renders a blank block."""
    intel = derive_callsheet_intel(ProspectRecord())
    assert intel.primary_gap == "manual_ops"
    assert intel.pain_summary
    assert intel.offer
    assert intel.pitch
    assert intel.issues  # generic fallback issues
    assert intel.qualify_prompts


def test_no_website_is_the_dominant_gap():
    intel = derive_callsheet_intel(_p(website_status="no_website"))
    assert intel.primary_gap == "no_presence"
    assert intel.gap_scores["no_presence"] == 100
    assert "no-web-presence" in intel.signal_tags
    assert any("no working" in iss.lower() for iss in intel.issues)


def test_no_website_offer_is_a_build_not_an_audit():
    """Product coherence: a prospect with NO website can't be sold an SEO
    Audit (there's nothing to audit). The offer must be a site/presence
    build. Regression guard for the 6/30 'audit a non-existent site' bug."""
    intel = derive_callsheet_intel(_p(website_status="no_website"))
    offer = (intel.offer or "").lower()
    assert "audit" not in offer, f"no-website offer must not mention an audit: {intel.offer!r}"
    assert any(w in offer for w in ("build", "site", "presence", "website")), (
        f"no-website offer should be a build: {intel.offer!r}"
    )
    # The pitch should pitch building a presence, not auditing one.
    assert "build" in (intel.pitch or "").lower()


def test_broken_website_scores_below_absent_website():
    """A down site (owner may not know) scores under a wholly-absent one."""
    down = derive_callsheet_intel(_p(website_status="server_error"))
    absent = derive_callsheet_intel(_p(website_status="no_website"))
    assert down.gap_scores["no_presence"] < absent.gap_scores["no_presence"]
    assert down.primary_gap == "no_presence"


def test_gone_website_is_a_top_no_presence_gap():
    """An HTTP 410 site is effectively absent — top no_presence score."""
    intel = derive_callsheet_intel(_p(website_status="gone"))
    assert intel.gap_scores["no_presence"] == 100
    assert intel.primary_gap == "no_presence"


def test_access_blocked_is_not_a_no_presence_gap():
    """A WAF-blocked crawl must not be pitched as 'you have no website' —
    the site is healthy for a human visitor."""
    intel = derive_callsheet_intel(_p(website_status="access_blocked", seo_score=85))
    assert intel.gap_scores["no_presence"] == 0
    assert intel.primary_gap != "no_presence"
    joined = " ".join(intel.issues).lower()
    assert "no working owned website" not in joined


def test_weak_seo_drives_visibility_gap():
    intel = derive_callsheet_intel(_p(website_status="live", seo_score=20))
    assert intel.primary_gap == "weak_visibility"
    # The pitch quotes the prospect's actual score.
    assert "20/100" in intel.pitch


def test_seo_score_zero_is_unaudited_not_a_gap():
    """seo_score 0 means unaudited — it must not fire a spurious 100 gap."""
    intel = derive_callsheet_intel(_p(website_status="live", seo_score=0))
    assert intel.gap_scores["weak_visibility"] == 0


def test_good_seo_score_no_visibility_gap():
    intel = derive_callsheet_intel(_p(website_status="live", seo_score=85))
    assert intel.gap_scores["weak_visibility"] == 0


def test_low_rating_drives_reputation_gap():
    intel = derive_callsheet_intel(
        _p(website_status="live", seo_score=80, review_rating="3.0", review_count="60")
    )
    assert intel.primary_gap == "reputation"
    assert "3.0" in intel.pitch


def test_thin_review_volume_drives_reputation_gap():
    intel = derive_callsheet_intel(
        _p(website_status="live", seo_score=80, review_rating="4.8", review_count="3")
    )
    assert intel.gap_scores["reputation"] > 0


def test_volume_driven_reputation_pitch_does_not_misread_a_good_rating():
    """A thin-but-5-star profile must not be pitched as 'below the bar'."""
    intel = derive_callsheet_intel(
        _p(website_status="live", seo_score=80, review_rating="5.0", review_count="11")
    )
    assert intel.primary_gap == "reputation"
    assert "below the bar" not in intel.pitch
    assert "only 11 reviews" in intel.pitch


def test_failing_security_grade_drives_trust_gap():
    intel = derive_callsheet_intel(_p(website_status="live", seo_score=85, security_grade="F"))
    assert intel.primary_gap == "trust_posture"
    assert intel.gap_scores["trust_posture"] == 100
    assert "F" in intel.pitch


def test_clean_security_grade_no_trust_gap():
    intel = derive_callsheet_intel(_p(website_status="live", seo_score=85, security_grade="A"))
    assert intel.gap_scores["trust_posture"] == 0


def test_secondary_gap_yields_a_pivot():
    """Two real gaps → the secondary rides as a pitch pivot."""
    intel = derive_callsheet_intel(_p(website_status="live", seo_score=15, security_grade="F"))
    assert intel.primary_gap == "trust_posture"
    assert intel.secondary_gap == "weak_visibility"
    assert intel.pivot
    assert intel.pivot != intel.pitch


def test_single_dominant_gap_pivots_to_manual_ops():
    """When only one observed gap is present, manual_ops is the pivot."""
    intel = derive_callsheet_intel(_p(website_status="no_website"))
    assert intel.primary_gap == "no_presence"
    assert intel.secondary_gap == "manual_ops"
    assert intel.pivot


def test_issues_reflect_only_observed_signals():
    intel = derive_callsheet_intel(_p(website_status="live", seo_score=30, security_grade="D"))
    joined = " ; ".join(intel.issues)
    assert "30/100" in joined
    assert "grade D" in joined
    # No review signal was supplied — no review issue line.
    assert "review" not in joined.lower()


def test_qualify_prompts_include_base_vapi_questions():
    """Every call sheet carries the universal Vapi qualification questions."""
    intel = derive_callsheet_intel(_p(website_status="live", seo_score=40))
    joined = " ".join(intel.qualify_prompts)
    assert "bringing in new clients" in joined
    assert "leads come in a week" in joined
    # Gap-specific prompts come first.
    assert len(intel.qualify_prompts) >= 3


def test_gap_scores_always_carry_every_axis():
    intel = derive_callsheet_intel(ProspectRecord())
    assert set(intel.gap_scores) == {
        "no_presence",
        "weak_visibility",
        "reputation",
        "trust_posture",
        "manual_ops",
    }


def test_pitch_names_the_company():
    intel = derive_callsheet_intel(
        _p(company_name="Bright Smile Dental", website_status="no_website")
    )
    assert "Bright Smile Dental" in intel.pitch


def test_unparseable_review_fields_do_not_raise():
    """Garbage review strings degrade to no reputation signal, never an error."""
    intel = derive_callsheet_intel(
        _p(website_status="live", seo_score=80, review_rating="N/A", review_count="lots")
    )
    assert intel.gap_scores["reputation"] == 0


# --- manual_ops volume scoring (added 2026-05-21) -------------------------
# High customer volume is the pre-call proxy for manual-ops pain: a busy
# business has lead flow hand-run capture + follow-up cannot keep up with.


def test_manual_ops_scores_zero_at_or_below_volume_floor():
    """A quiet business (<=25 reviews) reads as no manual-ops signal — that
    thin profile is a reputation gap, not a volume gap."""
    intel = derive_callsheet_intel(_p(website_status="live", seo_score=80, review_count="25"))
    assert intel.gap_scores["manual_ops"] == 0


def test_manual_ops_saturates_at_high_volume():
    intel = derive_callsheet_intel(_p(website_status="live", seo_score=80, review_count="1127"))
    assert intel.gap_scores["manual_ops"] == 100


def test_manual_ops_ramps_between_floor_and_ceiling():
    intel = derive_callsheet_intel(_p(website_status="live", seo_score=80, review_count="140"))
    assert 0 < intel.gap_scores["manual_ops"] < 100


def test_high_volume_alone_drives_manual_ops_primary():
    """High volume with no other observed gap makes automation the primary."""
    intel = derive_callsheet_intel(
        _p(website_status="live", seo_score=80, review_rating="4.8", review_count="600")
    )
    assert intel.primary_gap == "manual_ops"
    assert intel.gap_scores["manual_ops"] == 100


def test_high_volume_leads_with_automation_over_security_f():
    """The Juniper case: a high-volume practice with a security-F site leads
    with the automation angle; the trust gap rides as the pivot."""
    intel = derive_callsheet_intel(
        _p(
            website_status="live",
            seo_score=80,
            review_rating="4.9",
            review_count="1127",
            security_grade="F",
        )
    )
    assert intel.primary_gap == "manual_ops"
    assert intel.secondary_gap == "trust_posture"
    assert intel.pivot and intel.pivot != intel.pitch
    assert any("volume" in iss.lower() for iss in intel.issues)


def test_low_volume_security_f_still_leads_with_trust_gap():
    """Below the volume-saturation point the automation flip does NOT fire —
    a security-F still leads. The flip is targeted at high-volume prospects."""
    intel = derive_callsheet_intel(
        _p(
            website_status="live",
            seo_score=80,
            review_count="40",
            security_grade="F",
        )
    )
    assert intel.primary_gap == "trust_posture"
