"""Dynamic LLM budget scaler — MRR-proportional daily cap."""

from __future__ import annotations

import pytest

from backend.common import llm_budget_scaler as scaler


@pytest.fixture(autouse=True)
def _clean_cache():
    scaler.reset_cache()
    yield
    scaler.reset_cache()


# ---------------------------------------------------------------------------
# compute_daily_cap formula
# ---------------------------------------------------------------------------


def test_zero_mrr_returns_floor(monkeypatch):
    monkeypatch.setattr(scaler, "_fetch_mrr", lambda: 0.0)
    cap = scaler.compute_daily_cap()
    assert cap == scaler.FLOOR_USD


def test_moderate_mrr_scales_linearly(monkeypatch):
    monkeypatch.setattr(scaler, "_fetch_mrr", lambda: 1000.0)
    cap = scaler.compute_daily_cap()
    expected = 1000.0 * scaler.REINVEST_PCT / 30.0
    assert cap == pytest.approx(expected, abs=0.01)


def test_high_mrr_capped_at_ceiling(monkeypatch):
    monkeypatch.setattr(scaler, "_fetch_mrr", lambda: 100_000.0)
    cap = scaler.compute_daily_cap()
    assert cap == scaler.CEILING_USD


def test_mrr_just_above_floor(monkeypatch):
    threshold_mrr = scaler.FLOOR_USD * 30.0 / scaler.REINVEST_PCT
    monkeypatch.setattr(scaler, "_fetch_mrr", lambda: threshold_mrr + 1.0)
    cap = scaler.compute_daily_cap()
    assert cap > scaler.FLOOR_USD


# ---------------------------------------------------------------------------
# Cache behavior
# ---------------------------------------------------------------------------


def test_cache_prevents_repeated_fetch(monkeypatch):
    calls = {"n": 0}

    def _counting_fetch():
        calls["n"] += 1
        return 500.0

    monkeypatch.setattr(scaler, "_fetch_mrr", _counting_fetch)
    scaler.compute_daily_cap()
    scaler.compute_daily_cap()
    scaler.compute_daily_cap()
    assert calls["n"] == 1


def test_stripe_failure_retains_last_value(monkeypatch):
    monkeypatch.setattr(scaler, "_fetch_mrr", lambda: 2000.0)
    first = scaler.compute_daily_cap()

    scaler.reset_cache()
    monkeypatch.setattr(scaler, "_fetch_mrr", lambda: None)

    scaler._cached_mrr = 2000.0
    scaler._cached_at = 0.0
    cap = scaler.compute_daily_cap()
    assert cap == first


def test_cold_start_no_stripe_returns_floor(monkeypatch):
    monkeypatch.setattr(scaler, "_fetch_mrr", lambda: None)
    cap = scaler.compute_daily_cap()
    assert cap == scaler.FLOOR_USD


# ---------------------------------------------------------------------------
# Integration with LlmGlobalBudgetStore
# ---------------------------------------------------------------------------


def test_global_store_uses_dynamic_cap(tmp_path, monkeypatch):
    from backend.common.llm_global_budget import LlmGlobalBudgetStore

    monkeypatch.setattr(scaler, "_fetch_mrr", lambda: 3000.0)
    store = LlmGlobalBudgetStore(
        daily_dollar_cap=1.0,
        ddb_table=None,
        json_path=str(tmp_path / "g.json"),
        dynamic_cap=True,
    )
    expected = 3000.0 * scaler.REINVEST_PCT / 30.0
    assert store.daily_dollar_cap == pytest.approx(expected, abs=0.01)


def test_global_store_static_cap_when_dynamic_off(tmp_path):
    from backend.common.llm_global_budget import LlmGlobalBudgetStore

    store = LlmGlobalBudgetStore(
        daily_dollar_cap=1.0,
        ddb_table=None,
        json_path=str(tmp_path / "g.json"),
        dynamic_cap=False,
    )
    assert store.daily_dollar_cap == 1.0
