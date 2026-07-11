"""HOTL approval queue (backend/common/approvals.py) — ADR-0019 semantics."""
from __future__ import annotations

import time

import pytest

from backend.common import approvals as mod


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("SAMUS_APPROVALS_PATH", str(tmp_path / "approvals.json"))
    # Keep DDB out of the way in every environment.
    monkeypatch.setattr(mod, "_ddb_table", lambda: None)
    yield


def test_create_routine_low_risk():
    row = mod.create_approval("stake_draft", {"opportunity_id": "o1"},
                              risk_level="normal", ev_usd=1200.0, confidence=0.8)
    assert row["id"]
    assert row["status"] == "pending"
    assert row["severity"] == "routine"
    assert row["risk_level"] == "normal"
    assert row["ev_usd"] == 1200.0
    assert row["ttl_expires_at"] > row["created_at"]


@pytest.mark.parametrize("level", ["high", "critical", "HIGH"])
def test_high_and_critical_are_emergency(level):
    row = mod.create_approval("countermeasure", {}, risk_level=level)
    assert row["severity"] == "emergency"


def test_get_and_list_roundtrip():
    a = mod.create_approval("stake_draft", {"opportunity_id": "o1"})
    b = mod.create_approval("countermeasure", {}, risk_level="high")
    got = mod.get_approval(a["id"])
    assert got and got["kind"] == "stake_draft"
    pending = mod.list_approvals()
    assert {r["id"] for r in pending} == {a["id"], b["id"]}
    only_stake = mod.list_approvals(kind="stake_draft")
    assert [r["id"] for r in only_stake] == [a["id"]]


def test_approve_and_reject():
    a = mod.create_approval("stake_draft", {})
    row = mod.decide_approval(a["id"], "approve", decided_by="alex")
    assert row["status"] == "approved"
    assert row["decided_by"] == "alex"
    assert mod.is_currently_approved(a["id"]) is True

    b = mod.create_approval("stake_draft", {})
    row = mod.decide_approval(b["id"], "rejected")
    assert row["status"] == "rejected"
    assert mod.is_currently_approved(b["id"]) is False


def test_double_decide_returns_none():
    a = mod.create_approval("stake_draft", {})
    assert mod.decide_approval(a["id"], "approve") is not None
    assert mod.decide_approval(a["id"], "reject") is None


def test_unknown_id_and_bad_verb():
    assert mod.decide_approval("nope", "approve") is None
    a = mod.create_approval("stake_draft", {})
    assert mod.decide_approval(a["id"], "maybe") is None


def test_ttl_expiry_fail_closed(monkeypatch):
    a = mod.create_approval("stake_draft", {}, ttl_seconds=1)
    # Warp past the deadline.
    real_time = time.time
    monkeypatch.setattr(mod.time, "time", lambda: real_time() + 5)
    row = mod.get_approval(a["id"])
    assert row["status"] == "expired"
    # Expired can no longer be approved.
    assert mod.decide_approval(a["id"], "approve") is None
    assert mod.is_currently_approved(a["id"]) is False


def test_expired_rows_leave_pending_list():
    a = mod.create_approval("stake_draft", {}, ttl_seconds=1)
    b = mod.create_approval("stake_draft", {}, ttl_seconds=3600)
    import time as _t
    with pytest.MonkeyPatch.context() as mp:
        real = _t.time
        mp.setattr(mod.time, "time", lambda: real() + 5)
        pending = mod.list_approvals()
    assert [r["id"] for r in pending] == [b["id"]]


def test_batch_approve_low_only():
    low1 = mod.create_approval("stake_draft", {}, risk_level="normal")
    low2 = mod.create_approval("stake_draft", {}, risk_level="normal")
    high = mod.create_approval("stake_draft", {}, risk_level="high")
    out = mod.batch_approve([low1["id"], low2["id"], high["id"], "ghost"])
    assert set(out["approved"]) == {low1["id"], low2["id"]}
    whys = {s["id"]: s["why"] for s in out["skipped"]}
    assert whys[high["id"]] == "emergency_requires_per_item"
    assert whys["ghost"] == "not_pending"
    assert mod.get_approval(high["id"])["status"] == "pending"


def test_approved_decision_survives_ttl(monkeypatch):
    """An explicit operator yes does not evaporate at the pending-TTL."""
    a = mod.create_approval("stake_draft", {}, ttl_seconds=10)
    mod.decide_approval(a["id"], "approve")
    real = time.time
    monkeypatch.setattr(mod.time, "time", lambda: real() + 60)
    assert mod.is_currently_approved(a["id"]) is True


def test_create_never_raises_on_broken_store(monkeypatch):
    def _boom(row):
        raise RuntimeError("store down")

    monkeypatch.setattr(mod, "_save", _boom)
    row = mod.create_approval("stake_draft", {})
    assert isinstance(row, dict)
    assert row["status"] == "error"


def test_persistence_across_module_reads(tmp_path, monkeypatch):
    a = mod.create_approval("stake_draft", {"opportunity_id": "o9"})
    # A "fresh process" = new read of the same JSON file.
    rows = mod._json_load_all()
    assert a["id"] in rows
