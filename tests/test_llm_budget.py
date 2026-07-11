"""Per-workcell LLM token budget store + adaptive quota math."""

from __future__ import annotations


import pytest

from backend.common.llm_budget import (
    LlmBudgetStore,
    compute_quota,
)


# ---------------------------------------------------------------------------
# Pure quota math (no I/O)
# ---------------------------------------------------------------------------


def test_compute_quota_returns_base_when_insufficient_signal():
    """First 10 calls -> full base quota (no adaptive scaling yet)."""
    q = compute_quota(100_000, efficiency_ema=0.0, efficiency_call_count=3)
    assert q == 100_000  # < min_calls_for_adaptive


def test_compute_quota_100pct_efficiency_doubles_base():
    q = compute_quota(100_000, efficiency_ema=1.0, efficiency_call_count=100)
    assert q == 200_000  # factor = 0.5 + 1.5 * 1.0 = 2.0


def test_compute_quota_0pct_efficiency_halves_base():
    q = compute_quota(
        100_000,
        efficiency_ema=0.0,
        efficiency_call_count=100,
        floor_pct=0.10,
    )
    # factor = 0.5; 100_000 * 0.5 = 50_000. Floor is 10_000. max wins.
    assert q == 50_000


def test_compute_quota_floor_kicks_in_only_below_floor():
    """If efficiency drops past where factor would go below floor_pct, floor wins."""
    # base=100, floor_pct=0.30 -> floor=30. factor at ema=0 is 0.5 -> 50. floor doesn't bind.
    assert compute_quota(100, efficiency_ema=0.0, efficiency_call_count=100, floor_pct=0.30) == 50
    # base=100, floor_pct=0.80 -> floor=80. factor at ema=0 is 50. floor binds.
    assert compute_quota(100, efficiency_ema=0.0, efficiency_call_count=100, floor_pct=0.80) == 80


def test_compute_quota_clamps_invalid_ema_to_range():
    """EMA above 1.0 or below 0.0 (numerical drift) gets clamped."""
    high = compute_quota(100_000, efficiency_ema=1.5, efficiency_call_count=100)
    low = compute_quota(100_000, efficiency_ema=-0.5, efficiency_call_count=100)
    assert high == 200_000
    assert low == 50_000


# ---------------------------------------------------------------------------
# LlmBudgetStore — JSON backend (no DDB)
# ---------------------------------------------------------------------------


def _store(tmp_path, **overrides) -> LlmBudgetStore:
    """Build a store backed only by a tmp JSON file (no DDB)."""
    kwargs = dict(
        base_token_budget=100_000,
        ema_alpha=0.5,  # high alpha for fast test feedback
        floor_pct=0.10,
        ddb_table=None,
        json_path=str(tmp_path / "budget.json"),
    )
    kwargs.update(overrides)
    return LlmBudgetStore(**kwargs)


def test_store_starts_empty_with_default_ema(tmp_path):
    s = _store(tmp_path)
    b = s.snapshot("prospecting")
    assert b.workcell == "prospecting"
    assert b.efficiency_ema == 1.0  # benefit of the doubt
    assert b.total_tokens_today == 0


def test_can_spend_allows_when_within_quota(tmp_path):
    s = _store(tmp_path)
    d = s.can_spend("prospecting", est_tokens=10_000)
    assert d.allowed is True
    assert d.quota == 100_000


def test_can_spend_denies_when_over_quota(tmp_path):
    s = _store(tmp_path, base_token_budget=1_000)
    # Use up almost the whole quota
    s.record_spend("prospecting", input_tokens=400, output_tokens=400, outcome="success")
    d = s.can_spend("prospecting", est_tokens=500)
    assert d.allowed is False
    assert "budget_exceeded" in (d.reason or "")
    assert d.used == 800
    assert d.requested == 500


def test_record_spend_increments_counters_and_ema(tmp_path):
    s = _store(tmp_path, ema_alpha=0.5)
    s.record_spend("prospecting", input_tokens=100, output_tokens=50, outcome="success")
    b = s.snapshot("prospecting")
    assert b.input_tokens_today == 100
    assert b.output_tokens_today == 50
    assert b.total_tokens_today == 150
    assert b.call_count_today == 1
    assert b.success_count_today == 1
    # EMA: alpha=0.5, start=1.0, success -> (1-0.5)*1.0 + 0.5*1.0 = 1.0
    assert b.efficiency_ema == pytest.approx(1.0)


def test_failure_pulls_ema_down(tmp_path):
    s = _store(tmp_path, ema_alpha=0.5)
    # Three failures from start: 1.0 -> 0.5 -> 0.25 -> 0.125
    for _ in range(3):
        s.record_spend("prospecting", input_tokens=10, output_tokens=10, outcome="failure")
    b = s.snapshot("prospecting")
    assert b.failure_count_today == 3
    assert b.efficiency_ema == pytest.approx(0.125)


def test_error_outcome_does_not_affect_ema(tmp_path):
    """LLM-call errors are transient; they must NOT punish future quota."""
    s = _store(tmp_path, ema_alpha=0.5)
    # 5 errors
    for _ in range(5):
        s.record_spend("prospecting", input_tokens=0, output_tokens=0, outcome="error")
    b = s.snapshot("prospecting")
    assert b.error_count_today == 5
    assert b.efficiency_call_count == 0  # error doesn't increment this
    assert b.efficiency_ema == pytest.approx(1.0)  # untouched


def test_invalid_outcome_raises(tmp_path):
    s = _store(tmp_path)
    with pytest.raises(ValueError):
        s.record_spend("prospecting", input_tokens=0, output_tokens=0, outcome="weird")


def test_quota_shrinks_after_enough_failures(tmp_path):
    s = _store(tmp_path, base_token_budget=1_000, ema_alpha=0.5)
    # 15 failures — past the 10-call threshold for adaptive scaling
    for _ in range(15):
        s.record_spend("prospecting", input_tokens=1, output_tokens=1, outcome="failure")
    # EMA after 15 failures from 1.0 with alpha=0.5: 0.5^15 ≈ 0.000031, essentially 0
    d = s.can_spend("prospecting", est_tokens=1)
    # factor = 0.5 + 1.5 * ~0 = ~0.5 -> 500 quota
    # used = 30 tokens
    # 30 + 1 = 31 < 500 -> still allowed, but quota shrunk
    assert d.allowed is True
    assert d.quota < 1_000  # adaptive scaling kicked in
    assert d.quota >= int(1_000 * 0.10)  # floor respected


def test_floor_never_zero_even_after_perfect_failure(tmp_path):
    s = _store(tmp_path, base_token_budget=100, ema_alpha=0.5, floor_pct=0.10)
    for _ in range(50):
        s.record_spend("prospecting", input_tokens=0, output_tokens=0, outcome="failure")
    d = s.can_spend("prospecting", est_tokens=1)
    assert d.quota >= 10  # base * floor_pct


def test_per_workcell_isolation(tmp_path):
    """Two workcells must not share budget state."""
    s = _store(tmp_path)
    s.record_spend("prospecting", input_tokens=500, output_tokens=500, outcome="success")
    b_p = s.snapshot("prospecting")
    b_s = s.snapshot("seo")
    assert b_p.total_tokens_today == 1000
    assert b_s.total_tokens_today == 0


def test_json_persistence_round_trip(tmp_path):
    """Store state must survive a fresh store pointed at the same JSON file."""
    path = str(tmp_path / "budget.json")
    s1 = LlmBudgetStore(
        base_token_budget=1_000,
        ema_alpha=0.5,
        floor_pct=0.10,
        ddb_table=None,
        json_path=path,
    )
    s1.record_spend("prospecting", input_tokens=200, output_tokens=100, outcome="success")
    s2 = LlmBudgetStore(
        base_token_budget=1_000,
        ema_alpha=0.5,
        floor_pct=0.10,
        ddb_table=None,
        json_path=path,
    )
    b = s2.snapshot("prospecting")
    assert b.total_tokens_today == 300
    assert b.success_count_today == 1


def test_constructor_rejects_bad_ema_alpha(tmp_path):
    with pytest.raises(ValueError):
        LlmBudgetStore(
            base_token_budget=1_000,
            ema_alpha=0.0,
            floor_pct=0.10,
            ddb_table=None,
            json_path=str(tmp_path / "b.json"),
        )
    with pytest.raises(ValueError):
        LlmBudgetStore(
            base_token_budget=1_000,
            ema_alpha=1.5,
            floor_pct=0.10,
            ddb_table=None,
            json_path=str(tmp_path / "b.json"),
        )


def test_constructor_rejects_bad_floor_pct(tmp_path):
    with pytest.raises(ValueError):
        LlmBudgetStore(
            base_token_budget=1_000,
            ema_alpha=0.5,
            floor_pct=-0.1,
            ddb_table=None,
            json_path=str(tmp_path / "b.json"),
        )
    with pytest.raises(ValueError):
        LlmBudgetStore(
            base_token_budget=1_000,
            ema_alpha=0.5,
            floor_pct=1.5,
            ddb_table=None,
            json_path=str(tmp_path / "b.json"),
        )


def test_store_unavailable_still_allows_calls(tmp_path, monkeypatch):
    """Catastrophic store failure must not block work."""
    s = _store(tmp_path)

    # Force load to raise
    def _explode(_):
        raise RuntimeError("backend gone")

    monkeypatch.setattr(s, "_load", _explode)
    d = s.can_spend("prospecting", est_tokens=999)
    assert d.allowed is True
    assert "store_unavailable" in (d.reason or "")
