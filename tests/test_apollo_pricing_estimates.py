"""apollo_pricing — known endpoints have expected costs; unknown fall back."""
from __future__ import annotations

import logging

import pytest

from backend.common import apollo_pricing


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    monkeypatch.delenv("SAMUS_APOLLO_USD_PER_CREDIT", raising=False)
    yield


def test_known_endpoints():
    # default per-credit = 0.04
    assert apollo_pricing.estimate_call_cost("people_search") == pytest.approx(0.04)
    assert apollo_pricing.estimate_call_cost("email_unlock") == pytest.approx(0.04)
    assert apollo_pricing.estimate_call_cost("phone_unlock") == pytest.approx(0.32)
    assert apollo_pricing.estimate_call_cost("organization_search") == pytest.approx(0.0)


def test_unknown_endpoint_warns_and_assumes_one_credit(caplog):
    with caplog.at_level(logging.WARNING, logger="samus.common.apollo_pricing"):
        cost = apollo_pricing.estimate_call_cost("mystery_endpoint")
    assert cost == pytest.approx(0.04)
    assert any("unknown endpoint" in rec.message for rec in caplog.records)


def test_env_override_changes_rate(monkeypatch):
    monkeypatch.setenv("SAMUS_APOLLO_USD_PER_CREDIT", "0.10")
    assert apollo_pricing.usd_per_credit() == 0.10
    # phone_unlock = 8 credits * 0.10 = 0.80
    assert apollo_pricing.estimate_call_cost("phone_unlock") == pytest.approx(0.80)


def test_env_invalid_falls_back(monkeypatch):
    monkeypatch.setenv("SAMUS_APOLLO_USD_PER_CREDIT", "not-a-number")
    assert apollo_pricing.usd_per_credit() == 0.04


def test_env_negative_falls_back(monkeypatch):
    monkeypatch.setenv("SAMUS_APOLLO_USD_PER_CREDIT", "-1")
    assert apollo_pricing.usd_per_credit() == 0.04


def test_units_scale_linearly():
    # 5 search pages.
    assert apollo_pricing.estimate_call_cost("people_search", units=5) == pytest.approx(0.20)


def test_negative_units_clamped():
    assert apollo_pricing.estimate_call_cost("people_search", units=-3) == 0.0
