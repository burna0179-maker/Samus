"""Places API client — request shape + response parsing (mocked HTTP)."""
from __future__ import annotations

import pytest


_SAMPLE_PLACE = {
    "id": "ChIJ_test_1",
    "displayName": {"text": "Nav Accounts"},
    "formattedAddress": "100 Main St, Yuba City, CA 95993",
    "addressComponents": [
        {"types": ["locality"], "longText": "Yuba City", "shortText": "Yuba City"},
        {"types": ["administrative_area_level_1"], "longText": "California", "shortText": "CA"},
        {"types": ["postal_code"], "longText": "95993", "shortText": "95993"},
    ],
    "nationalPhoneNumber": "(530) 777-3265",
    "websiteUri": "https://navaccounts.com/",
    "types": ["accounting", "finance"],
    "rating": 4.8,
    "userRatingCount": 23,
    "regularOpeningHours": {
        "weekdayDescriptions": ["Monday: 9am-5pm", "Tuesday: 9am-5pm"],
    },
    "editorialSummary": {
        "text": "Accounting firm offering tax prep and bookkeeping.",
    },
}


class _FakeResponse:
    def __init__(self, json_body, status_code=200):
        self._json = json_body
        self.status_code = status_code
        self.text = ""

    def json(self):
        return self._json


class _FakeClient:
    def __init__(self, responses):
        self._responses = list(responses)
        self.captured = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def post(self, url, *, json=None, headers=None):
        self.captured.append({"url": url, "json": json, "headers": headers})
        return self._responses.pop(0)


def _patch_httpx(monkeypatch, responses):
    fake = _FakeClient(responses)
    def make_client(*args, **kwargs):
        return fake
    import httpx
    monkeypatch.setattr(httpx, "Client", make_client)
    return fake


def test_search_text_builds_request(monkeypatch):
    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "test-key-123")
    from backend.common.settings import reload_settings
    reload_settings()
    fake = _patch_httpx(monkeypatch, [_FakeResponse({"places": []})])

    from backend.prospecting import place_search
    place_search.search_text("finance in 95993", max_results=25)

    call = fake.captured[0]
    assert call["url"] == "https://places.googleapis.com/v1/places:searchText"
    assert call["json"]["textQuery"] == "finance in 95993"
    assert call["json"]["maxResultCount"] == 20  # clamped from 25
    assert call["headers"]["X-Goog-Api-Key"] == "test-key-123"
    field_mask = call["headers"]["X-Goog-FieldMask"]
    assert "places.id" in field_mask
    assert "places.editorialSummary" in field_mask


def test_search_text_requires_api_key(monkeypatch):
    monkeypatch.delenv("GOOGLE_PLACES_API_KEY", raising=False)
    from backend.common.settings import reload_settings
    reload_settings()
    from backend.prospecting import place_search
    with pytest.raises(place_search.PlacesError, match="GOOGLE_PLACES_API_KEY"):
        place_search.search_text("anything")


def test_place_to_prospect_maps_fields():
    from backend.prospecting.place_search import place_to_prospect
    p = place_to_prospect(_SAMPLE_PLACE, zipcode="95993", industry="finance")
    assert p.prospect_id.startswith("pr_")
    assert p.account_id.startswith("acct_")
    assert p.company_name == "Nav Accounts"
    assert p.phone == "(530) 777-3265"
    assert p.website_url == "https://navaccounts.com/"
    assert p.city == "Yuba City"
    assert p.state == "CA"
    assert p.zipcode == "95993"
    assert p.industry == "finance"
    assert p.review_rating == "4.8"
    assert p.review_count == "23"
    assert p.business_description == "Accounting firm offering tax prep and bookkeeping."
    assert "Monday: 9am-5pm" in p.business_hours
    assert "accounting" in p.business_categories


def test_discover_for_zipcode_dedupe_and_website_filter(monkeypatch):
    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "k")
    from backend.common.settings import reload_settings
    reload_settings()
    response = {
        "places": [
            {"id": "p1", "displayName": {"text": "A"}, "websiteUri": "https://a.com",
             "addressComponents": [], "types": []},
            {"id": "p2", "displayName": {"text": "B"}, "websiteUri": "",
             "addressComponents": [], "types": []},  # no website → filtered
            {"id": "p1", "displayName": {"text": "A-dup"}, "websiteUri": "https://a.com",
             "addressComponents": [], "types": []},  # dup → filtered
            {"id": "p3", "displayName": {"text": "C"}, "websiteUri": "https://c.com",
             "addressComponents": [], "types": []},
        ],
    }
    _patch_httpx(monkeypatch, [_FakeResponse(response)])

    from backend.prospecting.place_search import discover_for_zipcode
    out = discover_for_zipcode(
        zipcode="95993",
        industries=["finance"],
        max_results_per_zip=10,
        must_have_website=True,
    )
    names = [p.company_name for p in out]
    assert names == ["A", "C"]
