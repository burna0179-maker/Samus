"""Callsheet templating populates all 6 callsheet_* fields."""

from __future__ import annotations


def test_callsheet_fields_all_populated():
    from backend.prospecting.callsheet import build_call_sheet
    from backend.prospecting.models import ProspectRecord

    p = ProspectRecord(
        company_name="Acme Roofing",
        industry="construction",
        zipcode="95993",
        call_priority="warm",
    )
    out = build_call_sheet(p)
    assert out.callsheet_issues
    assert out.callsheet_offer
    assert out.callsheet_pitch
    assert out.callsheet_opener
    assert out.callsheet_voicemail
    assert out.callsheet_objections


def test_callsheet_offer_and_pitch_are_signal_driven():
    """Offer + pitch reflect each prospect's observed gaps, not a priority
    template — two prospects with different signals get different copy."""
    from backend.prospecting.callsheet import build_call_sheet
    from backend.prospecting.models import ProspectRecord

    # No working website — the loudest hook.
    no_site = build_call_sheet(
        ProspectRecord(
            company_name="X",
            industry="finance",
            website_status="no_website",
        )
    )
    # A live site that simply ranks poorly on local SEO.
    weak_seo = build_call_sheet(
        ProspectRecord(
            company_name="Y",
            industry="finance",
            website_status="live",
            seo_score=25,
        )
    )
    assert no_site.callsheet_offer != weak_seo.callsheet_offer
    assert no_site.callsheet_pitch != weak_seo.callsheet_pitch
    # The pitch names the prospect — it is not a generic template line.
    assert "X" in no_site.callsheet_pitch
    assert "25/100" in weak_seo.callsheet_pitch


def test_callsheet_issues_reflect_observed_signals():
    """callsheet_issues lists what discovery actually found for this prospect."""
    from backend.prospecting.callsheet import build_call_sheet
    from backend.prospecting.models import ProspectRecord

    p = build_call_sheet(
        ProspectRecord(
            company_name="Z",
            industry="dentist",
            website_status="live",
            seo_score=30,
            security_grade="F",
        )
    )
    assert "30/100" in p.callsheet_issues
    assert "grade F" in p.callsheet_issues


def test_callsheet_opener_is_gatekeeper_aware_and_withholds_finding():
    """Round-2 fix (2026-07-02): the opener routes to the DECISION-MAKER first,
    keeps an honest cold-call pattern-interrupt + a value TEASER, and WITHHOLDS
    the specific finding (the SEO score) so a receptionist never gets pitched.
    The specific finding is still carried on the record for the owner beat."""
    from backend.prospecting.callsheet import build_call_sheet
    from backend.prospecting.models import ProspectRecord

    out = build_call_sheet(
        ProspectRecord(
            company_name="Diamond Tax",
            industry="finance",
            zipcode="95993",
            website_status="live",
            seo_score=42,
            call_priority="warm",
        )
    )
    opener = out.callsheet_opener
    lowered = opener.lower()
    # Names the business.
    assert "Diamond Tax" in opener
    # Honest pattern-interrupt + value teaser (disarms without over-committing).
    assert "cold call" in lowered
    # Asks for the owner / decision-maker up front (the gatekeeper route).
    assert "owner" in lowered
    assert "who handles" in lowered or "whoever handles" in lowered
    # The SPECIFIC finding is WITHHELD from the opener — it must not leak the
    # concrete audit detail (the SEO score) that a gatekeeper can't act on.
    assert "42" not in opener
    # Banned telemarketer tells are gone.
    assert "thirty seconds" not in lowered
    assert "a minute" not in lowered
    assert "do you have a minute" not in lowered
    assert "more visibility on google" not in lowered
    # But the specific finding IS preserved on the record for the owner beat.
    assert out.callsheet_finding
    assert "42" in out.callsheet_finding


def test_callsheet_opener_uses_industry_customer_noun():
    """A dentist loses 'patients'; a generic business loses 'customers'."""
    from backend.prospecting.callsheet import build_call_sheet
    from backend.prospecting.models import ProspectRecord

    dentist = build_call_sheet(
        ProspectRecord(
            company_name="Bright Smiles",
            industry="dentist",
            website_status="live",
            seo_score=40,
        )
    )
    generic = build_call_sheet(
        ProspectRecord(
            company_name="Acme LLC",
            industry="landscaping",
            website_status="live",
            seo_score=40,
        )
    )
    assert "patients" in dentist.callsheet_opener.lower()
    assert "customers" in generic.callsheet_opener.lower()


def test_callsheet_voicemail_hook_first_no_banned_phrases():
    from backend.prospecting.callsheet import build_call_sheet
    from backend.prospecting.models import ProspectRecord

    out = build_call_sheet(
        ProspectRecord(
            company_name="Diamond Tax",
            industry="finance",
            security_grade="F",
            website_status="live",
        )
    )
    vm = out.callsheet_voicemail.lower()
    assert "diamond tax" in vm
    assert "[PHONE]" in out.callsheet_voicemail  # placeholder still present
    assert "more visibility on google" not in vm
    assert "thirty seconds" not in vm
