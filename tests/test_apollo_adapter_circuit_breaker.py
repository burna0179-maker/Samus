"""Circuit-breaker tests for the Apollo enrichment adapter.

Verifies that consecutive auth failures suppress further calls for the
cooldown period, preventing credit drain when the key is invalid or the
account is credit-exhausted.
"""
from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from backend.prospecting import apollo_adapter as mod


@pytest.fixture(autouse=True)
def _reset_breaker():
    """Reset the module-level circuit-breaker state before each test."""
    mod._auth_fail_count = 0
    mod._auth_fail_suppressed_until = 0.0
    yield
    mod._auth_fail_count = 0
    mod._auth_fail_suppressed_until = 0.0


class FakeSettings:
    apollo_api_key = "test_key_for_breaker"


class FakeResp:
    def __init__(self, status_code, text=""):
        self.status_code = status_code
        self.text = text

    def json(self):
        return {}


def _patch_stack(resp_status):
    """Return a stack of patches that let enrich_via_apollo reach the HTTP path."""
    return [
        patch("backend.prospecting.apollo_adapter.get_settings",
              create=True, return_value=FakeSettings()),
        patch("backend.common.config.get_settings", return_value=FakeSettings()),
        patch("httpx.post", return_value=FakeResp(resp_status, "mock error")),
        patch("backend.common.apollo_budget.get_store"),
        patch("backend.common.apollo_pricing.estimate_call_cost", return_value=0.01),
    ]


def _call(**kw):
    defaults = dict(company_name="Acme", website_url="https://acme.com")
    defaults.update(kw)
    return mod.enrich_via_apollo(**defaults)


def test_breaker_trips_after_threshold():
    patches = _patch_stack(401)
    for p in patches:
        p.start()
    try:
        for _ in range(mod._AUTH_FAIL_THRESHOLD):
            _call()
        assert mod._auth_fail_suppressed_until > time.monotonic()
    finally:
        for p in patches:
            p.stop()


def test_breaker_suppresses_calls_during_cooldown():
    mod._auth_fail_suppressed_until = time.monotonic() + 9999
    with patch("httpx.post") as mock_post:
        result = _call()
    assert result == {}
    mock_post.assert_not_called()


def test_breaker_resets_on_success():
    mod._auth_fail_count = mod._AUTH_FAIL_THRESHOLD - 1
    patches = _patch_stack(200)
    for p in patches:
        p.start()
    try:
        _call()
        assert mod._auth_fail_count == 0
    finally:
        for p in patches:
            p.stop()
