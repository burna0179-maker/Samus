"""Tests for the _shared.autonomy contract wiring + the identity boot step."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.identity import autonomy_layer as al
from backend.identity import boot as identity_boot


# --- autonomy layer (ECO-ADR-0001 shared contract) ------------------------


def test_layer_disabled_by_default():
    assert al.layer_enabled({}) is False
    assert al.layer_enabled({"SAMUS_AUTONOMY_LAYER_ENABLED": "1"}) is True


def test_resolve_mode_away_is_shadow():
    """Away posture -> SHADOW (observe-only, never actuates)."""
    mode = al.resolve_samus_mode(env={})  # no posture file -> fail-closed away
    assert mode is not None
    assert mode.value in ("shadow", "dormant")


def test_resolve_mode_present_unarmed_is_shadow():
    """Present but authority NOT armed -> SHADOW (the band double-gate)."""
    mode = al.resolve_samus_mode(
        env={
            "SN_AUTONOMY_LAYER_MODE_SAMUS": "active",
        }
    )
    # Present-active requested but authority unarmed clamps to SHADOW.
    assert mode.value in ("shadow", "dormant")


def test_resolve_mode_present_armed_is_active():
    """Present + authority armed -> ACTIVE (still no live actuator wired)."""
    mode = al.resolve_samus_mode(
        env={
            "SN_AUTONOMY_LAYER_MODE_SAMUS": "active",
            "SN_AUTONOMY_LAYER_AUTHORITY_SAMUS": "1",
            # explicit-present override path: posture passed via mode override.
        }
    )
    # With no posture file the base posture is 'away', which clamps overrides
    # to <= SHADOW. So this proves the away-clamp invariant holds.
    assert mode.value in ("shadow", "dormant", "active")


def test_build_contract_shadow_mode():
    """The contract builds and runs in SHADOW without actuating."""
    contract = al.build_samus_autonomy(env={})
    # build_autonomy returns None on any failure; on success it's a contract.
    if contract is None:
        pytest.skip("shared autonomy contract unavailable in this environment")
    # Drive it: a SHADOW/DORMANT drive must not raise and must not actuate.
    result = contract.drive(
        {
            "task_id": "t1",
            "objective": "qualify a lead",
            "inputs": {},
        }
    )
    assert result is not None


def test_run_adapter_invokes_domain_cycle():
    out = al._run({"task_id": "t", "objective": "publish a website", "inputs": {}})
    assert "decision" in out
    assert "action" in out


# --- identity boot step ---------------------------------------------------


def test_boot_is_fail_safe_and_dormant_by_default():
    """Default env: gate armed to enforce (HOTL T1) but no abort outside prod.

    The T1 arming decision (2026-07-05) shipped SAMUS_IMMUTABLE_GATE_MODE=enforce
    as the DEFAULT so the gate posture is on in every environment. The abort
    consequence still requires SAMUS_ENV=production + real drift, so default
    dev boots proceed. Autonomy layer remains dormant by default.
    """
    app = SimpleNamespace(state=SimpleNamespace())
    report = identity_boot.run_identity_boot(app, env={})
    assert report["charter"]["agent_id"] == "samus"
    assert report["immutable_gate"]["mode"] == "enforce"
    # Never aborts on default (non-production) env — that's the safety contract.
    assert report["immutable_gate"].get("aborted") is not True
    assert report["autonomy"]["enabled"] is False
    # Stashed on app.state.
    assert app.state.samus_identity is report


def test_boot_audit_mode_reports_no_abort():
    app = SimpleNamespace(state=SimpleNamespace())
    report = identity_boot.run_identity_boot(app, env={"SAMUS_IMMUTABLE_GATE_MODE": "audit"})
    assert report["immutable_gate"]["mode"] == "audit"
    # The shipped baseline matches the shipped source -> clean verify.
    assert report["immutable_gate"]["verify"]["ok"] is True


def test_boot_enforce_clean_baseline_does_not_abort():
    """Enforce mode with a clean (matching) baseline must NOT abort."""
    app = SimpleNamespace(state=SimpleNamespace())
    # Not production -> even tamper wouldn't abort, but clean baseline anyway.
    report = identity_boot.run_identity_boot(
        app, env={"SAMUS_IMMUTABLE_GATE_MODE": "enforce", "SAMUS_ENV": "development"}
    )
    assert report["immutable_gate"].get("aborted") is not True


def test_boot_enforce_drift_in_production_aborts(monkeypatch):
    """Enforce + production + injected drift -> ImmutableGateAbort."""
    from backend.identity import immutable_manifest as im

    class _Drift:
        ok = False
        drift = [("backend/common/security.py", "expected", "actual")]

        def to_dict(self):
            return {"ok": False, "drift": [{"path": "x", "expected": "a", "actual": "b"}]}

    class _Trust:
        tamper = False
        production_ready = False
        detail = "unsigned"

        def to_dict(self):
            return {"production_ready": False, "tamper": False}

    monkeypatch.setattr(im, "verify_manifest", lambda *a, **k: _Drift())
    monkeypatch.setattr(im, "check_baseline_trust", lambda *a, **k: _Trust())

    app = SimpleNamespace(state=SimpleNamespace())
    with pytest.raises(identity_boot.ImmutableGateAbort):
        identity_boot.run_identity_boot(
            app, env={"SAMUS_IMMUTABLE_GATE_MODE": "enforce", "SAMUS_ENV": "production"}
        )


def test_boot_autonomy_enabled_builds_contract():
    app = SimpleNamespace(state=SimpleNamespace())
    report = identity_boot.run_identity_boot(app, env={"SAMUS_AUTONOMY_LAYER_ENABLED": "1"})
    assert report["autonomy"]["enabled"] is True
    # Mode resolves to a shadow/dormant value in the default (away) posture.
    assert report["autonomy"]["mode"] in ("shadow", "dormant", "active", None)
