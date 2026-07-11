"""Post-call reconciliation — backfill missing end_of_call events (Gap-8)."""
from __future__ import annotations

import json
from datetime import date

import pytest

import backend.voice.reconcile as rec


def _today_ts(hour=19, minute=17, sec=0):
    d = date.today()
    return f"{d.isoformat()}T{hour:02d}:{minute:02d}:{sec:02d}Z"


@pytest.fixture
def events_file(monkeypatch, tmp_path):
    p = tmp_path / "voice_events.jsonl"
    monkeypatch.setenv("SAMUS_VOICE_EVENTS_PATH", str(p))
    return p


@pytest.fixture
def dial_runs_dir(monkeypatch, tmp_path):
    d = tmp_path / "dial_runs"
    d.mkdir()
    monkeypatch.setenv("SAMUS_VOICE_DIAL_RUNS_PATH", str(d))
    return d


def _write(events_file, rows):
    with events_file.open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def _write_dial_run(dial_runs_dir, name, attempts, ts=None):
    run = {"run_id": name, "ts": ts or _today_ts(), "attempts": attempts}
    (dial_runs_dir / f"{name}.json").write_text(
        json.dumps(run), encoding="utf-8"
    )


def test_outcome_from_ended_reason():
    assert rec._outcome_from_ended_reason("voicemail") == "voicemail_left"
    assert rec._outcome_from_ended_reason("customer-ended-call") == "not_interested"
    assert rec._outcome_from_ended_reason("no-answer") == "no_answer"
    assert rec._outcome_from_ended_reason("assistant-ended-call") == "completed_conversation"


def test_reconcile_backfills_missing_end_of_call(events_file):
    """An initiated dial with no end_of_call event + an ended Vapi call →
    a backfilled end_of_call event."""
    _write(events_file, [
        {"ts": _today_ts(), "kind": "dial_attempt", "outcome": "initiated",
         "call_id": "call_abc", "prospect_id": "pr_x", "company": "American Home Realty",
         "outbound_number_id": "f775b297"},
    ])

    def fake_fetch(cid):
        assert cid == "call_abc"
        return {"status": "ended", "endedReason": "voicemail", "cost": 0.0357,
                "durationSeconds": 42, "analysis": {"summary": "Left a voicemail."}}

    written = []
    summary = rec.reconcile_recent_calls(
        fetch_call=fake_fetch, append_event=written.append,
    )
    assert summary["candidates"] == 1
    assert summary["reconciled"] == 1
    assert len(written) == 1
    ev = written[0]
    assert ev["kind"] == "end_of_call"
    assert ev["call_id"] == "call_abc"
    assert ev["outcome"] == "voicemail_left"
    assert ev["company"] == "American Home Realty"
    assert ev["reconciled"] is True
    assert ev["vapi_cost"] == 0.0357


def test_reconcile_skips_call_with_existing_end_of_call(events_file):
    """Idempotent: a call that already has an end_of_call event is not redone."""
    _write(events_file, [
        {"ts": _today_ts(), "kind": "dial_attempt", "outcome": "initiated",
         "call_id": "call_abc", "prospect_id": "pr_x"},
        {"ts": _today_ts(minute=19), "kind": "end_of_call", "call_id": "call_abc",
         "outcome": "voicemail_left"},
    ])
    written = []
    summary = rec.reconcile_recent_calls(
        fetch_call=lambda cid: pytest.fail("should not fetch"),
        append_event=written.append,
    )
    assert summary["candidates"] == 0
    assert summary["reconciled"] == 0
    assert written == []


def test_reconcile_skips_still_in_progress(events_file):
    _write(events_file, [
        {"ts": _today_ts(), "kind": "dial_attempt", "outcome": "initiated",
         "call_id": "call_live", "prospect_id": "pr_x"},
    ])
    written = []
    summary = rec.reconcile_recent_calls(
        fetch_call=lambda cid: {"status": "in-progress"},
        append_event=written.append,
    )
    assert summary["still_in_progress"] == 1
    assert summary["reconciled"] == 0
    assert written == []


def test_reconcile_failopen_on_fetch_error(events_file):
    _write(events_file, [
        {"ts": _today_ts(), "kind": "dial_attempt", "outcome": "initiated",
         "call_id": "call_x", "prospect_id": "pr_x"},
    ])
    written = []
    summary = rec.reconcile_recent_calls(
        fetch_call=lambda cid: None,  # simulate fetch failure
        append_event=written.append,
    )
    assert summary["fetch_failed"] == 1
    assert summary["reconciled"] == 0


def test_reconcile_ignores_non_initiated_dials(events_file):
    """A skipped/errored dial has no call to reconcile."""
    _write(events_file, [
        {"ts": _today_ts(), "kind": "dial_attempt", "outcome": "skipped_cooldown",
         "call_id": None, "prospect_id": "pr_x"},
        {"ts": _today_ts(), "kind": "dial_attempt", "outcome": "vapi_error",
         "call_id": None, "prospect_id": "pr_y"},
    ])
    summary = rec.reconcile_recent_calls(
        fetch_call=lambda cid: pytest.fail("no call to fetch"),
        append_event=lambda e: None,
    )
    assert summary["candidates"] == 0


# ---------------------------------------------------------------------------
# Host dial_runs as a SECOND candidate source (the data-loss fix).
# ---------------------------------------------------------------------------

def test_reconcile_backfills_host_dialed_call_from_dial_runs(events_file, dial_runs_dir):
    """A call placed by the HOST dialer is recorded ONLY in dial_runs/*.json,
    never in voice_events. It must still become a candidate and get backfilled."""
    events_file.write_text("", encoding="utf-8")  # empty voice_events
    _write_dial_run(dial_runs_dir, "dial_run_today", [
        {"outcome": "skipped_cooldown", "call_id": None, "prospect_id": "pr_skip"},
        {"outcome": "initiated", "call_id": "019f1abf", "prospect_id": "pr_yuba",
         "company": "Sample Dental Practice", "phone": "+15005550006",
         "outbound_number_id": "f775b297"},
    ])

    def fake_fetch(cid):
        assert cid == "019f1abf"
        return {"status": "ended", "endedReason": "voicemail", "cost": 0.04,
                "durationSeconds": 30, "analysis": {"summary": "VM left."}}

    written = []
    summary = rec.reconcile_recent_calls(
        fetch_call=fake_fetch, append_event=written.append,
    )
    assert summary["candidates"] == 1
    assert summary["reconciled"] == 1
    assert len(written) == 1
    ev = written[0]
    assert ev["call_id"] == "019f1abf"
    assert ev["outcome"] == "voicemail_left"
    assert ev["company"] == "Sample Dental Practice"
    assert ev["prospect_id"] == "pr_yuba"
    assert ev["outbound_number_id"] == "f775b297"
    assert ev["reconciled"] is True


def test_reconcile_dial_runs_call_with_existing_eoc_is_idempotent(events_file, dial_runs_dir):
    """A dial_runs call that ALREADY has an end_of_call event in voice_events
    is skipped (idempotency filter applies to the merged set)."""
    _write(events_file, [
        {"ts": _today_ts(minute=20), "kind": "end_of_call", "call_id": "019f1abf",
         "outcome": "voicemail_left"},
    ])
    _write_dial_run(dial_runs_dir, "dial_run_today", [
        {"outcome": "initiated", "call_id": "019f1abf", "prospect_id": "pr_yuba",
         "company": "Sample Dental Practice"},
    ])
    written = []
    summary = rec.reconcile_recent_calls(
        fetch_call=lambda cid: pytest.fail("should not fetch"),
        append_event=written.append,
    )
    assert summary["candidates"] == 0
    assert summary["reconciled"] == 0
    assert written == []


def test_reconcile_unions_voice_events_and_dial_runs(events_file, dial_runs_dir):
    """One candidate from voice_events + one from dial_runs both reconcile."""
    _write(events_file, [
        {"ts": _today_ts(), "kind": "dial_attempt", "outcome": "initiated",
         "call_id": "call_events", "prospect_id": "pr_ev", "company": "Events Co"},
    ])
    _write_dial_run(dial_runs_dir, "dial_run_today", [
        {"outcome": "initiated", "call_id": "call_dialrun", "prospect_id": "pr_dr",
         "company": "DialRun Co", "phone": "+15300000000", "outbound_number_id": "21f7"},
    ])

    def fake_fetch(cid):
        return {"status": "ended", "endedReason": "no-answer",
                "analysis": {}}

    written = []
    summary = rec.reconcile_recent_calls(
        fetch_call=fake_fetch, append_event=written.append,
    )
    assert summary["candidates"] == 2
    assert summary["reconciled"] == 2
    backfilled = {ev["call_id"] for ev in written}
    assert backfilled == {"call_events", "call_dialrun"}


def test_reconcile_missing_dial_runs_dir_falls_back_to_events_only(events_file, monkeypatch):
    """An unset/missing dial_runs dir must not crash — reconcile falls back to
    voice_events-only behavior."""
    monkeypatch.setenv("SAMUS_VOICE_DIAL_RUNS_PATH", "/nonexistent/dial_runs/xyz")
    _write(events_file, [
        {"ts": _today_ts(), "kind": "dial_attempt", "outcome": "initiated",
         "call_id": "call_only_events", "prospect_id": "pr_x", "company": "Only Events"},
    ])

    def fake_fetch(cid):
        assert cid == "call_only_events"
        return {"status": "ended", "endedReason": "voicemail", "analysis": {}}

    written = []
    summary = rec.reconcile_recent_calls(
        fetch_call=fake_fetch, append_event=written.append,
    )
    assert summary["candidates"] == 1
    assert summary["reconciled"] == 1
    assert written[0]["call_id"] == "call_only_events"


def test_reconcile_populates_duration_from_duration_seconds(events_file):
    _write(events_file, [
        {"ts": _today_ts(), "kind": "dial_attempt", "outcome": "initiated",
         "call_id": "call_d1", "prospect_id": "pr_x"},
    ])
    written = []
    rec.reconcile_recent_calls(
        fetch_call=lambda cid: {"status": "ended", "endedReason": "customer-ended-call",
                                "durationSeconds": 18},
        append_event=written.append,
    )
    assert written[0]["duration_seconds"] == 18.0


def test_reconcile_computes_duration_from_started_ended(events_file):
    """When durationSeconds is absent, derive it from endedAt - startedAt."""
    _write(events_file, [
        {"ts": _today_ts(), "kind": "dial_attempt", "outcome": "initiated",
         "call_id": "call_d2", "prospect_id": "pr_x"},
    ])
    written = []
    rec.reconcile_recent_calls(
        fetch_call=lambda cid: {
            "status": "ended", "endedReason": "customer-ended-call",
            "startedAt": "2026-07-01T18:00:00.000Z",
            "endedAt": "2026-07-01T18:00:23.000Z",
        },
        append_event=written.append,
    )
    assert written[0]["duration_seconds"] == 23.0


def test_reconcile_duration_from_ms_last_resort(events_file):
    _write(events_file, [
        {"ts": _today_ts(), "kind": "dial_attempt", "outcome": "initiated",
         "call_id": "call_d3", "prospect_id": "pr_x"},
    ])
    written = []
    rec.reconcile_recent_calls(
        fetch_call=lambda cid: {"status": "ended", "endedReason": "voicemail",
                                "durationMs": 42000},
        append_event=written.append,
    )
    assert written[0]["duration_seconds"] == 42.0


def test_duration_seconds_none_when_no_source():
    assert rec._duration_seconds({"status": "ended"}) is None
