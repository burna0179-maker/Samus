"""Tests for backend.gateway.morning_ritual_task.

Focus is the pure reasoning function should_fire_now -- everything else in
this module is a defensive wrapper around it. The reasoning is a simple
predicate but the operator's day depends on it firing once (and only once)
inside the morning window, so pin every branch.
"""
from __future__ import annotations

import json

import pytest

from backend.gateway import morning_ritual_task as mrt


# ---------------------------------------------------------------------------
# should_fire_now -- pure reasoning
# ---------------------------------------------------------------------------

def _clear_env(monkeypatch):
    for k in (mrt.ENV_ENABLED, mrt.ENV_HOUR, mrt.ENV_LATEST):
        monkeypatch.delenv(k, raising=False)


@pytest.fixture
def isolated_ledger(tmp_path, monkeypatch):
    """Point storage.root() at tmp so ledger reads/writes stay local."""
    monkeypatch.setenv("SAMUS_ARTIFACT_ROOT", str(tmp_path))
    return tmp_path


def test_fires_inside_window_when_not_yet_fired(monkeypatch, isolated_ledger):
    _clear_env(monkeypatch)
    fire, reason = mrt.should_fire_now("2026-07-07", 7)
    assert fire is True, reason
    assert "inside window" in reason


def test_skips_before_earliest_hour(monkeypatch, isolated_ledger):
    _clear_env(monkeypatch)
    fire, reason = mrt.should_fire_now("2026-07-07", 6)
    assert fire is False
    assert "before window" in reason


def test_skips_after_latest_hour(monkeypatch, isolated_ledger):
    _clear_env(monkeypatch)
    fire, reason = mrt.should_fire_now("2026-07-07", 10)
    assert fire is False
    assert "after window" in reason


def test_respects_custom_window(monkeypatch, isolated_ledger):
    """Window knobs override the 07-10 default."""
    monkeypatch.setenv(mrt.ENV_HOUR, "6")
    monkeypatch.setenv(mrt.ENV_LATEST, "8")
    fire, _ = mrt.should_fire_now("2026-07-07", 6)  # newly inside
    assert fire is True
    fire, _ = mrt.should_fire_now("2026-07-07", 5)  # still below
    assert fire is False
    fire, _ = mrt.should_fire_now("2026-07-07", 8)  # newly at cap (excl)
    assert fire is False


def test_skips_when_master_disabled(monkeypatch, isolated_ledger):
    monkeypatch.setenv(mrt.ENV_ENABLED, "0")
    fire, reason = mrt.should_fire_now("2026-07-07", 7)
    assert fire is False
    assert "disabled" in reason


def test_skips_when_already_fired_today(monkeypatch, isolated_ledger):
    """A ledger record for today means "already handled, do not double-send"."""
    _clear_env(monkeypatch)
    from backend.common import storage
    d = storage.root() / "cognition"
    d.mkdir(parents=True, exist_ok=True)
    (d / "morning_ritual_fired_2026-07-07.json").write_text(
        json.dumps({"business_date": "2026-07-07"}), encoding="utf-8"
    )
    fire, reason = mrt.should_fire_now("2026-07-07", 7)
    assert fire is False
    assert "already fired" in reason


def test_yesterdays_ledger_does_not_block_today(monkeypatch, isolated_ledger):
    """A leftover ledger from a prior business date must NOT gate today's fire."""
    _clear_env(monkeypatch)
    from backend.common import storage
    d = storage.root() / "cognition"
    d.mkdir(parents=True, exist_ok=True)
    (d / "morning_ritual_fired_2026-07-06.json").write_text(
        json.dumps({"business_date": "2026-07-06"}), encoding="utf-8"
    )
    fire, _ = mrt.should_fire_now("2026-07-07", 7)
    assert fire is True


def test_corrupt_ledger_is_fail_closed(monkeypatch, isolated_ledger):
    """A corrupt ledger file returns True (already-fired) to avoid double-send.

    Operator can rm the file to force a fire on the next tick.
    """
    _clear_env(monkeypatch)
    from backend.common import storage
    d = storage.root() / "cognition"
    d.mkdir(parents=True, exist_ok=True)
    (d / "morning_ritual_fired_2026-07-07.json").write_text(
        "not-json-at-all{{{{", encoding="utf-8"
    )
    fire, reason = mrt.should_fire_now("2026-07-07", 7)
    assert fire is False
    assert "already fired" in reason


# ---------------------------------------------------------------------------
# _mark_fired -- ledger write
# ---------------------------------------------------------------------------

def test_mark_fired_writes_ledger(monkeypatch, isolated_ledger):
    _clear_env(monkeypatch)
    mrt._mark_fired("2026-07-07", {
        "briefing_id": "b-123", "ingested": 2, "send_rc": 0,
    })
    from backend.common import storage
    path = storage.root() / "cognition" / "morning_ritual_fired_2026-07-07.json"
    assert path.is_file()
    row = json.loads(path.read_text(encoding="utf-8"))
    assert row["business_date"] == "2026-07-07"
    assert row["briefing_id"] == "b-123"
    assert row["ingested"] == 2
    assert row["send_rc"] == 0


def test_full_cycle_ledger_gates_second_fire(monkeypatch, isolated_ledger):
    """mark_fired then should_fire_now returns False on the same day.

    This is the critical invariant: even a race between two ticks in the
    same 5-min window must not send twice.
    """
    _clear_env(monkeypatch)
    fire1, _ = mrt.should_fire_now("2026-07-07", 7)
    assert fire1 is True
    mrt._mark_fired("2026-07-07", {"briefing_id": "b-x"})
    fire2, reason = mrt.should_fire_now("2026-07-07", 7)
    assert fire2 is False
    assert "already fired" in reason


# ---------------------------------------------------------------------------
# _do_send -- SAMUS_BRIEF_EMAIL_TO default
# ---------------------------------------------------------------------------

def test_do_send_defaults_email_to(monkeypatch, isolated_ledger):
    """Empty SAMUS_BRIEF_EMAIL_TO gets the operator's default so morning_send
    does not treat email as disabled inside the container."""
    monkeypatch.delenv(mrt.ENV_EMAIL_TO, raising=False)

    called = {}
    class _FakeMorningSend:
        @staticmethod
        def main(argv):
            called["email_to"] = __import__("os").environ.get(mrt.ENV_EMAIL_TO)
            called["argv"] = argv
            return 0
    monkeypatch.setitem(__import__("sys").modules, "backend.morning_send", _FakeMorningSend)

    result = mrt._do_send()
    assert result == {"sent": True, "send_rc": 0}
    assert called["email_to"] == mrt._DEFAULT_EMAIL_TO


def test_do_send_respects_send_disabled(monkeypatch, isolated_ledger):
    monkeypatch.setenv(mrt.ENV_SEND, "0")
    result = mrt._do_send()
    assert result == {"sent": False, "skipped": True}


def test_do_attest_respects_attest_disabled(monkeypatch, isolated_ledger):
    monkeypatch.setenv(mrt.ENV_ATTEST, "0")
    result = mrt._do_attest()
    assert result == {"attested": False, "skipped": True}


# ---------------------------------------------------------------------------
# _fire -- atomic attest + send + stamp
# ---------------------------------------------------------------------------

def test_fire_stamps_ledger_atomically(monkeypatch, isolated_ledger):
    """_fire must stamp the ledger itself so an out-of-band manual invocation
    (docker exec ... _fire()) cannot be double-fired by a later auto-tick."""
    _clear_env(monkeypatch)
    monkeypatch.setattr(mrt, "_do_attest", lambda: {
        "attested": True, "briefing_id": "b-atomic", "ingested": 3,
    })
    monkeypatch.setattr(mrt, "_do_send", lambda: {"sent": True, "send_rc": 0})

    result = mrt._fire()

    assert result["attested"] is True
    assert result["sent"] is True
    assert result["briefing_id"] == "b-atomic"
    assert result["send_rc"] == 0

    business_date, _ = mrt._now_pt()
    from backend.common import storage
    path = storage.root() / "cognition" / f"morning_ritual_fired_{business_date}.json"
    assert path.is_file(), "ledger must be stamped by _fire itself"
    row = json.loads(path.read_text(encoding="utf-8"))
    assert row["business_date"] == business_date
    assert row["briefing_id"] == "b-atomic"
    assert row["ingested"] == 3
    assert row["send_rc"] == 0


def test_fire_then_should_fire_now_is_false(monkeypatch, isolated_ledger):
    """After _fire(), the reasoning core sees "already fired" without any
    separate mark_fired call from the loop."""
    _clear_env(monkeypatch)
    monkeypatch.setattr(mrt, "_do_attest", lambda: {"attested": True})
    monkeypatch.setattr(mrt, "_do_send", lambda: {"sent": True, "send_rc": 0})

    mrt._fire()

    business_date, _ = mrt._now_pt()
    fire, reason = mrt.should_fire_now(business_date, 7)
    assert fire is False
    assert "already fired" in reason
