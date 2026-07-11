"""Tests for backend.strategy.triggers (Lever 3.2 — event-driven portfolio re-plan).

Five trigger conditions × two directions (fires / does not fire) + short-circuit
ordering + operator rate-limit + audit ledger + JSON-fallback snapshot
persistence. LLM calls are stubbed via the ``propose_fn`` injection point —
no real ``anthropic_messages`` traffic.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from backend.strategy import triggers as trig
from backend.strategy.portfolio_manager import AllocationDecision


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_decision(parse_error: bool = False) -> AllocationDecision:
    """Build a non-empty AllocationDecision so budget_denied stays False."""
    return AllocationDecision(
        priorities=["p1"],
        deprioritize=[],
        actions=[{"type": "accelerate", "prospect_id": "p1"}],
        raw_text='{"priorities":["p1"]}',
        parse_error=parse_error,
    )


def _stub_propose(call_log: list[tuple]) -> "callable":
    """Return a propose_allocation stub that records each call."""
    def _propose(state, market_signals, *, api_key=None):
        call_log.append((state, market_signals, api_key))
        return _make_decision()
    return _propose


def _ctx(
    *,
    prospects=None,
    bandit_stats=None,
    efficiency_ema_by_workcell=None,
) -> trig.TriggerContext:
    return trig.TriggerContext(
        prospects=list(prospects or []),
        bandit_stats=dict(bandit_stats or {}),
        efficiency_ema_by_workcell=dict(efficiency_ema_by_workcell or {}),
        market_signals={},
        budget_remaining=1_000.0,
        avg_conversion_rate=0.1,
        pipeline_value=5_000.0,
    )


def _store(tmp_path: Path) -> trig.SnapshotStore:
    """Build a JSON-only store rooted at tmp_path (no DDB)."""
    return trig.SnapshotStore(
        ddb_table="",  # disable DDB path
        region="us-west-1",
        json_path=str(tmp_path / "snapshots.json"),
    )


@pytest.fixture(autouse=True)
def _isolate_module_state(tmp_path, monkeypatch):
    """Reset module-level singletons + redirect ledger to tmp."""
    trig._MANUAL_SIGNAL.clear()
    trig._MANUAL_RATE_LIMITER.reset()
    trig.set_default_store(None)
    monkeypatch.setenv(
        "SAMUS_PORTFOLIO_TRIGGER_LEDGER", str(tmp_path / "triggers.jsonl"),
    )
    monkeypatch.setenv(
        "SAMUS_PORTFOLIO_SNAPSHOT_PATH", str(tmp_path / "snapshots.json"),
    )
    yield
    trig._MANUAL_SIGNAL.clear()
    trig._MANUAL_RATE_LIMITER.reset()
    trig.set_default_store(None)


# ---------------------------------------------------------------------------
# 1. pipeline_ev_step — fires + does not fire
# ---------------------------------------------------------------------------

def test_pipeline_ev_step_fires_on_large_delta(tmp_path):
    store = _store(tmp_path)
    # Seed prev snapshot with EV 1000
    prev = trig.PortfolioSnapshot(
        bucket_day="2026-05-19", ts="2026-05-19T00:00:00Z",
        ev_total=1000.0, prospect_count=1, pipeline_median_score=50.0,
    )
    store.save(prev)
    log: list[tuple] = []
    # 20% jump (>= 15% threshold)
    ctx = _ctx(prospects=[{"prospect_id": "p1", "lead_score": 100.0, "seo_score": 50.0, "conversion_signals": ["email_open"]}])
    # _score_opportunity: 100*0.6 + 1*5 + (100-50)*0.2 = 60+5+10 = 75
    # That's a -92.5% drop from 1000 to 75 -> well above 15% threshold (absolute delta).
    event = trig.check_and_fire(
        ctx, store=store, min_signal_change=0.15, propose_fn=_stub_propose(log),
    )
    assert event is not None
    assert event.name == "pipeline_ev_step"
    assert len(log) == 1


def test_pipeline_ev_step_does_not_fire_on_small_delta(tmp_path):
    store = _store(tmp_path)
    # Seed prev snapshot with EV that closely matches the next one
    # _score_opportunity for {"lead":100,"seo":50,"sig":[email_open]} = 75
    prev = trig.PortfolioSnapshot(
        bucket_day="2026-05-19", ts="2026-05-19T00:00:00Z",
        ev_total=80.0, prospect_count=1, pipeline_median_score=100.0,
        prospect_ids=["p1"],  # same prospect -> no new_cohort
    )
    store.save(prev)
    log: list[tuple] = []
    ctx = _ctx(prospects=[
        {"prospect_id": "p1", "lead_score": 100.0, "seo_score": 50.0, "conversion_signals": ["email_open"]},
    ])
    # cur EV = 75, prev = 80, delta = 5 -> 6.25% < 15%, does not fire
    event = trig.check_and_fire(
        ctx, store=store, min_signal_change=0.15, propose_fn=_stub_propose(log),
    )
    assert event is None
    assert log == []


# ---------------------------------------------------------------------------
# 2. bandit_divergence — fires + does not fire
# ---------------------------------------------------------------------------

def test_bandit_divergence_fires_when_top_arm_changes(tmp_path):
    store = _store(tmp_path)
    # Seed prev with one top-arm; current stats have a different winner.
    prev = trig.PortfolioSnapshot(
        bucket_day="2026-05-19", ts="2026-05-19T00:00:00Z",
        ev_total=100.0, prospect_count=1, pipeline_median_score=50.0,
        bandit_top_arms={"_default": "accelerate"},
        prospect_ids=["p1"],
    )
    store.save(prev)
    log: list[tuple] = []
    ctx = _ctx(
        prospects=[{"prospect_id": "p1", "lead_score": 80.0, "seo_score": 70.0, "conversion_signals": []}],
        bandit_stats={
            "accelerate": {"wins": 1.0, "trials": 10},
            "defer": {"wins": 9.0, "trials": 10},  # higher mean -> new top
        },
    )
    # Configure thresholds so pipeline_ev_step doesn't beat us to it:
    # cur EV = 80*0.6 + 0 + (100-70)*0.2 = 48+6 = 54; prev = 100; delta 46 = 46% (>15%) -> fires first!
    # So we need to pick prev_ev close to cur ev.
    prev2 = trig.PortfolioSnapshot(
        bucket_day="2026-05-19", ts="2026-05-19T00:00:00Z",
        ev_total=55.0, prospect_count=1, pipeline_median_score=50.0,
        bandit_top_arms={"_default": "accelerate"},
        prospect_ids=["p1"],
    )
    store.save(prev2)

    event = trig.check_and_fire(
        ctx, store=store, min_signal_change=0.15, propose_fn=_stub_propose(log),
    )
    assert event is not None
    assert event.name == "bandit_divergence"
    assert len(log) == 1
    assert event.detail["previous_top_arms"] == {"_default": "accelerate"}
    assert event.detail["current_top_arms"] == {"_default": "defer"}


def test_bandit_divergence_does_not_fire_when_top_arm_stable(tmp_path):
    store = _store(tmp_path)
    prev = trig.PortfolioSnapshot(
        bucket_day="2026-05-19", ts="2026-05-19T00:00:00Z",
        ev_total=55.0, prospect_count=1, pipeline_median_score=50.0,
        bandit_top_arms={"_default": "accelerate"},
        prospect_ids=["p1"],
    )
    store.save(prev)
    log: list[tuple] = []
    ctx = _ctx(
        prospects=[{"prospect_id": "p1", "lead_score": 80.0, "seo_score": 70.0, "conversion_signals": []}],
        bandit_stats={
            "accelerate": {"wins": 9.0, "trials": 10},
            "defer": {"wins": 1.0, "trials": 10},  # accelerate still wins
        },
    )
    # ev: cur = 54, prev = 55 -> ~2% < 15% threshold
    event = trig.check_and_fire(
        ctx, store=store, min_signal_change=0.15, propose_fn=_stub_propose(log),
    )
    assert event is None
    assert log == []


# ---------------------------------------------------------------------------
# 3. new_cohort — fires + does not fire
# ---------------------------------------------------------------------------

def test_new_cohort_fires_when_new_prospects_exceed_median(tmp_path):
    store = _store(tmp_path)
    # Seed prev EV close to cur so pipeline_ev_step doesn't pre-empt.
    # cur prospects below: p1 (lead=80, seo=70, sigs=[]) -> 54
    # plus new p2 (lead=90, seo=70, sigs=[]) -> 60 EV. total = 114
    # Need prev_ev ~ 114 too (within 15%).
    prev = trig.PortfolioSnapshot(
        bucket_day="2026-05-19", ts="2026-05-19T00:00:00Z",
        ev_total=110.0, prospect_count=1, pipeline_median_score=80.0,
        bandit_top_arms={},
        prospect_ids=["p1"],
    )
    store.save(prev)
    log: list[tuple] = []
    ctx = _ctx(
        prospects=[
            {"prospect_id": "p1", "lead_score": 80.0, "seo_score": 70.0, "conversion_signals": []},
            {"prospect_id": "p2", "lead_score": 90.0, "seo_score": 70.0, "conversion_signals": []},
        ],
    )
    event = trig.check_and_fire(
        ctx, store=store, min_signal_change=0.15, propose_fn=_stub_propose(log),
    )
    assert event is not None
    assert event.name == "new_cohort"
    assert event.detail["new_prospect_count"] == 1
    assert len(log) == 1


def test_new_cohort_does_not_fire_when_no_new_prospects(tmp_path):
    store = _store(tmp_path)
    prev = trig.PortfolioSnapshot(
        bucket_day="2026-05-19", ts="2026-05-19T00:00:00Z",
        ev_total=55.0, prospect_count=1, pipeline_median_score=80.0,
        bandit_top_arms={},
        prospect_ids=["p1"],
    )
    store.save(prev)
    log: list[tuple] = []
    ctx = _ctx(
        prospects=[
            {"prospect_id": "p1", "lead_score": 80.0, "seo_score": 70.0, "conversion_signals": []},
        ],
    )
    event = trig.check_and_fire(
        ctx, store=store, min_signal_change=0.15, propose_fn=_stub_propose(log),
    )
    assert event is None
    assert log == []


# ---------------------------------------------------------------------------
# 4. operator_manual — fires + does not fire (rate limit)
# ---------------------------------------------------------------------------

def test_operator_manual_fires_on_signal(tmp_path):
    store = _store(tmp_path)
    prev = trig.PortfolioSnapshot(
        bucket_day="2026-05-19", ts="2026-05-19T00:00:00Z",
        ev_total=55.0, prospect_count=1, pipeline_median_score=80.0,
        bandit_top_arms={},
        prospect_ids=["p1"],
    )
    store.save(prev)
    log: list[tuple] = []
    trig.manual_signal()
    ctx = _ctx(
        prospects=[
            {"prospect_id": "p1", "lead_score": 80.0, "seo_score": 70.0, "conversion_signals": []},
        ],
    )
    event = trig.check_and_fire(
        ctx, store=store, min_signal_change=0.15, propose_fn=_stub_propose(log),
    )
    assert event is not None
    assert event.name == "operator_manual"
    assert len(log) == 1


def test_operator_manual_does_not_fire_without_signal(tmp_path):
    store = _store(tmp_path)
    prev = trig.PortfolioSnapshot(
        bucket_day="2026-05-19", ts="2026-05-19T00:00:00Z",
        ev_total=55.0, prospect_count=1, pipeline_median_score=80.0,
        bandit_top_arms={},
        prospect_ids=["p1"],
    )
    store.save(prev)
    log: list[tuple] = []
    ctx = _ctx(
        prospects=[
            {"prospect_id": "p1", "lead_score": 80.0, "seo_score": 70.0, "conversion_signals": []},
        ],
    )
    # No manual_signal() call here.
    event = trig.check_and_fire(
        ctx, store=store, min_signal_change=0.15, propose_fn=_stub_propose(log),
    )
    assert event is None
    assert log == []


# ---------------------------------------------------------------------------
# 5. budget_recovery — fires + does not fire
# ---------------------------------------------------------------------------

def test_budget_recovery_fires_on_low_efficiency(tmp_path):
    store = _store(tmp_path)
    prev = trig.PortfolioSnapshot(
        bucket_day="2026-05-19", ts="2026-05-19T00:00:00Z",
        ev_total=55.0, prospect_count=1, pipeline_median_score=80.0,
        bandit_top_arms={},
        prospect_ids=["p1"],
    )
    store.save(prev)
    log: list[tuple] = []
    ctx = _ctx(
        prospects=[
            {"prospect_id": "p1", "lead_score": 80.0, "seo_score": 70.0, "conversion_signals": []},
        ],
        efficiency_ema_by_workcell={"prospecting": 0.25, "seo": 0.8},
    )
    event = trig.check_and_fire(
        ctx, store=store, min_signal_change=0.15, propose_fn=_stub_propose(log),
    )
    assert event is not None
    assert event.name == "budget_recovery"
    assert event.detail["efficiency_ema"]["prospecting"] == 0.25
    assert len(log) == 1


def test_budget_recovery_does_not_fire_on_healthy_efficiency(tmp_path):
    store = _store(tmp_path)
    prev = trig.PortfolioSnapshot(
        bucket_day="2026-05-19", ts="2026-05-19T00:00:00Z",
        ev_total=55.0, prospect_count=1, pipeline_median_score=80.0,
        bandit_top_arms={},
        prospect_ids=["p1"],
    )
    store.save(prev)
    log: list[tuple] = []
    ctx = _ctx(
        prospects=[
            {"prospect_id": "p1", "lead_score": 80.0, "seo_score": 70.0, "conversion_signals": []},
        ],
        efficiency_ema_by_workcell={"prospecting": 0.9, "seo": 0.85},
    )
    event = trig.check_and_fire(
        ctx, store=store, min_signal_change=0.15, propose_fn=_stub_propose(log),
    )
    assert event is None
    assert log == []


# ---------------------------------------------------------------------------
# Short-circuit ordering — first true trigger wins, rest aren't evaluated
# ---------------------------------------------------------------------------

def test_check_and_fire_short_circuits_on_first_true(tmp_path):
    """If pipeline_ev_step fires, bandit_divergence + downstream don't run.

    We arrange a state where multiple triggers would individually fire, then
    assert the event is the FIRST one in walk order (pipeline_ev_step) AND
    the LLM was called exactly once (not five times).
    """
    store = _store(tmp_path)
    # Prev: huge EV, single prospect, accelerate-top, prospecting healthy
    prev = trig.PortfolioSnapshot(
        bucket_day="2026-05-19", ts="2026-05-19T00:00:00Z",
        ev_total=10_000.0, prospect_count=1, pipeline_median_score=50.0,
        bandit_top_arms={"_default": "accelerate"},
        prospect_ids=["p1"],
    )
    store.save(prev)
    log: list[tuple] = []
    trig.manual_signal()  # also armed -> would fire operator_manual
    ctx = _ctx(
        prospects=[
            # p2 is new -> would fire new_cohort
            {"prospect_id": "p1", "lead_score": 10.0, "seo_score": 90.0, "conversion_signals": []},
            {"prospect_id": "p2", "lead_score": 90.0, "seo_score": 50.0, "conversion_signals": []},
        ],
        bandit_stats={
            "accelerate": {"wins": 1.0, "trials": 10},
            "defer": {"wins": 9.0, "trials": 10},  # would fire bandit_divergence
        },
        efficiency_ema_by_workcell={"prospecting": 0.1, "seo": 0.1},  # would fire budget_recovery
    )

    event = trig.check_and_fire(
        ctx, store=store, min_signal_change=0.15, propose_fn=_stub_propose(log),
    )

    assert event is not None
    # pipeline_ev_step is walk-order #1 and a 10000 -> ~60 delta is well > 15%
    assert event.name == "pipeline_ev_step"
    # Exactly ONE LLM call despite four conditions being true.
    assert len(log) == 1
    # The manual signal was NOT consumed (since check 4 wasn't reached AND
    # we documented that manual_signal stays armed until its branch evaluates).
    # We verify it's still armed:
    assert trig._manual_signal_armed() is True


# ---------------------------------------------------------------------------
# Operator-manual rate-limit holds to 1/hour
# ---------------------------------------------------------------------------

def test_operator_manual_rate_limit_holds_one_per_hour(tmp_path):
    """Two manual_signal() calls within an hour -> exactly one fire."""
    store = _store(tmp_path)
    # Seed prev so no other trigger fires.
    prev = trig.PortfolioSnapshot(
        bucket_day="2026-05-19", ts="2026-05-19T00:00:00Z",
        ev_total=55.0, prospect_count=1, pipeline_median_score=80.0,
        bandit_top_arms={},
        prospect_ids=["p1"],
    )
    store.save(prev)
    log: list[tuple] = []

    # Deterministic clock for the rate limiter
    clock = {"now": 1_000_000.0}
    trig._MANUAL_RATE_LIMITER.set_clock(lambda: clock["now"])

    base_ctx_kwargs = dict(
        prospects=[
            {"prospect_id": "p1", "lead_score": 80.0, "seo_score": 70.0, "conversion_signals": []},
        ],
    )

    # First call: signal armed, limiter allows -> fire
    trig.manual_signal()
    e1 = trig.check_and_fire(_ctx(**base_ctx_kwargs), store=store, propose_fn=_stub_propose(log))
    assert e1 is not None and e1.name == "operator_manual"
    assert len(log) == 1

    # Re-seed prev to match the just-saved snapshot so no other trigger fires
    # (check_and_fire already saved it; load_latest will return the new state).
    # Advance clock by 5 minutes -> still inside 1-hour window.
    clock["now"] += 300.0
    trig.manual_signal()
    e2 = trig.check_and_fire(_ctx(**base_ctx_kwargs), store=store, propose_fn=_stub_propose(log))
    # Rate-limited: no fire, signal consumed.
    assert e2 is None
    assert len(log) == 1
    assert trig._manual_signal_armed() is False

    # Advance clock past 1 hour -> next manual_signal should fire.
    clock["now"] += 3601.0
    trig.manual_signal()
    e3 = trig.check_and_fire(_ctx(**base_ctx_kwargs), store=store, propose_fn=_stub_propose(log))
    assert e3 is not None and e3.name == "operator_manual"
    assert len(log) == 2


# ---------------------------------------------------------------------------
# Audit ledger persists fire events
# ---------------------------------------------------------------------------

def test_check_and_fire_records_event_to_audit_ledger(tmp_path):
    store = _store(tmp_path)
    prev = trig.PortfolioSnapshot(
        bucket_day="2026-05-19", ts="2026-05-19T00:00:00Z",
        ev_total=55.0, prospect_count=1, pipeline_median_score=80.0,
        bandit_top_arms={},
        prospect_ids=["p1"],
    )
    store.save(prev)
    log: list[tuple] = []
    trig.manual_signal()
    ctx = _ctx(
        prospects=[
            {"prospect_id": "p1", "lead_score": 80.0, "seo_score": 70.0, "conversion_signals": []},
        ],
    )

    event = trig.check_and_fire(ctx, store=store, propose_fn=_stub_propose(log))
    assert event is not None

    ledger_path = trig.trigger_ledger_path()
    assert os.path.exists(ledger_path), "ledger should be created"
    with open(ledger_path, "r", encoding="utf-8") as fh:
        lines = [json.loads(line) for line in fh.readlines() if line.strip()]
    assert len(lines) == 1
    record = lines[0]
    assert record["name"] == "operator_manual"
    assert "fired_at" in record
    assert "detail" in record
    assert record["budget_denied"] is False


# ---------------------------------------------------------------------------
# JSON-fallback snapshot persistence works without boto3 / DDB
# ---------------------------------------------------------------------------

def test_json_fallback_snapshot_round_trip_without_ddb(tmp_path, monkeypatch):
    """Snapshot save + load_latest must work with DDB disabled (empty table name).

    Mirrors the llm_budget JSON-fallback contract: when the DDB table env is
    empty, the store skips DDB entirely and only writes to the JSON file.
    boto3 doesn't need to be importable for this path to function.
    """
    json_path = tmp_path / "snapshots.json"
    store = trig.SnapshotStore(
        ddb_table="",  # empty -> DDB backend disabled entirely
        region="us-west-1",
        json_path=str(json_path),
    )
    # The DDB backend should be None
    assert store._ddb_backend is None

    snap = trig.PortfolioSnapshot(
        bucket_day="2026-05-19", ts="2026-05-19T00:00:00Z",
        ev_total=123.45, prospect_count=7, pipeline_median_score=42.0,
        bandit_top_arms={"_default": "accelerate"},
        efficiency_ema_by_workcell={"prospecting": 0.7, "seo": 0.8},
        prospect_ids=["a", "b", "c"],
    )
    ok = store.save(snap)
    assert ok is True
    assert json_path.exists()

    # Raw file is valid JSON keyed by bucket_day
    with open(json_path, "r", encoding="utf-8") as fh:
        raw = json.load(fh)
    assert "2026-05-19" in raw
    assert raw["2026-05-19"]["ev_total"] == 123.45

    # load_latest returns the same shape
    loaded = store.load_latest()
    assert loaded is not None
    assert loaded.bucket_day == "2026-05-19"
    assert loaded.ev_total == 123.45
    assert loaded.bandit_top_arms == {"_default": "accelerate"}
    assert loaded.efficiency_ema_by_workcell == {"prospecting": 0.7, "seo": 0.8}
    assert loaded.prospect_ids == ["a", "b", "c"]


# ---------------------------------------------------------------------------
# Settings binding — env vars propagate to Settings
# ---------------------------------------------------------------------------

def test_settings_pick_up_portfolio_env_vars(monkeypatch):
    monkeypatch.setenv("SAMUS_PORTFOLIO_DOLLAR_CAP", "0.35")
    monkeypatch.setenv("SAMUS_PORTFOLIO_MIN_SIGNAL_CHANGE", "0.22")
    monkeypatch.setenv("SAMUS_PORTFOLIO_TICK_INTERVAL_SEC", "600")

    from backend.common.settings import reload_settings
    s = reload_settings()
    assert s.portfolio_workcell_dollar_cap == 0.35
    assert s.portfolio_min_signal_change == 0.22
    assert s.portfolio_tick_interval_sec == 600


# ---------------------------------------------------------------------------
# Tick loop wiring — handle stops cleanly without firing
# ---------------------------------------------------------------------------

def test_start_tick_loop_returns_handle_and_stop_cancels(tmp_path):
    """Smoke: handle is created with a long interval, .stop() cancels cleanly."""
    store = _store(tmp_path)
    log: list[tuple] = []

    def _provider() -> trig.TriggerContext:
        return _ctx(
            prospects=[{"prospect_id": "p1", "lead_score": 80.0, "seo_score": 70.0, "conversion_signals": []}],
        )

    # Long interval; we cancel before it fires.
    handle = trig.start_tick_loop(
        _provider,
        interval_sec=3600.0,
        store=store,
        propose_fn=_stub_propose(log),
    )
    try:
        # No fire should have happened (interval is 1h)
        time.sleep(0.05)
        assert log == []
    finally:
        handle.stop()
    assert handle.stopped is True
