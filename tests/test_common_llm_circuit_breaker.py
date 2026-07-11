"""Per-workcell circuit breaker (Control C, token-cost-hardening 2026-05-18).

Tests the new ``consecutive_errors`` + ``circuit_open_until`` fields on
``WorkcellBudget`` and the deny path in ``can_spend``. Deterministic
time via the ``now_func`` injection on ``LlmBudgetStore``.
"""
from __future__ import annotations

import calendar
import time

import pytest

from backend.common.llm_budget import LlmBudgetStore, WorkcellBudget


def _fixed_now(ts: float):
    holder = {"t": ts}

    def _n() -> float:
        return holder["t"]

    _n.advance = lambda dt: holder.__setitem__("t", holder["t"] + dt)
    return _n


def _store(tmp_path, *, threshold: int = 10, cooldown: int = 300,
           now=None) -> LlmBudgetStore:
    return LlmBudgetStore(
        base_token_budget=100_000,
        ema_alpha=0.5,
        floor_pct=0.10,
        ddb_table=None,
        json_path=str(tmp_path / "b.json"),
        circuit_breaker_threshold=threshold,
        circuit_breaker_cooldown_sec=cooldown,
        now_func=now,
    )


# ---------------------------------------------------------------------------
# Field defaults — backwards compat with pre-hardening DDB rows
# ---------------------------------------------------------------------------

def test_workcell_budget_defaults_zero_consecutive_errors():
    b = WorkcellBudget(workcell="prospecting")
    assert b.consecutive_errors == 0
    assert b.circuit_open_until == ""


def test_workcell_budget_from_item_missing_fields_defaults():
    """A row written by pre-hardening code (no consecutive_errors,
    no circuit_open_until) must load with defaults, not raise.
    """
    item = {
        "workcell": "prospecting",
        "bucket_day": "2026-05-18",
        "input_tokens_today": 100,
        "output_tokens_today": 50,
        # explicitly NOT including consecutive_errors / circuit_open_until
    }
    b = WorkcellBudget.from_item(item)
    assert b.consecutive_errors == 0
    assert b.circuit_open_until == ""


# ---------------------------------------------------------------------------
# Trip mechanics
# ---------------------------------------------------------------------------

def test_below_threshold_does_not_trip(tmp_path):
    s = _store(tmp_path, threshold=3)
    s.record_spend("prospecting", input_tokens=0, output_tokens=0, outcome="error")
    s.record_spend("prospecting", input_tokens=0, output_tokens=0, outcome="error")
    b = s.snapshot("prospecting")
    assert b.consecutive_errors == 2
    assert b.circuit_open_until == ""


def test_reaching_threshold_trips_breaker(tmp_path):
    """3 consecutive errors at threshold=3 must trip."""
    now = _fixed_now(1_700_000_000.0)  # arbitrary
    s = _store(tmp_path, threshold=3, cooldown=300, now=now)
    for _ in range(3):
        s.record_spend("prospecting", input_tokens=0, output_tokens=0, outcome="error")
    b = s.snapshot("prospecting")
    assert b.consecutive_errors == 3
    assert b.circuit_open_until != ""


def test_tripped_breaker_denies_can_spend(tmp_path):
    now = _fixed_now(1_700_000_000.0)
    s = _store(tmp_path, threshold=2, cooldown=300, now=now)
    s.record_spend("prospecting", input_tokens=0, output_tokens=0, outcome="error")
    s.record_spend("prospecting", input_tokens=0, output_tokens=0, outcome="error")
    d = s.can_spend("prospecting", est_tokens=100)
    assert d.allowed is False
    assert "circuit_open_until_" in (d.reason or "")


def test_cooldown_lapse_re_allows(tmp_path):
    """After cooldown_sec passes, can_spend allows again."""
    now = _fixed_now(1_700_000_000.0)
    s = _store(tmp_path, threshold=2, cooldown=300, now=now)
    s.record_spend("prospecting", input_tokens=0, output_tokens=0, outcome="error")
    s.record_spend("prospecting", input_tokens=0, output_tokens=0, outcome="error")
    # Move past the cooldown.
    now.advance(301)
    d = s.can_spend("prospecting", est_tokens=100)
    assert d.allowed is True


# ---------------------------------------------------------------------------
# Reset on success / failure
# ---------------------------------------------------------------------------

def test_success_resets_consecutive_errors(tmp_path):
    s = _store(tmp_path, threshold=10)
    for _ in range(5):
        s.record_spend("prospecting", input_tokens=0, output_tokens=0, outcome="error")
    s.record_spend("prospecting", input_tokens=10, output_tokens=10, outcome="success")
    b = s.snapshot("prospecting")
    assert b.consecutive_errors == 0
    assert b.circuit_open_until == ""


def test_failure_also_resets_consecutive_errors(tmp_path):
    """A task-level failure (model answered, answer was wrong) clears the
    breaker because the infra is healthy — Control C only tracks infra
    faults, not task quality.
    """
    s = _store(tmp_path, threshold=10)
    for _ in range(5):
        s.record_spend("prospecting", input_tokens=0, output_tokens=0, outcome="error")
    s.record_spend("prospecting", input_tokens=10, output_tokens=10, outcome="failure")
    b = s.snapshot("prospecting")
    assert b.consecutive_errors == 0
    assert b.circuit_open_until == ""


def test_success_after_trip_closes_breaker(tmp_path):
    """Even after the breaker tripped, a single success closes it."""
    now = _fixed_now(1_700_000_000.0)
    s = _store(tmp_path, threshold=2, cooldown=300, now=now)
    s.record_spend("prospecting", input_tokens=0, output_tokens=0, outcome="error")
    s.record_spend("prospecting", input_tokens=0, output_tokens=0, outcome="error")
    assert s.snapshot("prospecting").circuit_open_until != ""
    s.record_spend("prospecting", input_tokens=10, output_tokens=10, outcome="success")
    b = s.snapshot("prospecting")
    assert b.circuit_open_until == ""
    assert b.consecutive_errors == 0
    # And next can_spend allows again.
    d = s.can_spend("prospecting", est_tokens=100)
    assert d.allowed is True


# ---------------------------------------------------------------------------
# Per-workcell isolation
# ---------------------------------------------------------------------------

def test_workcells_have_independent_breakers(tmp_path):
    """Tripping workcell A must not affect workcell B."""
    now = _fixed_now(1_700_000_000.0)
    s = _store(tmp_path, threshold=2, cooldown=300, now=now)
    for _ in range(2):
        s.record_spend("prospecting", input_tokens=0, output_tokens=0, outcome="error")
    assert s.can_spend("prospecting", est_tokens=100).allowed is False
    assert s.can_spend("seo", est_tokens=100).allowed is True


# ---------------------------------------------------------------------------
# Constructor + edge cases
# ---------------------------------------------------------------------------

def test_constructor_rejects_zero_threshold(tmp_path):
    with pytest.raises(ValueError):
        LlmBudgetStore(
            base_token_budget=1_000,
            ema_alpha=0.5, floor_pct=0.10,
            ddb_table=None, json_path=str(tmp_path / "b.json"),
            circuit_breaker_threshold=0,
        )


def test_constructor_rejects_negative_cooldown(tmp_path):
    with pytest.raises(ValueError):
        LlmBudgetStore(
            base_token_budget=1_000,
            ema_alpha=0.5, floor_pct=0.10,
            ddb_table=None, json_path=str(tmp_path / "b.json"),
            circuit_breaker_cooldown_sec=-1,
        )


def test_malformed_circuit_open_until_string_treated_as_closed(tmp_path):
    """If the persisted ISO timestamp is corrupt, treat the breaker as closed
    (otherwise a one-time write glitch could keep a workcell offline forever).
    """
    s = _store(tmp_path, threshold=10)
    # Hand-craft a row with garbage in circuit_open_until.
    b = WorkcellBudget(
        workcell="prospecting",
        bucket_day=time.strftime("%Y-%m-%d", time.gmtime()),
        circuit_open_until="not-a-date",
    )
    s._save(b)
    # Force fresh load by busting the cache.
    s._cache.clear()
    d = s.can_spend("prospecting", est_tokens=100)
    assert d.allowed is True


def test_circuit_open_until_is_iso_format(tmp_path):
    now = _fixed_now(1_700_000_000.0)  # 2023-11-14 22:13:20 UTC
    s = _store(tmp_path, threshold=1, cooldown=300, now=now)
    s.record_spend("prospecting", input_tokens=0, output_tokens=0, outcome="error")
    b = s.snapshot("prospecting")
    # Parseable back to an epoch via calendar.timegm.
    parsed = calendar.timegm(time.strptime(b.circuit_open_until, "%Y-%m-%dT%H:%M:%SZ"))
    assert parsed == int(1_700_000_000.0 + 300)
