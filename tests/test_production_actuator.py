"""Tests for the production actuator (idle-drive's ACT layer).

Routing is pure; orchestration runs with injected fakes so no real send/call
fires. Central invariant (ADR-017): a prospect with no consent basis is NEVER
live-dialed — it becomes a voicemail draft."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.cash_engine.production_actuator import (
    ProductionDeps,
    _route_voice,
    run_production,
)

_FENCES_PASS = {"within_call_hours": True, "cooldown_ok": True,
                "under_daily_cap": True, "dnc_ok": True}

_CODEX_DIR = Path(__file__).resolve().parents[1] / "docs" / "codex"


def _arm_governed_dial(monkeypatch):
    """Arm the governed-dial flag + prime the Codex registry so place_governed_dial
    can reach the injected dial_fn (otherwise VR-G5 blocks before it)."""
    from backend.common.codex.registry import REGISTRY
    from backend.common.settings import reload_settings
    REGISTRY.load(_CODEX_DIR)
    monkeypatch.setenv("SAMUS_GOVERNED_AUTONOMOUS_DIAL_ENABLED", "true")
    reload_settings()


# --- pure routing ------------------------------------------------------------

def test_route_no_consent_is_voicemail_even_if_fenced():
    f = {**_FENCES_PASS, "consent_ok": False}
    assert _route_voice(f) == "voicemail"


def test_route_consent_plus_all_fences_is_dial():
    f = {**_FENCES_PASS, "consent_ok": True}
    assert _route_voice(f) == "dial"


@pytest.mark.parametrize("fence", list(_FENCES_PASS))
def test_route_consent_but_one_fence_false_is_skip(fence):
    f = {**_FENCES_PASS, "consent_ok": True, fence: False}
    assert _route_voice(f) == "skip"


# --- orchestration with fakes ------------------------------------------------

def _prospect(pid, phone="+15555550000"):
    return SimpleNamespace(prospect_id=pid, phone=phone, state="CA",
                           callsheet_opener=f"Alex flagged {pid}.",
                           callsheet_voicemail=f"VM for {pid}")


def _deps(*, candidates, consent, dial_fn=None, email=None):
    drafted = []
    dialed = []

    def _draft(p):
        drafted.append(p.prospect_id)
        return {"drafted": True, "path": f"/x/{p.prospect_id}.txt"}

    def _dial(**kw):
        dialed.append(kw["prospect_id"])
        return {"call_id": "c1"}

    d = ProductionDeps(
        voice_candidates=lambda: list(candidates),
        classify_consent=lambda p: consent.get(p.prospect_id, False),
        compute_fences=lambda p: dict(_FENCES_PASS),
        draft_voicemail=_draft,
        email_batch=email or (lambda cap: {"sent": 0, "failed": 0}),
        dial_fn=(dial_fn if dial_fn is not None else _dial),
    )
    return d, drafted, dialed


_DECISION = SimpleNamespace(reason="idle 90m during business hours; pace unknown")


def test_cold_prospects_all_become_voicemail_never_dial():
    cands = [_prospect("cold1"), _prospect("cold2")]
    deps, drafted, dialed = _deps(candidates=cands, consent={})  # nobody consents
    out = run_production(_DECISION, deps=deps, cap_voice=5)
    assert out["voice"]["drafted"] == 2 and out["voice"]["dialed"] == 0
    assert dialed == []  # the dialer is NEVER invoked for cold prospects
    assert set(drafted) == {"cold1", "cold2"}


def test_consented_and_fenced_prospect_is_dialed(monkeypatch):
    _arm_governed_dial(monkeypatch)  # the producer never bypasses the Codex gate
    cands = [_prospect("warm1")]
    deps, drafted, dialed = _deps(candidates=cands, consent={"warm1": True})
    out = run_production(_DECISION, deps=deps, cap_voice=5)
    assert out["voice"]["dialed"] == 1 and dialed == ["warm1"]
    assert drafted == []


def test_consented_prospect_not_dialed_when_governed_flag_off(monkeypatch):
    """The actuator defers to the Codex: consented + fenced, but the governed
    policy is disarmed -> place_governed_dial blocks, dial_fn never reached."""
    monkeypatch.delenv("SAMUS_GOVERNED_AUTONOMOUS_DIAL_ENABLED", raising=False)
    from backend.common.settings import reload_settings
    reload_settings()
    cands = [_prospect("warm1")]
    deps, drafted, dialed = _deps(candidates=cands, consent={"warm1": True})
    out = run_production(_DECISION, deps=deps, cap_voice=5)
    assert out["voice"]["dialed"] == 0 and dialed == []
    assert out["voice"]["blocked"] == 1  # codex refused, not actuated


def test_consented_but_no_dialer_bound_is_skip_not_dial():
    cands = [_prospect("warm1")]
    # dial_fn=None: consented + ready but no live dialer -> counted skipped.
    deps = ProductionDeps(
        voice_candidates=lambda: cands,
        classify_consent=lambda p: True,
        compute_fences=lambda p: dict(_FENCES_PASS),
        draft_voicemail=lambda p: {"drafted": True},
        email_batch=lambda cap: {"sent": 0, "failed": 0},
        dial_fn=None,
    )
    out = run_production(_DECISION, deps=deps, cap_voice=5)
    assert out["voice"]["dialed"] == 0 and out["voice"]["skipped"] == 1


def test_consented_but_fence_closed_is_skip():
    cands = [_prospect("warm1")]
    deps = ProductionDeps(
        voice_candidates=lambda: cands,
        classify_consent=lambda p: True,
        compute_fences=lambda p: {**_FENCES_PASS, "dnc_ok": False},  # on DNC
        draft_voicemail=lambda p: {"drafted": True},
        email_batch=lambda cap: {"sent": 0, "failed": 0},
        dial_fn=lambda **kw: {"call_id": "x"},
    )
    out = run_production(_DECISION, deps=deps, cap_voice=5)
    assert out["voice"]["skipped"] == 1 and out["voice"]["dialed"] == 0


def test_voice_cap_bounds_drafts():
    cands = [_prospect(f"c{i}") for i in range(10)]
    deps, drafted, _ = _deps(candidates=cands, consent={})
    out = run_production(_DECISION, deps=deps, cap_voice=3)
    assert out["voice"]["drafted"] == 3  # capped


def test_email_channel_counts_surface():
    deps, _, _ = _deps(candidates=[], consent={},
                       email=lambda cap: {"sent": 4, "failed": 1})
    out = run_production(_DECISION, deps=deps)
    assert out["email"] == {"sent": 4, "failed": 1}
    assert out["initiated"] == 4  # 0 voice + 4 email


def test_email_fault_does_not_sink_voice_channel():
    cands = [_prospect("cold1")]

    def _boom(cap):
        raise RuntimeError("smtp down")

    deps, drafted, _ = _deps(candidates=cands, consent={}, email=_boom)
    out = run_production(_DECISION, deps=deps)
    assert "error" in out["email"] and out["voice"]["drafted"] == 1  # voice still ran


def test_one_bad_prospect_does_not_sink_the_pass():
    good = _prospect("cold1")
    bad = _prospect("cold2")
    calls = {"n": 0}

    def _draft(p):
        if p.prospect_id == "cold2":
            raise RuntimeError("draft blew up")
        return {"drafted": True}

    deps = ProductionDeps(
        voice_candidates=lambda: [bad, good],
        classify_consent=lambda p: False,
        compute_fences=lambda p: dict(_FENCES_PASS),
        draft_voicemail=_draft,
        email_batch=lambda cap: {"sent": 0, "failed": 0},
        dial_fn=None,
    )
    out = run_production(_DECISION, deps=deps, cap_voice=5)
    # bad prospect counted as skipped (error path), good one still drafted
    assert out["voice"]["drafted"] == 1 and out["voice"]["skipped"] == 1
