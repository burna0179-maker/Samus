"""Mercury read-only client + the 3-tier cash resolver (Mercury > Found > Stripe).

No real HTTP: fetch_accounts is monkeypatched. Verifies wire-not-arm gating,
balance summing, caching, and fail-soft fallback."""

from __future__ import annotations

import pytest

from backend.finance import mercury_client as mc

_ENVS = ("SAMUS_MERCURY_ENABLED", "MERCURY_API_TOKEN")


def _arm(monkeypatch, on=True, token="secret-token:mercury_production_test"):
    monkeypatch.setenv("SAMUS_MERCURY_ENABLED", "1" if on else "0")
    if token:
        monkeypatch.setenv("MERCURY_API_TOKEN", token)
    else:
        monkeypatch.delenv("MERCURY_API_TOKEN", raising=False)
    # reset the module cache each test
    import time

    mc._cache["cash"] = None
    mc._cache["at"] = time.monotonic() - mc._CACHE_TTL_SEC - 1


def test_disarmed_returns_none(monkeypatch):
    _arm(monkeypatch, on=False)
    assert mc.mercury_available() is False
    assert mc.total_available_cash_usd() is None


def test_armed_without_token_is_unavailable(monkeypatch):
    _arm(monkeypatch, on=True, token=None)
    assert mc.mercury_available() is False
    assert mc.total_available_cash_usd() is None


def test_sums_available_balance_across_accounts(monkeypatch):
    _arm(monkeypatch)
    monkeypatch.setattr(
        mc,
        "fetch_accounts",
        lambda: [
            {"id": "a1", "availableBalance": 1200.50, "currentBalance": 1300.0},
            {"id": "a2", "availableBalance": 49.50, "currentBalance": 49.50},
        ],
    )
    assert mc.total_available_cash_usd(_now=1000.0) == pytest.approx(1250.0)


def test_empty_accounts_returns_none_not_zero(monkeypatch):
    _arm(monkeypatch)
    monkeypatch.setattr(mc, "fetch_accounts", lambda: [])
    assert mc.total_available_cash_usd() is None  # never cache a failure as $0


def test_balance_is_cached(monkeypatch):
    _arm(monkeypatch)
    calls = {"n": 0}

    def _fake():
        calls["n"] += 1
        return [{"availableBalance": 500.0}]

    monkeypatch.setattr(mc, "fetch_accounts", _fake)
    assert mc.total_available_cash_usd(_now=1000.0) == 500.0
    assert mc.total_available_cash_usd(_now=1000.0 + 60) == 500.0  # within TTL
    assert calls["n"] == 1  # second call served from cache


# --- 3-tier resolver ---------------------------------------------------------


def test_resolver_prefers_mercury(monkeypatch):
    monkeypatch.setattr("backend.finance.mercury_client.total_available_cash_usd", lambda: 5000.0)
    from backend.finance.found_cash import best_available_cash_usd

    assert best_available_cash_usd(0.0) == (5000.0, "mercury")


def test_resolver_falls_back_to_found_then_stripe(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "backend.finance.mercury_client.total_available_cash_usd", lambda: None
    )  # mercury off
    # no Found dir configured -> found returns None -> stripe
    monkeypatch.delenv("SAMUS_FOUND_ACTIVITY_DIR", raising=False)
    from backend.finance.found_cash import best_available_cash_usd

    assert best_available_cash_usd(42.0) == (42.0, "stripe_available")
    # with a Found export present -> found tier
    (tmp_path / "hustleforge_llc_activity_report_1.csv").write_text(
        "Date,Description,Amount\n07/02/2026,x,100.0\n", encoding="utf-8"
    )
    monkeypatch.setenv("SAMUS_FOUND_ACTIVITY_DIR", str(tmp_path))
    usd, src = best_available_cash_usd(42.0)
    assert usd == pytest.approx(100.0) and src == "found_bank"
