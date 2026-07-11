"""LlmGlobalBudgetStore — cross-workcell $-cap (Control A).

token-cost-hardening 2026-05-18. Mock-only: no DDB, JSON backend only.
Deterministic time via the injectable ``now_func`` so daily-reset
behaviour can be tested without sleep / freezegun.
"""
from __future__ import annotations

import time

import pytest

from backend.common.llm_global_budget import (
    GlobalBudget,
    LlmGlobalBudgetStore,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _store(tmp_path, *, cap: float = 25.0, now=None) -> LlmGlobalBudgetStore:
    return LlmGlobalBudgetStore(
        daily_dollar_cap=cap,
        ddb_table=None,
        json_path=str(tmp_path / "global.json"),
        now_func=now,
    )


def _fixed_now(ts: float):
    """Return a callable that always returns the same epoch seconds."""
    holder = {"t": ts}

    def _n() -> float:
        return holder["t"]

    _n.advance = lambda dt: holder.__setitem__("t", holder["t"] + dt)
    return _n


# ---------------------------------------------------------------------------
# can_spend_global — pre-flight estimate vs cap
# ---------------------------------------------------------------------------

def test_can_spend_global_allows_first_call(tmp_path):
    s = _store(tmp_path, cap=25.0)
    d = s.can_spend_global(
        "claude-haiku-4-5", est_input_tokens=1000, est_output_tokens=500,
    )
    assert d.allowed is True
    assert d.cap_usd == 25.0
    assert d.used_usd == 0.0


def test_can_spend_global_denies_when_over_cap(tmp_path):
    # Cap = $0.01 — anything substantial trips it.
    s = _store(tmp_path, cap=0.01)
    # 1M input tokens on Haiku = $0.80
    d = s.can_spend_global(
        "claude-haiku-4-5",
        est_input_tokens=1_000_000, est_output_tokens=0,
    )
    assert d.allowed is False
    assert "global_cap_exceeded" in (d.reason or "")
    assert d.estimated_usd == pytest.approx(0.80)
    assert d.cap_usd == 0.01


def test_can_spend_global_with_unknown_model_allows_and_logs(tmp_path, caplog):
    """Defense in depth: unknown model id => allow but log (don't break all calls)."""
    s = _store(tmp_path, cap=25.0)
    d = s.can_spend_global("gpt-5", est_input_tokens=1000, est_output_tokens=100)
    assert d.allowed is True
    assert "unknown_model_pricing" in (d.reason or "")


# ---------------------------------------------------------------------------
# record_spend_global — adds $ to today's row
# ---------------------------------------------------------------------------

def test_record_spend_global_accumulates_dollars(tmp_path):
    s = _store(tmp_path, cap=25.0)
    # 1M input on Haiku = $0.80
    s.record_spend_global("claude-haiku-4-5", actual_input=1_000_000, actual_output=0)
    snap = s.snapshot()
    assert snap.dollars_today == pytest.approx(0.80)
    assert snap.call_count_today == 1


def test_record_spend_global_two_calls_sum(tmp_path):
    s = _store(tmp_path, cap=25.0)
    s.record_spend_global("claude-haiku-4-5", actual_input=500_000, actual_output=0)  # $0.40
    s.record_spend_global("claude-haiku-4-5", actual_input=500_000, actual_output=0)  # $0.40
    snap = s.snapshot()
    assert snap.dollars_today == pytest.approx(0.80)
    assert snap.call_count_today == 2


def test_record_spend_global_unknown_model_skipped(tmp_path):
    """Unknown model id during record => skip (don't crash, don't charge)."""
    s = _store(tmp_path, cap=25.0)
    s.record_spend_global("gpt-5", actual_input=100, actual_output=100)
    snap = s.snapshot()
    assert snap.dollars_today == 0.0
    assert snap.call_count_today == 0


def test_record_spend_global_with_cache_tokens(tmp_path):
    """Cache write + read tokens contribute to $ alongside input/output."""
    s = _store(tmp_path, cap=25.0)
    # 1M cache_write Haiku = $1.00; 1M cache_read = $0.08
    s.record_spend_global(
        "claude-haiku-4-5",
        actual_input=0, actual_output=0,
        cache_write_tokens=1_000_000,
        cache_read_tokens=1_000_000,
    )
    snap = s.snapshot()
    assert snap.dollars_today == pytest.approx(1.08)


# ---------------------------------------------------------------------------
# Cap enforcement after spending
# ---------------------------------------------------------------------------

def test_cap_breached_after_record_blocks_next_call(tmp_path):
    """Spend right up to cap, next call must be denied."""
    s = _store(tmp_path, cap=1.00)
    # Spend $0.80
    s.record_spend_global("claude-haiku-4-5", actual_input=1_000_000, actual_output=0)
    # Next call estimating $0.40 (500k input on Haiku) would total $1.20 > $1.00
    d = s.can_spend_global(
        "claude-haiku-4-5", est_input_tokens=500_000, est_output_tokens=0,
    )
    assert d.allowed is False
    assert d.used_usd == pytest.approx(0.80)


def test_under_cap_still_allows(tmp_path):
    s = _store(tmp_path, cap=1.00)
    # Spend $0.10
    s.record_spend_global("claude-haiku-4-5", actual_input=125_000, actual_output=0)
    d = s.can_spend_global(
        "claude-haiku-4-5", est_input_tokens=100_000, est_output_tokens=0,
    )
    assert d.allowed is True


# ---------------------------------------------------------------------------
# Daily reset (deterministic via now_func injection)
# ---------------------------------------------------------------------------

def test_daily_reset_zeroes_dollars_on_new_day(tmp_path):
    """Roll the clock 24h forward — dollars_today must reset."""
    # 2026-05-18 12:00 UTC
    now = _fixed_now(time.mktime(time.strptime("2026-05-18 12:00:00 UTC",
                                               "%Y-%m-%d %H:%M:%S %Z")))
    s = _store(tmp_path, cap=25.0, now=now)
    s.record_spend_global("claude-haiku-4-5", actual_input=1_000_000, actual_output=0)
    assert s.snapshot().dollars_today == pytest.approx(0.80)
    # Advance 25 hours -> next UTC day. Bust the 30s read cache too.
    now.advance(25 * 3600)
    # Force fresh load: clear store cache by reading after TTL.
    snap = s.snapshot()
    assert snap.dollars_today == 0.0
    assert snap.call_count_today == 0


def test_same_day_no_reset(tmp_path):
    now = _fixed_now(time.mktime(time.strptime("2026-05-18 12:00:00 UTC",
                                               "%Y-%m-%d %H:%M:%S %Z")))
    s = _store(tmp_path, cap=25.0, now=now)
    s.record_spend_global("claude-haiku-4-5", actual_input=1_000_000, actual_output=0)
    # Advance 1 hour same day — but bust the 30s cache by going past TTL.
    now.advance(3600)
    snap = s.snapshot()
    assert snap.dollars_today == pytest.approx(0.80)


# ---------------------------------------------------------------------------
# Persistence round-trip
# ---------------------------------------------------------------------------

def test_json_round_trip(tmp_path):
    s1 = _store(tmp_path, cap=25.0)
    s1.record_spend_global("claude-haiku-4-5", actual_input=500_000, actual_output=0)
    # New store on the same JSON file — must see the prior spend.
    s2 = LlmGlobalBudgetStore(
        daily_dollar_cap=25.0,
        ddb_table=None,
        json_path=str(tmp_path / "global.json"),
    )
    assert s2.snapshot().dollars_today == pytest.approx(0.40)


# ---------------------------------------------------------------------------
# Allow-on-persistence-failure (brief's hard rule)
# ---------------------------------------------------------------------------

def test_store_failure_still_allows(tmp_path, monkeypatch):
    """If load() throws, can_spend_global must return allowed=True with reason."""
    s = _store(tmp_path, cap=25.0)
    def _explode():
        raise RuntimeError("ddb gone")
    monkeypatch.setattr(s, "_load", _explode)
    d = s.can_spend_global(
        "claude-haiku-4-5", est_input_tokens=100, est_output_tokens=100,
    )
    assert d.allowed is True
    assert "store_unavailable" in (d.reason or "")


# ---------------------------------------------------------------------------
# Constructor validation
# ---------------------------------------------------------------------------

def test_constructor_rejects_negative_cap(tmp_path):
    with pytest.raises(ValueError):
        LlmGlobalBudgetStore(
            daily_dollar_cap=-1.0,
            ddb_table=None,
            json_path=str(tmp_path / "g.json"),
        )


def test_default_cap_one_dollar():
    """Samus production default: $1/day.

    Lever 1.1 (token-cost-hardening port to feat/samus-lever-1-hardening,
    2026-05-19) tightened the original 29ba2df brief default of $25/day
    down to $1/day to match the operator-chosen production posture in
    memory:project_samus_llm_token_policy. The test intent (verify the
    documented production default) is preserved — only the dollar figure
    moves.
    """
    from backend.common.config import Settings
    s = Settings()
    assert s.llm_global_daily_dollar_cap == 1.0
