"""Self-supply — starvation diagnosis + bounded replenishment (2026-07-03).

Covers the live failure that motivated the module: the idle-drive portfolio
ran every business-hours tick with ``initiated=0`` and every campaign skipped
``ineligible`` (missing daily call list), with no awareness and no remediation.
"""
from __future__ import annotations

import json

import pytest

from backend.cash_engine.self_supply import (
    StarvationDiagnosis,
    diagnose_starvation,
    replenish,
    run_self_supply,
)


def _starved_actuation() -> dict:
    """The exact shape observed live on 2026-07-03 ticks."""
    return {
        "channel": "portfolio",
        "initiated": 0,
        "selected": [],
        "skipped": [["email_outreach", "ineligible"], ["voice_consent_routed", "ineligible"]],
    }


# ---------------------------------------------------------------------------
# diagnose_starvation — pure judgment
# ---------------------------------------------------------------------------
def test_diagnoses_starvation_when_all_ineligible_and_no_list():
    d = diagnose_starvation(_starved_actuation(), call_list_exists=lambda: False)
    assert d.starved is True
    assert d.causes == ["call_list_missing"]


def test_starved_but_list_present_reports_no_replenishable_cause():
    d = diagnose_starvation(_starved_actuation(), call_list_exists=lambda: True)
    assert d.starved is True
    assert d.causes == []
    assert "outside self-supply scope" in d.note


def test_not_starved_when_production_flowed():
    act = _starved_actuation() | {"initiated": 2}
    d = diagnose_starvation(act, call_list_exists=lambda: False)
    assert d.starved is False


def test_not_starved_when_skips_are_not_ineligible():
    act = _starved_actuation() | {"skipped": [["email_outreach", "over_budget"]]}
    d = diagnose_starvation(act, call_list_exists=lambda: False)
    assert d.starved is False


def test_none_and_non_portfolio_actuations_are_ignored():
    assert diagnose_starvation(None).starved is False
    assert diagnose_starvation({"channel": "direct"}).starved is False


# ---------------------------------------------------------------------------
# replenish — bounded actuation
# ---------------------------------------------------------------------------
@pytest.fixture()
def state_root(monkeypatch, tmp_path):
    from backend.common import storage

    monkeypatch.setattr(storage, "root", lambda: tmp_path)
    return tmp_path


def test_replenish_dispatches_prospecting_daily_supply(state_root):
    calls = []

    def fake_dispatch(service, action):
        calls.append((service, action))
        return {"status": "started"}

    out = replenish(["call_list_missing"], dispatch=fake_dispatch)
    assert calls == [("prospecting", "daily_supply")]
    assert out["call_list_missing"]["outcome"] == "dispatched"
    assert out["call_list_missing"]["attempt"] == 1
    ledger = state_root / "cash_engine" / "self_supply_ledger.jsonl"
    rows = [json.loads(l) for l in ledger.read_text().splitlines()]
    assert rows[0]["kind"] == "replenish_attempt" and rows[0]["cause"] == "call_list_missing"


def test_replenish_caps_attempts_and_escalates(state_root):
    def fake_dispatch(service, action):
        return {"status": "started"}

    for _ in range(3):
        out = replenish(["call_list_missing"], dispatch=fake_dispatch, max_attempts=3)
        assert out["call_list_missing"]["outcome"] == "dispatched"
    out = replenish(["call_list_missing"], dispatch=fake_dispatch, max_attempts=3)
    assert out["call_list_missing"]["outcome"] == "attempts_exhausted"
    alerts = list((state_root / "cash_engine" / "supply_alerts").glob("alert_*.json"))
    assert len(alerts) == 1
    payload = json.loads(alerts[0].read_text())
    assert payload["kind"] == "self_supply_exhausted"


def test_replenish_dispatch_error_is_ledgered_not_raised(state_root):
    def boom(service, action):
        raise RuntimeError("prospecting_http_503")

    out = replenish(["call_list_missing"], dispatch=boom)
    assert out["call_list_missing"]["outcome"] == "error"
    ledger = state_root / "cash_engine" / "self_supply_ledger.jsonl"
    assert "prospecting_http_503" in ledger.read_text()


def test_replenish_error_attempts_still_count_toward_cap(state_root):
    def boom(service, action):
        raise RuntimeError("down")

    for _ in range(3):
        replenish(["call_list_missing"], dispatch=boom, max_attempts=3)
    out = replenish(["call_list_missing"], dispatch=boom, max_attempts=3)
    assert out["call_list_missing"]["outcome"] == "attempts_exhausted"


def test_unknown_cause_is_not_replenishable(state_root):
    out = replenish(["martians"], dispatch=lambda s, a: {"status": "started"})
    assert out["martians"]["outcome"] == "not_replenishable"


# ---------------------------------------------------------------------------
# run_self_supply — the idle-drive hook (flag-gated actuation, free awareness)
# ---------------------------------------------------------------------------
def test_disarmed_still_diagnoses_but_holds_replenish(state_root, monkeypatch):
    monkeypatch.delenv("SAMUS_SELF_SUPPLY_ENABLED", raising=False)
    fired = []
    out = run_self_supply(_starved_actuation(), dispatch=lambda s, a: fired.append((s, a)))
    assert out["starved"] is True
    assert "call_list_missing" in out["causes"] or out["causes"] == []
    assert out.get("replenish") is None
    assert fired == []


def test_armed_replenishes(state_root, monkeypatch):
    import backend.cash_engine.self_supply as ss

    monkeypatch.setattr(ss, "_enabled", lambda: True)
    monkeypatch.setattr(ss, "_call_list_exists", lambda: False)
    fired = []

    def fake_dispatch(service, action):
        fired.append((service, action))
        return {"status": "started"}

    out = run_self_supply(_starved_actuation(), dispatch=fake_dispatch)
    assert out["starved"] is True
    assert fired == [("prospecting", "daily_supply")]
    assert out["replenish"]["call_list_missing"]["outcome"] == "dispatched"


def test_healthy_actuation_returns_quiet(state_root):
    out = run_self_supply({"channel": "portfolio", "initiated": 3, "skipped": []})
    assert out["starved"] is False
    assert "replenish" not in out


def test_hook_never_raises_on_garbage():
    out = run_self_supply({"channel": "portfolio", "initiated": "weird",
                           "skipped": "not-a-list"})
    assert out["ok"] is True


# ---------------------------------------------------------------------------
# diagnose_pool_exhaustion — dry-pool awareness (the 7/03 midday stall)
# ---------------------------------------------------------------------------
def _flowing_but_suppressed_actuation() -> dict:
    return {
        "channel": "portfolio",
        "initiated": 3,
        "skipped": [],
        "results": {
            "voice_consent_routed": {"dialed": 0, "drafted": 3},
            "email_outreach": {"sent": 0, "failed": 0,
                               "day_tally": {"suppressed": 5}},
        },
    }


def test_detects_dry_pool_when_all_suppressed():
    from backend.cash_engine.self_supply import diagnose_pool_exhaustion

    ex = diagnose_pool_exhaustion(_flowing_but_suppressed_actuation())
    assert ex and ex["exhausted"] is True and ex["suppressed_today"] == 5


def test_no_exhaustion_when_sends_flow():
    from backend.cash_engine.self_supply import diagnose_pool_exhaustion

    act = _flowing_but_suppressed_actuation()
    act["results"]["email_outreach"]["day_tally"] = {"sent": 4, "suppressed": 5}
    act["results"]["email_outreach"]["sent"] = 4
    assert diagnose_pool_exhaustion(act) is None


def test_no_exhaustion_without_tally():
    from backend.cash_engine.self_supply import diagnose_pool_exhaustion

    act = _flowing_but_suppressed_actuation()
    act["results"]["email_outreach"].pop("day_tally")
    assert diagnose_pool_exhaustion(act) is None


def test_run_self_supply_alerts_once_per_day_on_dry_pool(state_root):
    from backend.cash_engine.self_supply import run_self_supply

    out1 = run_self_supply(_flowing_but_suppressed_actuation())
    assert out1["pool_exhaustion"]["exhausted"] is True
    out2 = run_self_supply(_flowing_but_suppressed_actuation())
    assert out2["pool_exhaustion"]["exhausted"] is True
    alerts = list((state_root / "cash_engine" / "supply_alerts").glob("*.json"))
    assert len(alerts) == 1  # deduped per business day
