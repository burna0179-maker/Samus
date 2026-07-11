"""Tests for the idle-production drive (self-pacing engine).

Layers: the PURE decision (decide_idle_production), the real observer helpers
(ledger reads + business-hours window), and the orchestrator (run_idle_drive)
with injected observer/producer so no real production fires."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from backend.cash_engine import idle_production as idp
from backend.cash_engine.idle_production import (
    IdleObservation,
    decide_idle_production,
    run_idle_drive,
)

NOW = 1_800_000_000.0
THRESH = 2700.0  # 45m
LONG_AGO = NOW - 3 * THRESH
JUST_NOW = NOW - 60.0


# --- pure decision -----------------------------------------------------------


def _decide(**over):
    kw = dict(
        enabled=True,
        now_ts=NOW,
        last_activity_ts=LONG_AGO,
        in_business_hours=True,
        behind_pace=None,
        idle_threshold_s=THRESH,
    )
    kw.update(over)
    return decide_idle_production(**kw)


def test_produces_when_idle_in_hours_pace_unknown():
    d = _decide()
    assert d.should_produce is True
    assert "idle" in d.reason and d.idle_seconds == pytest.approx(3 * THRESH)


def test_produces_when_idle_and_behind_pace():
    assert _decide(behind_pace=True).should_produce is True


def test_holds_when_disarmed_even_if_idle():
    d = _decide(enabled=False)
    assert d.should_produce is False and d.reason == "disarmed"


def test_holds_off_hours_even_if_idle_and_behind():
    d = _decide(in_business_hours=False, behind_pace=True)
    assert d.should_produce is False and d.reason == "off-hours"


def test_holds_when_recently_active():
    d = _decide(last_activity_ts=JUST_NOW)
    assert d.should_produce is False and "recently active" in d.reason


def test_holds_when_on_or_ahead_of_pace():
    d = _decide(behind_pace=False)
    assert d.should_produce is False and d.reason == "on/ahead of pace"


def test_cold_start_none_activity_is_idle_enough_to_produce():
    d = _decide(last_activity_ts=None)
    assert d.should_produce is True and d.idle_seconds == pytest.approx(THRESH)


def test_boundary_just_under_threshold_holds():
    d = _decide(last_activity_ts=NOW - (THRESH - 1))
    assert d.should_produce is False


# --- real observer helpers ---------------------------------------------------


def test_parse_iso_epoch_handles_z_and_naive():
    z = idp._parse_iso_epoch("2026-07-02T15:00:00Z")
    off = idp._parse_iso_epoch("2026-07-02T15:00:00+00:00")
    assert z == off and z is not None
    assert idp._parse_iso_epoch("") is None
    assert idp._parse_iso_epoch("not-a-date") is None


def test_last_call_activity_reads_initiated_attempt(monkeypatch, tmp_path):
    monkeypatch.setattr("backend.common.storage._ROOT", tmp_path)
    now = datetime(2026, 7, 2, 20, 0, tzinfo=timezone.utc)
    runs = tmp_path / "voice" / "dial_runs"
    runs.mkdir(parents=True)
    (runs / "dial_run_20260702_1.json").write_text(
        json.dumps(
            {
                "attempts": [
                    {"outcome": "skipped_cooldown", "initiated_at": "2026-07-02T18:00:00Z"},
                    {"outcome": "initiated", "initiated_at": "2026-07-02T19:30:00Z"},
                    {"outcome": "initiated", "initiated_at": "2026-07-02T19:45:00Z"},
                ]
            }
        ),
        encoding="utf-8",
    )
    ts = idp._last_call_activity_ts(now)
    assert ts == idp._parse_iso_epoch("2026-07-02T19:45:00Z")  # newest initiated only


def test_last_call_activity_none_when_only_skips(monkeypatch, tmp_path):
    monkeypatch.setattr("backend.common.storage._ROOT", tmp_path)
    now = datetime(2026, 7, 2, 20, 0, tzinfo=timezone.utc)
    runs = tmp_path / "voice" / "dial_runs"
    runs.mkdir(parents=True)
    (runs / "dial_run_20260702_1.json").write_text(
        json.dumps({"attempts": [{"outcome": "dry_run", "initiated_at": "2026-07-02T19:00:00Z"}]}),
        encoding="utf-8",
    )
    assert idp._last_call_activity_ts(now) is None


def test_last_email_activity_reads_sent_row(monkeypatch, tmp_path):
    monkeypatch.setattr("backend.common.storage._ROOT", tmp_path)
    now = datetime(2026, 7, 2, 20, 0, tzinfo=timezone.utc)
    # ISO-dashed filename — the name morning_batch actually writes. The old
    # test used the compact form (morning_batch_20260702) which is precisely
    # the name the reader never matched: it asserted the bug.
    (tmp_path / "morning_batch_2026-07-02.jsonl").write_text(
        json.dumps({"status": "failed", "ts": "2026-07-02T17:00:00Z"})
        + "\n"
        + json.dumps({"status": "sent", "ts": "2026-07-02T18:15:00Z"})
        + "\n",
        encoding="utf-8",
    )
    ts = idp._last_email_activity_ts(now)
    assert ts == idp._parse_iso_epoch("2026-07-02T18:15:00Z")


def test_last_email_activity_reads_outreach_subdir_iso_name(monkeypatch, tmp_path):
    """The live layout: <root>/outreach/morning_batch_<ISO>.jsonl."""
    monkeypatch.setattr("backend.common.storage._ROOT", tmp_path)
    now = datetime(2026, 7, 3, 20, 0, tzinfo=timezone.utc)
    outreach = tmp_path / "outreach"
    outreach.mkdir(parents=True)
    (outreach / "morning_batch_2026-07-03.jsonl").write_text(
        json.dumps({"status": "sent", "ts": "2026-07-03T19:08:45Z"}) + "\n",
        encoding="utf-8",
    )
    assert idp._last_email_activity_ts(now) == idp._parse_iso_epoch("2026-07-03T19:08:45Z")


def test_last_voicemail_draft_activity_reads_newest(monkeypatch, tmp_path):
    monkeypatch.setattr("backend.common.storage._ROOT", tmp_path)
    now = datetime(2026, 7, 3, 20, 0, tzinfo=timezone.utc)
    drafts = tmp_path / "voice" / "voicemail_drafts"
    drafts.mkdir(parents=True)
    f1 = drafts / "pr_a_2026-07-03.txt"
    f2 = drafts / "pr_b_2026-07-03.txt"
    f1.write_text("draft a", encoding="utf-8")
    f2.write_text("draft b", encoding="utf-8")
    import os as _os

    _os.utime(f1, (1_700_000_000, 1_700_000_000))
    _os.utime(f2, (1_700_000_500, 1_700_000_500))  # newer
    assert idp._last_voicemail_draft_activity_ts(now) == 1_700_000_500.0
    # Drafts count as production activity in the composite observation.
    assert idp._last_voicemail_draft_activity_ts(now) is not None


def test_last_activity_missing_ledgers_is_none(monkeypatch, tmp_path):
    monkeypatch.setattr("backend.common.storage._ROOT", tmp_path)
    now = datetime(2026, 7, 2, 20, 0, tzinfo=timezone.utc)
    assert idp._last_call_activity_ts(now) is None
    assert idp._last_email_activity_ts(now) is None


@pytest.mark.parametrize(
    "hour_utc,expect",
    [
        (16, True),  # 09:00 PDT Thu — inside 08–18 window
        (23, True),  # 16:00 PDT Thu — inside window
        (2, False),  # 19:00 PDT Wed — past 18:00, out of window
        (13, False),  # 06:00 PDT Thu — before 08:00, out of window
    ],
)
def test_drive_business_hours_weekday(hour_utc, expect):
    # 2026-07-02 is a Thursday; Pacific is UTC-7 in July (PDT).
    now = datetime(2026, 7, 2, hour_utc, 0, tzinfo=timezone.utc)
    assert idp._in_drive_business_hours(now) is expect


def test_drive_business_hours_excludes_weekend(monkeypatch):
    # 2026-07-04 is a Saturday.
    sat = datetime(2026, 7, 4, 18, 0, tzinfo=timezone.utc)  # 11:00 PT Sat
    assert idp._in_drive_business_hours(sat) is False


# --- orchestrator ------------------------------------------------------------


def _arm(monkeypatch, on: bool) -> None:
    from backend.common.settings import reload_settings

    if on:
        monkeypatch.setenv("SAMUS_IDLE_PRODUCTION_DRIVE_ENABLED", "true")
    else:
        monkeypatch.delenv("SAMUS_IDLE_PRODUCTION_DRIVE_ENABLED", raising=False)
    reload_settings()


class _Producer:
    def __init__(self):
        self.calls = []

    def __call__(self, decision):
        self.calls.append(decision)
        return {"channel": "voice", "initiated": 1}


def _obs(**over):
    kw = dict(last_activity_ts=LONG_AGO, in_business_hours=True, behind_pace=True)
    kw.update(over)
    return lambda: IdleObservation(**kw)


def test_run_disarmed_never_calls_producer(monkeypatch):
    _arm(monkeypatch, on=False)
    p = _Producer()
    out = run_idle_drive(now_ts=NOW, observer=_obs(), producer=p, idle_threshold_s=THRESH)
    assert out["ok"] is True and out["enabled"] is False
    assert out["produced"] is False and out["reason"] == "disarmed"
    assert p.calls == []  # dormant: producer NEVER touched when off


def test_run_armed_and_idle_invokes_producer(monkeypatch):
    _arm(monkeypatch, on=True)
    p = _Producer()
    out = run_idle_drive(now_ts=NOW, observer=_obs(), producer=p, idle_threshold_s=THRESH)
    assert out["produced"] is True and out["actuation"]["initiated"] == 1
    assert len(p.calls) == 1


def test_run_armed_but_recently_active_holds(monkeypatch):
    _arm(monkeypatch, on=True)
    p = _Producer()
    out = run_idle_drive(
        now_ts=NOW, observer=_obs(last_activity_ts=JUST_NOW), producer=p, idle_threshold_s=THRESH
    )
    assert out["produced"] is False and p.calls == []


def test_run_observer_fault_holds_and_does_not_raise(monkeypatch):
    _arm(monkeypatch, on=True)
    p = _Producer()

    def _boom():
        raise RuntimeError("crm down")

    out = run_idle_drive(now_ts=NOW, observer=_boom, producer=p, idle_threshold_s=THRESH)
    assert out["ok"] is True and out["produced"] is False
    assert "observer-error" in out["reason"] and p.calls == []


def test_run_producer_fault_holds_and_does_not_raise(monkeypatch):
    _arm(monkeypatch, on=True)

    def _boom(_decision):
        raise RuntimeError("dial stack down")

    out = run_idle_drive(now_ts=NOW, observer=_obs(), producer=_boom, idle_threshold_s=THRESH)
    assert out["ok"] is True and out["produced"] is False
    assert "producer-error" in out["reason"]
