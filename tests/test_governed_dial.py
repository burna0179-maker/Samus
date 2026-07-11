"""Tests for backend.voice.governed_dial.place_governed_dial (ADR-016 actuator).

Fail-closed at every step: local fence check -> Codex ratify -> dial only on
allow. The Codex is the REAL validator (global registry primed); the dialer is a
fake so no live call is placed."""
from __future__ import annotations

from pathlib import Path

import pytest

from backend.common.codex import adr_drafter
from backend.common.codex.registry import REGISTRY
from backend.voice.governed_dial import place_governed_dial

REPO_ROOT = Path(__file__).resolve().parents[1]
CODEX_DIR = REPO_ROOT / "docs" / "codex"


@pytest.fixture
def codex_ready(monkeypatch, tmp_path):
    REGISTRY.load(CODEX_DIR)  # prime the global registry check_action() uses
    # keep ADR-draft writes (on a block) out of the real _drafts dir
    original = adr_drafter.draft_adr_for_violation

    def _wrapped(action, violated_rule_id, reason, registry, drafts_dir=None):
        return original(action, violated_rule_id, reason, registry,
                        drafts_dir=drafts_dir or (tmp_path / "_drafts"))

    monkeypatch.setattr(adr_drafter, "draft_adr_for_violation", _wrapped)
    from backend.common.codex import validator as _v
    monkeypatch.setattr(_v, "draft_adr_for_violation", _wrapped)


def _arm(monkeypatch, on: bool) -> None:
    from backend.common.settings import reload_settings
    if on:
        monkeypatch.setenv("SAMUS_GOVERNED_AUTONOMOUS_DIAL_ENABLED", "true")
    else:
        monkeypatch.delenv("SAMUS_GOVERNED_AUTONOMOUS_DIAL_ENABLED", raising=False)
    reload_settings()


class _FakeDialer:
    def __init__(self, raises=False):
        self.calls = []
        self.raises = raises

    def __call__(self, **kw):
        if self.raises:
            raise RuntimeError("vapi boom")
        self.calls.append(kw)
        return {"call_id": "c1", "prospect_id": kw.get("prospect_id")}


_PASS = {"within_call_hours": True, "cooldown_ok": True,
         "under_daily_cap": True, "dnc_ok": True, "consent_ok": True}
_STAKE = "Alex flagged Acme because their trust grade is F."


def test_dials_when_armed_and_all_fences_pass(codex_ready, monkeypatch):
    _arm(monkeypatch, on=True)
    d = _FakeDialer()
    r = place_governed_dial(prospect_id="p1", stake_sentence=_STAKE,
                            fences=dict(_PASS), dial_fn=d, phone="+15555550000")
    assert r.dialed is True and r.blocked is False
    assert len(d.calls) == 1 and d.calls[0]["prospect_id"] == "p1"


def test_blocked_when_unarmed_and_dialer_untouched(codex_ready, monkeypatch):
    _arm(monkeypatch, on=False)  # default posture
    d = _FakeDialer()
    r = place_governed_dial(prospect_id="p1", stake_sentence=_STAKE,
                            fences=dict(_PASS), dial_fn=d)
    assert r.dialed is False and r.blocked is True
    assert r.rule == "VR-G5"
    assert d.calls == []  # dialer NEVER touched when the Codex blocks


@pytest.mark.parametrize("fence", list(_PASS))
def test_blocked_locally_when_a_fence_false_codex_not_consulted(fence, codex_ready, monkeypatch):
    _arm(monkeypatch, on=True)
    d = _FakeDialer()
    bad = dict(_PASS)
    bad[fence] = False
    r = place_governed_dial(prospect_id="p1", stake_sentence=_STAKE, fences=bad, dial_fn=d)
    assert r.dialed is False and r.rule == "FENCE"
    assert d.calls == []


def test_blocked_when_fence_missing(codex_ready, monkeypatch):
    _arm(monkeypatch, on=True)
    d = _FakeDialer()
    partial = dict(_PASS)
    del partial["dnc_ok"]
    r = place_governed_dial(prospect_id="p1", stake_sentence=_STAKE, fences=partial, dial_fn=d)
    assert r.dialed is False and r.rule == "FENCE"
    assert d.calls == []


def test_blocked_missing_stake(codex_ready, monkeypatch):
    _arm(monkeypatch, on=True)
    d = _FakeDialer()
    r = place_governed_dial(prospect_id="p1", stake_sentence="  ", fences=dict(_PASS), dial_fn=d)
    assert r.dialed is False and r.rule == "G1"
    assert d.calls == []


def test_dial_error_is_blocked_not_raised(codex_ready, monkeypatch):
    _arm(monkeypatch, on=True)
    d = _FakeDialer(raises=True)
    r = place_governed_dial(prospect_id="p1", stake_sentence=_STAKE, fences=dict(_PASS), dial_fn=d)
    assert r.dialed is False and r.blocked is False and r.rule == "DIAL_ERROR"
