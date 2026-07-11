"""Prospecting exclusions — government offices + the operator denylist.

Covers backend.prospecting.exclusions.exclusion_reason and its application in
discover_for_zipcode: government / public-sector offices are dropped by Google
Places `types`, and an operator denylist drops too-institutional orgs (like
Ampla Health) that Google tags as ordinary businesses.
"""

from __future__ import annotations


def test_government_place_type_is_excluded():
    from backend.prospecting.exclusions import exclusion_reason

    reason = exclusion_reason(
        place_types="local_government_office, point_of_interest, establishment",
        website_url="https://cityhall.example.gov",
        company_name="Marysville City Hall",
    )
    assert reason == "government_office"


def test_post_office_and_police_are_excluded():
    from backend.prospecting.exclusions import exclusion_reason

    assert (
        exclusion_reason(
            place_types="post_office, establishment",
            website_url="https://usps.com",
            company_name="US Post Office",
        )
        == "government_office"
    )
    assert (
        exclusion_reason(
            place_types="police",
            website_url="",
            company_name="City PD",
        )
        == "government_office"
    )


def test_denylisted_domain_is_excluded():
    """Ampla Health — Google tags it medical_clinic; caught by the denylist."""
    from backend.prospecting.exclusions import exclusion_reason

    reason = exclusion_reason(
        place_types="medical_clinic, pharmacy, doctor, store, point_of_interest, health, establishment",
        website_url="https://www.amplahealth.org/?utm_source=google&utm_medium=organic",
        company_name="Ampla Health Lindhurst Medical Clinic & Xpress Care",
    )
    assert reason == "denylist_domain:amplahealth.org"


def test_denylist_matches_www_bare_and_subdomain():
    from backend.prospecting.exclusions import exclusion_reason

    for url in (
        "https://amplahealth.org",
        "amplahealth.org",
        "https://www.amplahealth.org/contact",
        "https://clinic.amplahealth.org/about",
    ):
        assert (
            exclusion_reason(
                place_types="doctor",
                website_url=url,
                company_name="x",
            )
            == "denylist_domain:amplahealth.org"
        ), url


def test_ordinary_prospect_is_kept():
    from backend.prospecting.exclusions import exclusion_reason

    assert (
        exclusion_reason(
            place_types="dentist, doctor, health, point_of_interest, establishment",
            website_url="https://smiletowndental.com",
            company_name="Smiletown Dental",
        )
        == ""
    )


def test_name_substring_denylist(monkeypatch):
    """EXCLUDED_NAME_SUBSTRINGS catches by company-name substring when set."""
    import backend.prospecting.exclusions as ex

    monkeypatch.setattr(ex, "EXCLUDED_NAME_SUBSTRINGS", {"county of"})
    reason = ex.exclusion_reason(
        place_types="establishment",
        website_url="https://x.com",
        company_name="County of Sutter Public Works",
    )
    assert reason == "denylist_name:county of"


def test_discover_for_zipcode_drops_excluded_places(monkeypatch):
    """discover_for_zipcode skips a government office + a denylisted domain,
    and keeps the ordinary business."""
    import backend.prospecting.place_search as ps

    fake_response = {
        "places": [
            {
                "id": "p_gov",
                "displayName": {"text": "City Hall"},
                "websiteUri": "https://city.example.gov",
                "types": ["local_government_office", "establishment"],
            },
            {
                "id": "p_deny",
                "displayName": {"text": "Ampla Health Clinic"},
                "websiteUri": "https://www.amplahealth.org/",
                "types": ["medical_clinic", "doctor"],
            },
            {
                "id": "p_ok",
                "displayName": {"text": "Smiletown Dental"},
                "websiteUri": "https://smiletowndental.com",
                "types": ["dentist", "doctor"],
            },
        ],
    }
    monkeypatch.setattr(ps, "search_text", lambda *a, **kw: fake_response)

    out = ps.discover_for_zipcode(zipcode="95961", industries=["dentist"])
    assert {p.company_name for p in out} == {"Smiletown Dental"}
