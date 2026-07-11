"""Offline tests for the Apollo people-source adapter."""
from __future__ import annotations

import json

import pytest

from backend.outreach import apollo_source
from backend.outreach.apollo_source import ApolloContact, ApolloError, search_people


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self) -> dict:
        return self._payload


class _FakeClient:
    """Captures the last POST and returns a queued response."""

    def __init__(self, response: _FakeResponse):
        self._response = response
        self.last_path = None
        self.last_json = None

    def post(self, path, json=None):  # noqa: A002 - mirror httpx signature
        self.last_path = path
        self.last_json = json
        return self._response

    def close(self):  # pragma: no cover - injected clients are not owned/closed
        raise AssertionError("injected client must not be closed by the adapter")


_APOLLO_PERSON = {
    "id": "p123",
    "first_name": "Dana",
    "last_name": "Reyes",
    "title": "Owner",
    "email": "dana@acmehvac.com",
    "email_status": "verified",
    "linkedin_url": "https://www.linkedin.com/in/danareyes",
    "city": "Yuba City",
    "state": "California",
    "country": "US",
    "organization": {
        "name": "Acme HVAC",
        "primary_domain": "acmehvac.com",
        "industry": "hvac",
    },
}


def test_search_people_parses_and_maps_fields():
    client = _FakeClient(_FakeResponse(200, {"people": [_APOLLO_PERSON]}))
    people = search_people(titles=["owner"], locations=["Yuba City, California, US"], client=client)

    assert client.last_path == apollo_source._SEARCH_PATH
    assert client.last_json["person_titles"] == ["owner"]
    assert len(people) == 1
    c = people[0]
    assert c.person_id == "p123"
    assert c.name == "Dana Reyes"
    assert c.email == "dana@acmehvac.com"
    assert c.email_status == "verified"
    assert c.company == "Acme HVAC"
    assert c.company_domain == "acmehvac.com"
    assert c.linkedin_url.endswith("/danareyes")
    assert c.sendable is True


def test_locked_email_is_not_sendable():
    c = ApolloContact(email="email_not_unlocked@domain.com", email_status="verified")
    assert c.email_locked is True
    assert c.sendable is False


def test_guessed_email_is_not_sendable_by_default():
    c = ApolloContact(email="x@y.com", email_status="guessed")
    assert c.sendable is False


def test_search_http_error_raises_apollo_error():
    client = _FakeClient(_FakeResponse(429, {"error": "rate limited"}))
    with pytest.raises(ApolloError):
        search_people(titles=["owner"], client=client)


def test_missing_api_key_raises(monkeypatch):
    monkeypatch.delenv("APOLLO_API_KEY", raising=False)
    with pytest.raises(ApolloError):
        # No injected client -> adapter must build one, which needs the key.
        search_people(titles=["owner"])
