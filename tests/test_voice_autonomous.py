"""B2 + C — mid-call adaptation bridge + dormant autonomous dial gate.

Pins: (1) mid-call adaptation is dormant by default and only fires on a final
user turn when enabled; (2) the autonomous dial can NEVER place a live call —
it is fenced by the dormant flag AND, even when the flag is on, by the Codex
VR-G5 (ADR-002) block, and a Codex outage is itself fail-closed.
"""
from __future__ import annotations

import pytest

from backend.voice import autonomous
from backend.voice.models import VapiWebhookMessage


def _msg(**kw) -> VapiWebhookMessage:
    kw.setdefault("type", "transcript")
    return VapiWebhookMessage(**kw)


# ---------------------------------------------------------------------------
# B2 — process_midcall_transcript
# ---------------------------------------------------------------------------

def test_midcall_dormant_by_default(monkeypatch):
    monkeypatch.delenv("SAMUS_VOICE_MIDCALL_ENABLED", raising=False)
    assert autonomous.process_midcall_transcript(
        _msg(transcript="I'm not sure about the price", role="user", transcriptType="final")
    ) is None


def test_midcall_enabled_adapts_final_user_turn(monkeypatch):
    monkeypatch.setenv("SAMUS_VOICE_MIDCALL_ENABLED", "1")
    out = autonomous.process_midcall_transcript(
        _msg(transcript="this sounds great, how do we start?",
             role="user", transcriptType="final"),
    )
    assert out is not None
    assert out["adapted"] is True
    for key in ("sentiment", "tone", "pacing", "strategy"):
        assert key in out


def test_midcall_ignores_non_transcript_type(monkeypatch):
    monkeypatch.setenv("SAMUS_VOICE_MIDCALL_ENABLED", "1")
    assert autonomous.process_midcall_transcript(
        _msg(type="status-update", transcript="x", role="user", transcriptType="final")
    ) is None


def test_midcall_ignores_empty_transcript(monkeypatch):
    monkeypatch.setenv("SAMUS_VOICE_MIDCALL_ENABLED", "1")
    assert autonomous.process_midcall_transcript(_msg(transcript="   ")) is None


def test_midcall_ignores_assistant_role(monkeypatch):
    monkeypatch.setenv("SAMUS_VOICE_MIDCALL_ENABLED", "1")
    assert autonomous.process_midcall_transcript(
        _msg(transcript="hello there", role="assistant", transcriptType="final")
    ) is None


def test_midcall_ignores_partial_transcript(monkeypatch):
    monkeypatch.setenv("SAMUS_VOICE_MIDCALL_ENABLED", "1")
    assert autonomous.process_midcall_transcript(
        _msg(transcript="I was thinking", role="user", transcriptType="partial")
    ) is None


def test_midcall_adds_next_action_when_autonomous_on_with_intel(monkeypatch):
    monkeypatch.setenv("SAMUS_VOICE_MIDCALL_ENABLED", "1")
    monkeypatch.setenv("SAMUS_AUTONOMOUS_CLOSER_ENABLED", "1")
    out = autonomous.process_midcall_transcript(
        _msg(transcript="tell me more", role="user", transcriptType="final"),
        intel={"products": {"primary": "seo", "secondary": "ads"}},
        current_state="engage",
    )
    assert out is not None
    assert out["next_action"] == "ask_question"   # engage -> ask_question
    assert "next_state" in out


# ---------------------------------------------------------------------------
# C — attempt_autonomous_dial (NEVER dials)
# ---------------------------------------------------------------------------

def test_dial_refused_when_dormant(monkeypatch):
    monkeypatch.delenv("SAMUS_AUTONOMOUS_CLOSER_ENABLED", raising=False)
    res = autonomous.attempt_autonomous_dial("p1", phone="+15555550100")
    assert res["dialed"] is False
    assert res["blocked"] is True
    assert res["rule"] == "DORMANT"


def test_dial_blocked_by_vr_g5_even_when_enabled(monkeypatch):
    # Flag ON — the Codex (VR-G5 / ADR-002) must still block the dial. The
    # codex registry is loaded by the conftest session fixture.
    monkeypatch.setenv("SAMUS_AUTONOMOUS_CLOSER_ENABLED", "1")
    res = autonomous.attempt_autonomous_dial("p1", phone="+15555550100")
    assert res["dialed"] is False
    assert res["blocked"] is True
    assert res["rule"] == "VR-G5"


def test_dial_fail_closed_when_codex_unavailable(monkeypatch):
    monkeypatch.setenv("SAMUS_AUTONOMOUS_CLOSER_ENABLED", "1")
    import backend.common.codex as codex

    def _raise(_action):
        raise codex.CodexUnavailable("registry down")

    monkeypatch.setattr(codex, "check_action", _raise)
    res = autonomous.attempt_autonomous_dial("p1")
    assert res["dialed"] is False
    assert res["blocked"] is True
    assert res["rule"] == "CODEX_UNAVAILABLE"


# ---------------------------------------------------------------------------
# run_autonomous_closer_cycle
# ---------------------------------------------------------------------------

def test_cycle_dormant_by_default(monkeypatch):
    monkeypatch.delenv("SAMUS_AUTONOMOUS_CLOSER_ENABLED", raising=False)
    out = autonomous.run_autonomous_closer_cycle([{"prospect_id": "p1"}])
    assert out["ran"] is False
    assert out["attempts"] == []


def test_cycle_enabled_dials_nothing(monkeypatch):
    monkeypatch.setenv("SAMUS_AUTONOMOUS_CLOSER_ENABLED", "1")
    out = autonomous.run_autonomous_closer_cycle(
        [{"prospect_id": "p1"}, {"prospect_id": "p2"}],
    )
    assert out["ran"] is True
    assert out["dialed_count"] == 0          # NEVER dials
    assert out["blocked_count"] == 2
    assert all(a["blocked"] for a in out["attempts"])
