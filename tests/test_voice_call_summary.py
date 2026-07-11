"""get_voice_call_summary — rollup of voice_events.jsonl for the morning briefing."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from backend.voice.service import get_voice_call_summary


def _write_event_lines(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def _ts(offset_hours: float = 0.0) -> str:
    now = datetime.now(timezone.utc) - timedelta(hours=offset_hours)
    return now.strftime("%Y-%m-%dT%H:%M:%SZ")


def test_get_voice_call_summary_no_log(tmp_path, monkeypatch):
    monkeypatch.setenv("SAMUS_VOICE_EVENTS_PATH", str(tmp_path / "nope.jsonl"))
    summary = get_voice_call_summary(window_hours=24)
    assert summary.log_loaded is False
    assert summary.total_calls == 0
    assert summary.dial_attempt_count == 0
    assert summary.booked_calls == []


def test_get_voice_call_summary_filters_window(tmp_path, monkeypatch):
    events = tmp_path / "voice_events.jsonl"
    rows = [
        # in window
        {"ts": _ts(1), "kind": "dial_attempt", "outcome": "initiated",
         "prospect_id": "p1", "call_id": "c1"},
        # stale (48h ago)
        {"ts": _ts(48), "kind": "dial_attempt", "outcome": "initiated",
         "prospect_id": "p_old", "call_id": "c_old"},
    ]
    _write_event_lines(events, rows)
    monkeypatch.setenv("SAMUS_VOICE_EVENTS_PATH", str(events))
    summary = get_voice_call_summary(window_hours=24)
    assert summary.dial_attempt_count == 1
    assert summary.dial_attempts_by_outcome == {"initiated": 1}
    # initiated_count derives from outcome='initiated'
    assert summary.initiated_count == 1


def test_get_voice_call_summary_aggregates_end_of_call(tmp_path, monkeypatch):
    events = tmp_path / "voice_events.jsonl"
    rows = [
        {"ts": _ts(2), "kind": "dial_attempt", "outcome": "initiated",
         "prospect_id": "p1", "call_id": "c1"},
        {"ts": _ts(1.9), "kind": "end_of_call",
         "call_id": "c1", "company": "Acme",
         "tier": "high", "intent_score": 82,
         "recommended_action": "book_call"},
        {"ts": _ts(1.5), "kind": "end_of_call",
         "call_id": "c2", "tier": "medium", "intent_score": 50,
         "recommended_action": "follow_up"},
        {"ts": _ts(1.0), "kind": "end_of_call",
         "call_id": "c3", "tier": "low", "intent_score": 20,
         "recommended_action": "disqualify"},
    ]
    _write_event_lines(events, rows)
    monkeypatch.setenv("SAMUS_VOICE_EVENTS_PATH", str(events))
    summary = get_voice_call_summary(window_hours=24)
    assert summary.dial_attempt_count == 1
    assert summary.initiated_count == 1
    assert summary.end_of_call_count == 3
    assert summary.total_calls == 4
    assert summary.by_recommended_action["book_call"] == 1
    assert summary.by_recommended_action["follow_up"] == 1
    assert summary.by_recommended_action["disqualify"] == 1
    assert summary.by_tier == {"high": 1, "medium": 1, "low": 1}
    assert summary.avg_intent_score == 50.7
    assert len(summary.booked_calls) == 1
    assert summary.booked_calls[0].call_id == "c1"
    assert summary.booked_calls[0].intent_score == 82
    assert summary.booked_calls[0].tier == "high"
    assert summary.booked_calls[0].company == "Acme"


def test_get_voice_call_summary_aggregates_dial_outcomes(tmp_path, monkeypatch):
    """Dialer rollup buckets every outcome (initiated / dry_run / skipped_*)."""
    events = tmp_path / "voice_events.jsonl"
    rows = [
        {"ts": _ts(1), "kind": "dial_attempt", "outcome": "initiated",
         "prospect_id": "p1"},
        {"ts": _ts(1), "kind": "dial_attempt", "outcome": "initiated",
         "prospect_id": "p2"},
        {"ts": _ts(1), "kind": "dial_attempt", "outcome": "skipped_hours",
         "prospect_id": "p3"},
        {"ts": _ts(1), "kind": "dial_attempt", "outcome": "skipped_phone",
         "prospect_id": "p4"},
        {"ts": _ts(1), "kind": "dial_attempt", "outcome": "vapi_error",
         "prospect_id": "p5"},
    ]
    _write_event_lines(events, rows)
    monkeypatch.setenv("SAMUS_VOICE_EVENTS_PATH", str(events))
    summary = get_voice_call_summary(window_hours=24)
    assert summary.dial_attempt_count == 5
    assert summary.dial_attempts_by_outcome == {
        "initiated": 2,
        "skipped_hours": 1,
        "skipped_phone": 1,
        "vapi_error": 1,
    }
    # initiated_count == count of 'initiated' outcome
    assert summary.initiated_count == 2


def test_get_voice_call_summary_handles_missing_fields(tmp_path, monkeypatch):
    """An end_of_call with no tier / no intent / no recommended_action should
    not crash; bucket under '(none)' and skip avg."""
    events = tmp_path / "voice_events.jsonl"
    rows = [{"ts": _ts(1), "kind": "end_of_call", "call_id": "c1"}]
    _write_event_lines(events, rows)
    monkeypatch.setenv("SAMUS_VOICE_EVENTS_PATH", str(events))
    summary = get_voice_call_summary(window_hours=24)
    assert summary.end_of_call_count == 1
    assert summary.avg_intent_score is None
    assert summary.by_recommended_action.get("(none)") == 1
    assert summary.by_tier.get("(none)") == 1
    assert summary.booked_calls == []
    assert summary.recent_high_intent == []


def test_get_voice_call_summary_skips_malformed_lines(tmp_path, monkeypatch):
    events = tmp_path / "voice_events.jsonl"
    events.parent.mkdir(parents=True, exist_ok=True)
    events.write_text(
        '\n'.join([
            '{"not": "valid"',
            'not json at all',
            json.dumps({"ts": _ts(1), "kind": "dial_attempt",
                        "outcome": "initiated", "prospect_id": "p1"}),
        ]),
        encoding="utf-8",
    )
    monkeypatch.setenv("SAMUS_VOICE_EVENTS_PATH", str(events))
    summary = get_voice_call_summary(window_hours=24)
    assert summary.dial_attempt_count == 1
