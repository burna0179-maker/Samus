"""Gateway-internal control-tick loop — start/stop, gating, fault tolerance."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from backend.gateway import control_tick_task as ctt


@pytest.fixture(autouse=True)
def _fast_initial_delay(monkeypatch):
    # The real loop waits 120s before the first tick; collapse it for tests.
    monkeypatch.setattr(ctt, "_INITIAL_DELAY_SEC", 0.0)


def test_disabled_env_returns_no_task(monkeypatch):
    monkeypatch.setenv(ctt.ENV_ENABLED, "0")
    app = SimpleNamespace(state=SimpleNamespace())

    async def _run():
        return await ctt.start_control_tick_loop(app)

    assert asyncio.run(_run()) is None


def test_default_on_starts_task(monkeypatch):
    monkeypatch.delenv(ctt.ENV_ENABLED, raising=False)
    monkeypatch.setenv(ctt.ENV_INTERVAL, "0.01")

    async def _run():
        app = SimpleNamespace(state=SimpleNamespace())
        task = await ctt.start_control_tick_loop(app)
        assert task is not None and not task.done()
        await ctt.stop_control_tick_loop(app)
        assert getattr(app.state, "control_tick_task") is None
        return True

    assert asyncio.run(_run()) is True


def test_loop_actually_invokes_run_control_tick(monkeypatch):
    monkeypatch.delenv(ctt.ENV_ENABLED, raising=False)
    monkeypatch.setenv(ctt.ENV_INTERVAL, "0.01")
    calls = []

    import backend.gateway.control_tick as ct

    monkeypatch.setattr(
        ct,
        "run_control_tick",
        lambda **kw: calls.append(1) or {"ok": True, "recommendations": {"stale_deals": 0}},
    )

    async def _run():
        app = SimpleNamespace(state=SimpleNamespace())
        await ctt.start_control_tick_loop(app)
        for _ in range(50):
            await asyncio.sleep(0.01)
            if calls:
                break
        await ctt.stop_control_tick_loop(app)
        return len(calls)

    assert asyncio.run(_run()) >= 1


def test_tick_fault_does_not_kill_loop(monkeypatch):
    monkeypatch.delenv(ctt.ENV_ENABLED, raising=False)
    monkeypatch.setenv(ctt.ENV_INTERVAL, "0.01")
    calls = []

    import backend.gateway.control_tick as ct

    def _flaky(**kw):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("first tick boom")
        return {"ok": True, "recommendations": {}}

    monkeypatch.setattr(ct, "run_control_tick", _flaky)

    async def _run():
        app = SimpleNamespace(state=SimpleNamespace())
        await ctt.start_control_tick_loop(app)
        for _ in range(50):
            await asyncio.sleep(0.01)
            if len(calls) >= 2:
                break
        await ctt.stop_control_tick_loop(app)
        return len(calls)

    # Loop survived the first-tick exception and ticked again.
    assert asyncio.run(_run()) >= 2


def test_start_is_idempotent(monkeypatch):
    monkeypatch.delenv(ctt.ENV_ENABLED, raising=False)
    monkeypatch.setenv(ctt.ENV_INTERVAL, "5")

    async def _run():
        app = SimpleNamespace(state=SimpleNamespace())
        t1 = await ctt.start_control_tick_loop(app)
        t2 = await ctt.start_control_tick_loop(app)
        same = t1 is t2
        await ctt.stop_control_tick_loop(app)
        return same

    assert asyncio.run(_run()) is True
