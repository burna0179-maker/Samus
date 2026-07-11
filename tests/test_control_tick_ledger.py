"""Tests for the control-tick JSONL ledger (backend.common.control_tick_ledger).

Covers: append + tail round-trip, ts injection, limit clamping, and the
fail-soft contract (record_tick returns a bool and never raises; recent_ticks
returns a populated ``error`` field on a read failure).
"""

from __future__ import annotations

import pytest

from backend.common import control_tick_ledger as ctl


@pytest.fixture()
def ledger_path(tmp_path, monkeypatch):
    """Point the ledger at a per-test tmpfile via the env override."""
    path = tmp_path / "control_ticks.jsonl"
    monkeypatch.setenv("SAMUS_CONTROL_TICK_PATH", str(path))
    return path


def test_record_and_recent_round_trip(ledger_path):
    assert ctl.record_tick({"task_id": "ct-1", "ok": True}) is True
    assert ctl.record_tick({"task_id": "ct-2", "ok": False}) is True

    view = ctl.recent_ticks(limit=10)
    assert view["error"] is None
    assert view["count"] == 2
    # Newest last — tail order is append order.
    assert [t["task_id"] for t in view["ticks"]] == ["ct-1", "ct-2"]


def test_record_tick_injects_ts_when_absent(ledger_path):
    ctl.record_tick({"task_id": "ct-no-ts", "ok": True})
    view = ctl.recent_ticks()
    assert view["count"] == 1
    row = view["ticks"][0]
    assert "ts" in row and row["ts"]  # ISO8601 string injected


def test_record_tick_preserves_caller_ts(ledger_path):
    ctl.record_tick({"task_id": "ct-ts", "ts": "2026-05-20T00:00:00+00:00"})
    row = ctl.recent_ticks()["ticks"][0]
    assert row["ts"] == "2026-05-20T00:00:00+00:00"


def test_recent_ticks_empty_when_never_written(ledger_path):
    view = ctl.recent_ticks()
    assert view == {"ticks": [], "count": 0, "error": None}


def test_recent_ticks_limit_clamped_low(ledger_path):
    for i in range(5):
        ctl.record_tick({"task_id": f"ct-{i}"})
    # limit <= 0 clamps to 1.
    view = ctl.recent_ticks(limit=0)
    assert view["count"] == 1
    # Returns the newest row.
    assert view["ticks"][0]["task_id"] == "ct-4"


def test_recent_ticks_limit_respected(ledger_path):
    for i in range(10):
        ctl.record_tick({"task_id": f"ct-{i}"})
    view = ctl.recent_ticks(limit=3)
    assert view["count"] == 3
    assert [t["task_id"] for t in view["ticks"]] == ["ct-7", "ct-8", "ct-9"]


def test_record_tick_fail_soft_on_oserror(ledger_path, monkeypatch):
    """A filesystem error degrades to False, never raises."""
    from backend.common.persistence import JsonlLedger

    def _boom(self, record):
        raise OSError("disk gone")

    monkeypatch.setattr(JsonlLedger, "append", _boom)
    assert ctl.record_tick({"task_id": "ct-fail"}) is False


def test_recent_ticks_fail_soft_on_read_error(ledger_path, monkeypatch):
    """A read failure yields an empty list + populated error string."""
    from backend.common.persistence import JsonlLedger

    def _boom(self, limit=50):
        raise RuntimeError("ledger unreadable")

    monkeypatch.setattr(JsonlLedger, "tail", _boom)
    view = ctl.recent_ticks()
    assert view["ticks"] == []
    assert view["count"] == 0
    assert view["error"] is not None
    assert "control_tick_read_failed" in view["error"]
