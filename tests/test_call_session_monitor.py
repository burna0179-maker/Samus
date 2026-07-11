"""Intraday session monitor — streak trigger + callsheet-rewrite path.

Gap-3 from the autonomous-call-loop audit. The monitor was wired but had
ZERO test coverage; arming it without a regression net was a QA gap. These
tests validate the core capability in isolation (tmp event log + intel dir,
mocked LLM) so the live arming is backed by a net.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone

import pytest

import backend.voice.call_session_monitor as csm


def _today_ts(hour: int, minute: int = 0) -> str:
    d = date.today()
    return datetime(d.year, d.month, d.day, hour, minute,
                    tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _end_of_call(company: str, outcome: str, hour: int) -> dict:
    return {
        "kind": "end_of_call",
        "ts": _today_ts(hour),
        "company": company,
        "outcome": outcome,
        "ended_reason": "customer-ended-call",
        "duration_sec": 30,
    }


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    """Tmp event log + artifact root + monitor armed."""
    events_path = tmp_path / "voice_events.jsonl"
    monkeypatch.setenv("SAMUS_VOICE_EVENTS_PATH", str(events_path))
    monkeypatch.setenv("SAMUS_ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    monkeypatch.setenv("SAMUS_VOICE_SESSION_MONITOR", "1")
    monkeypatch.setenv("SAMUS_SESSION_STREAK_THRESHOLD", "3")
    monkeypatch.setenv("SAMUS_SESSION_MIN_CALLS", "3")
    # No cooldown interference.
    monkeypatch.setenv("SAMUS_SESSION_COOLDOWN_S", "0")
    return tmp_path, events_path


def _write_events(events_path, events: list[dict]):
    with events_path.open("w", encoding="utf-8") as fh:
        for e in events:
            fh.write(json.dumps(e) + "\n")


def test_enabled_reads_flag(monkeypatch):
    monkeypatch.setenv("SAMUS_VOICE_SESSION_MONITOR", "1")
    assert csm._is_enabled() is True
    monkeypatch.setenv("SAMUS_VOICE_SESSION_MONITOR", "0")
    assert csm._is_enabled() is False


def test_streak_trigger_fires_and_rewrites_callsheet(isolated, monkeypatch):
    """3 consecutive non-positive outcomes → STREAK trigger → mid-session
    adjustment generated → applied to the callsheet intel."""
    _tmp, events_path = isolated
    _write_events(events_path, [
        _end_of_call("Acme Plumbing", "not_interested", 9),
        _end_of_call("Bar HVAC", "no_answer", 10),
        _end_of_call("Baz Roofing", "not_interested", 11),
    ])

    # Mock the LLM so we don't depend on LM Studio.
    fake_response = (
        "DIAGNOSIS:\nThe opener is landing as a sales pitch and prospects "
        "disengage immediately.\n\n"
        "ADJUSTMENTS:\n"
        "1. Lead with a specific local observation, not a generic pitch.\n"
        "2. Ask permission to continue within the first 10 seconds.\n"
        "3. Drop the price anchor until interest is confirmed.\n"
    )
    monkeypatch.setattr(csm, "llm_chat", lambda **kw: fake_response)

    captured = {}
    real_apply = csm._apply_to_callsheet

    def _spy_apply(adj):
        captured["applied"] = adj
        return real_apply(adj)
    monkeypatch.setattr(csm, "_apply_to_callsheet", _spy_apply)

    adj = csm.check_session(force=True)

    assert adj is not None, "STREAK should have fired"
    assert adj.trigger == "STREAK"
    assert "3 consecutive" in adj.trigger_detail
    assert len(adj.recommendations) == 3
    assert adj.applied_to_callsheet is True
    assert "applied" in captured  # the rewrite path actually ran


def test_no_trigger_when_outcomes_mixed(isolated, monkeypatch):
    """A positive outcome resets the streak — no trigger fires."""
    _tmp, events_path = isolated
    _write_events(events_path, [
        _end_of_call("Acme", "not_interested", 9),
        _end_of_call("Bar", "converted", 10),   # resets streak
        _end_of_call("Baz", "not_interested", 11),
    ])
    monkeypatch.setattr(csm, "llm_chat", lambda **kw: "should not be called")
    adj = csm.check_session(force=True)
    assert adj is None


def test_min_calls_floor_blocks_premature_check(isolated, monkeypatch):
    """Below the min-calls floor, the monitor no-ops even with a streak."""
    _tmp, events_path = isolated
    _write_events(events_path, [
        _end_of_call("Acme", "not_interested", 9),
        _end_of_call("Bar", "not_interested", 10),
    ])  # only 2 calls, floor is 3
    monkeypatch.setattr(csm, "llm_chat", lambda **kw: "nope")
    adj = csm.check_session(force=True)
    assert adj is None


def test_disabled_flag_no_ops_without_force(isolated, monkeypatch):
    """With the flag OFF and force=False, check_session is a pure no-op."""
    _tmp, events_path = isolated
    monkeypatch.setenv("SAMUS_VOICE_SESSION_MONITOR", "0")
    _write_events(events_path, [
        _end_of_call("Acme", "not_interested", 9),
        _end_of_call("Bar", "no_answer", 10),
        _end_of_call("Baz", "not_interested", 11),
    ])
    monkeypatch.setattr(csm, "llm_chat", lambda **kw: "nope")
    assert csm.check_session(force=False) is None


def test_llm_empty_response_skips_callsheet_apply(isolated, monkeypatch):
    """An empty LLM response records llm_error and does NOT corrupt the
    callsheet with empty recommendations."""
    _tmp, events_path = isolated
    _write_events(events_path, [
        _end_of_call("Acme", "not_interested", 9),
        _end_of_call("Bar", "no_answer", 10),
        _end_of_call("Baz", "not_interested", 11),
    ])
    monkeypatch.setattr(csm, "llm_chat", lambda **kw: "")
    adj = csm.check_session(force=True)
    assert adj is not None
    assert adj.llm_error == "lm_studio_empty_response"
    assert adj.applied_to_callsheet is False
