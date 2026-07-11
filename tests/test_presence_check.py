"""Tests for backend.website.presence_check — the pre-build insurance gate.
Places API is mocked; no network."""
from __future__ import annotations

import backend.prospecting.place_search as ps
from backend.website import presence_check as pc


def _place(name, *, website="", rating=None, reviews=None, ptype="Car dealer"):
    p = {
        "displayName": {"text": name},
        "regularOpeningHours": {"weekdayDescriptions": ["Mon: 9-5", "Tue: 9-5"]},
        "primaryTypeDisplayName": {"text": ptype},
        "formattedAddress": "1 Main St, Yuba City, CA",
        "nationalPhoneNumber": "(530) 555-1000",
    }
    if website:
        p["websiteUri"] = website
    if rating is not None:
        p["rating"] = rating
    if reviews is not None:
        p["userRatingCount"] = reviews
    return p


def _mock(monkeypatch, places):
    monkeypatch.setattr(ps, "search_text", lambda q, **k: {"places": places})


def test_existing_website_short_circuits():
    v = pc.verify_presence("Acme Auto", city="Yuba City", existing_website="http://acme.com")
    assert v.buildable is False
    assert "acme.com" in v.website


def test_places_website_blocks_build(monkeypatch):
    _mock(monkeypatch, [_place("Acme Auto", website="http://acmeauto.com", rating=4.8, reviews=28)])
    v = pc.verify_presence("Acme Auto", city="Yuba City", state="CA")
    assert v.buildable is False
    assert "acmeauto.com" in v.website
    assert v.business["rating"] == 4.8 and v.business["review_count"] == 28


def test_no_website_is_buildable_with_enrichment(monkeypatch):
    _mock(monkeypatch, [_place("Bob's Cleaning", rating=5, reviews=7, ptype="Services")])
    v = pc.verify_presence("Bob's Cleaning", city="Yuba City")
    assert v.buildable is True
    assert v.business["primary_type"] == "Services"
    assert v.business["review_count"] == 7
    assert len(v.business["hours"]) == 2


def test_unrelated_top_result_does_not_block(monkeypatch):
    # a DIFFERENT business with a site must NOT skip our no-site prospect
    _mock(monkeypatch, [_place("Totally Different Co", website="http://other.com")])
    monkeypatch.setattr(pc, "web_search_finds_site", lambda *a, **k: "")
    v = pc.verify_presence("USA Auto Sale", city="Yuba City")
    assert v.buildable is True                      # not blocked by an unrelated hit
    assert v.website == ""                           # the unrelated site's URL is not used


def test_web_search_returns_own_site_over_directory():
    import httpx

    def handler(req):
        return httpx.Response(200, json={"items": [
            {"link": "https://www.yelp.com/biz/acme"},
            {"link": "https://acmeauto.com/"},
        ]})

    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        site = pc.web_search_finds_site("Acme Auto", "Yuba City",
                                        api_key="K", cse_id="cx", http_client=c)
    assert site == "https://acmeauto.com/"


def test_web_search_all_directories_returns_empty():
    import httpx

    def handler(req):
        return httpx.Response(200, json={"items": [
            {"link": "https://www.yelp.com/biz/acme"},
            {"link": "https://www.facebook.com/acme"},
        ]})

    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        assert pc.web_search_finds_site("Acme", "X", api_key="K", cse_id="cx",
                                        http_client=c) == ""


def test_web_search_no_creds_is_noop():
    assert pc.web_search_finds_site("Acme", "X", api_key="", cse_id="") == ""


def test_verify_presence_web_search_fallback_blocks(monkeypatch):
    # Places has NO linked site, but a web search finds one -> not buildable
    _mock(monkeypatch, [_place("Webtech Solutions")])          # no websiteUri
    monkeypatch.setattr(pc, "web_search_finds_site",
                        lambda *a, **k: "https://webtechsolution.org/")
    v = pc.verify_presence("Webtech Solutions", city="Yuba City")
    assert v.buildable is False
    assert "webtechsolution.org" in v.website


def test_no_places_results_is_buildable(monkeypatch):
    _mock(monkeypatch, [])
    v = pc.verify_presence("Nobody Ltd", city="Nowhere")
    assert v.buildable is True


def test_search_failure_fails_open(monkeypatch):
    def _boom(q, **k):
        raise RuntimeError("places down")
    monkeypatch.setattr(ps, "search_text", _boom)
    v = pc.verify_presence("Acme", city="X")
    assert v.buildable is True                      # insurance never hard-blocks
    assert "failed" in v.reason
