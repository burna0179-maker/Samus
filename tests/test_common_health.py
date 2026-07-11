"""Canonical 4-state /health surface — registry + worst-state aggregation."""
from __future__ import annotations

from pathlib import Path

import pytest

from backend.common import audit_ledger, health


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch):
    path = tmp_path / "audit.jsonl"
    monkeypatch.setenv("SAMUS_AUDIT_LEDGER_PATH", str(path))
    audit_ledger.reset_default_ledger()
    health.reset_default_registry()
    yield
    audit_ledger.reset_default_ledger()
    health.reset_default_registry()


def test_empty_registry_is_ok() -> None:
    reg = health.HealthRegistry()
    out = reg.probe()
    assert out["state"] == health.HealthState.OK
    assert out["probes"] == {}


def test_registry_aggregates_worst_state() -> None:
    reg = health.HealthRegistry()
    reg.register("a", lambda: health.ProbeResult("a", health.HealthState.OK))
    reg.register("b", lambda: health.ProbeResult("b", health.HealthState.DEGRADED))
    reg.register("c", lambda: health.ProbeResult("c", health.HealthState.OK))
    out = reg.probe()
    assert out["state"] == health.HealthState.DEGRADED


def test_awaiting_operator_action_outranks_degraded() -> None:
    reg = health.HealthRegistry()
    reg.register("a", lambda: health.ProbeResult("a", health.HealthState.DEGRADED))
    reg.register("b", lambda: health.ProbeResult("b", health.HealthState.AWAITING_OPERATOR_ACTION))
    out = reg.probe()
    assert out["state"] == health.HealthState.AWAITING_OPERATOR_ACTION


def test_fail_outranks_awaiting_operator_action() -> None:
    reg = health.HealthRegistry()
    reg.register("a", lambda: health.ProbeResult("a", health.HealthState.AWAITING_OPERATOR_ACTION))
    reg.register("b", lambda: health.ProbeResult("b", health.HealthState.FAIL))
    out = reg.probe()
    assert out["state"] == health.HealthState.FAIL


def test_raising_probe_is_recorded_as_fail() -> None:
    def boom() -> health.ProbeResult:
        raise RuntimeError("inner")

    reg = health.HealthRegistry()
    reg.register("boom", boom)
    out = reg.probe()
    assert out["state"] == health.HealthState.FAIL
    assert "raised" in out["probes"]["boom"]["detail"]


def test_default_registry_includes_canonical_probes() -> None:
    reg = health.default_registry()
    assert "settings" in reg.names()
    assert "ledger_secret" in reg.names()
    assert "audit_ledger" in reg.names()


def test_probe_audit_ledger_ok_on_fresh_chain() -> None:
    audit_ledger.get_default_ledger().record("init", {})
    r = health.probe_audit_ledger()
    assert r.state == health.HealthState.OK


def test_probe_ledger_secret_awaiting_when_no_key(monkeypatch) -> None:
    from backend.common import config

    s = config.get_settings()
    monkeypatch.setattr(s, "shared_hmac_key", "")
    monkeypatch.setattr(s, "samus_ledger_secret_key", "", raising=False)
    r = health.probe_ledger_secret()
    assert r.state == health.HealthState.AWAITING_OPERATOR_ACTION


def test_probe_ledger_secret_degraded_when_only_shared(monkeypatch) -> None:
    from backend.common import config

    s = config.get_settings()
    monkeypatch.setattr(s, "shared_hmac_key", "x" * 32)
    monkeypatch.setattr(s, "samus_ledger_secret_key", "", raising=False)
    r = health.probe_ledger_secret()
    assert r.state == health.HealthState.DEGRADED


def test_probe_ledger_secret_ok_when_dedicated_key(monkeypatch) -> None:
    from backend.common import config

    s = config.get_settings()
    monkeypatch.setattr(s, "samus_ledger_secret_key", "y" * 32, raising=False)
    r = health.probe_ledger_secret()
    assert r.state == health.HealthState.OK
