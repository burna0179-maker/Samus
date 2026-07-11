"""Production pulse — idle-signal-driven campaign progression (2026-07-03).

The trigger moves from a 30-min metronome to watching the idle signal:
dormant by default, arms via SAMUS_PRODUCTION_PULSE_ENABLED, and each pulse
re-runs the SAME governed idle-drive reasoning with a pulse-scale threshold.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from backend.gateway import production_pulse_task as pp


def _app():
    return SimpleNamespace(state=SimpleNamespace())


def test_dormant_by_default(monkeypatch):
    monkeypatch.delenv(pp.ENV_ENABLED, raising=False)
    app = _app()
    task = asyncio.run(pp.start_production_pulse_loop(app))
    assert task is None
    assert getattr(app.state, "production_pulse_task", None) is None


def test_knob_parsing(monkeypatch):
    monkeypatch.setenv(pp.ENV_PULSE_SEC, "45")
    monkeypatch.setenv(pp.ENV_IDLE_THRESHOLD_S, "90")
    assert pp._pulse_sec() == 45.0
    assert pp._idle_threshold_s() == 90.0
    # Floors: pulse never under 30s, threshold never under 60s.
    monkeypatch.setenv(pp.ENV_PULSE_SEC, "5")
    monkeypatch.setenv(pp.ENV_IDLE_THRESHOLD_S, "10")
    assert pp._pulse_sec() == 30.0
    assert pp._idle_threshold_s() == 60.0
    # Garbage degrades to defaults.
    monkeypatch.setenv(pp.ENV_PULSE_SEC, "banana")
    assert pp._pulse_sec() == 120.0


def test_armed_start_and_stop_idempotent(monkeypatch):
    monkeypatch.setenv(pp.ENV_ENABLED, "1")

    async def _run():
        app = _app()
        t1 = await pp.start_production_pulse_loop(app)
        assert t1 is not None and not t1.done()
        t2 = await pp.start_production_pulse_loop(app)
        assert t2 is t1  # idempotent
        await pp.stop_production_pulse_loop(app)
        assert getattr(app.state, "production_pulse_task", None) is None
        await pp.stop_production_pulse_loop(app)  # second stop is a no-op

    asyncio.run(_run())


def test_pulse_invokes_idle_drive_with_threshold(monkeypatch):
    """One loop iteration calls run_idle_drive with the pulse threshold and a
    fault there does not kill the loop."""
    monkeypatch.setenv(pp.ENV_ENABLED, "1")
    monkeypatch.setenv(pp.ENV_IDLE_THRESHOLD_S, "60")
    calls = []

    import backend.cash_engine.idle_production as ip

    def fake_drive(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise RuntimeError("first pulse faults")  # loop must survive
        return {"ok": True, "produced": True, "reason": "test",
                "actuation": {"initiated": 2}}

    monkeypatch.setattr(ip, "run_idle_drive", fake_drive)
    monkeypatch.setattr(pp, "_INITIAL_DELAY_SEC", 0.0)

    async def _run():
        loop_task = asyncio.create_task(pp._production_pulse_loop(0.01, 60.0))
        for _ in range(200):
            if len(calls) >= 2:
                break
            await asyncio.sleep(0.01)
        loop_task.cancel()
        try:
            await loop_task
        except asyncio.CancelledError:
            pass

    asyncio.run(_run())
    assert len(calls) >= 2  # survived the first-pulse fault
    assert calls[0]["idle_threshold_s"] == 60.0
