"""Tests for backend.gateway.production_health_task.

The loop is a thin driver over
``backend.observability.production_health_notify.dispatch`` -- dedup + email
composition live there. So the tests pin the driver's own contract:

  * master kill-switch respected on start()
  * interval env override respected
  * start() creates a named task, stop() cancels it cleanly (idempotent)
  * a tick fault does NOT kill the loop (the assessment raises, next tick
    still runs)

Nothing here reaches out to SendGrid or the file system beyond what
``dispatch`` normally touches, and we monkeypatch it in every test so the
underlying alert-state ledger stays untouched.

Async test bodies drive via ``asyncio.run`` -- the Samus test suite does not
depend on pytest-asyncio (see ``tests/cognitive/test_cadence.py``).
"""

from __future__ import annotations

import asyncio
import sys
import types

from backend.gateway import production_health_task as pht


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


class _FakeAppState:
    def __init__(self) -> None:
        self.production_health_task = None


class _FakeApp:
    def __init__(self) -> None:
        self.state = _FakeAppState()


def _run(coro):
    return asyncio.run(coro)


def _install_fake_dispatch(monkeypatch, side_effect):
    """Install a fake ``dispatch`` on the notifier module.

    ``side_effect`` is either a value to return (dict) or an Exception to
    raise. Returns a mutable dict tracking call count.
    """
    calls = {"n": 0}

    def fake_dispatch(*a, **kw):
        calls["n"] += 1
        if isinstance(side_effect, Exception):
            raise side_effect
        return side_effect

    mod = types.ModuleType("backend.observability.production_health_notify")
    mod.dispatch = fake_dispatch
    monkeypatch.setitem(
        sys.modules,
        "backend.observability.production_health_notify",
        mod,
    )
    return calls


def _install_fake_dispatch_sequence(monkeypatch, side_effects):
    """Install a dispatch that walks through ``side_effects`` on each call and
    repeats the last one thereafter. Each item is either a dict (return value)
    or an Exception (raised)."""
    calls = {"n": 0}

    def fake_dispatch(*a, **kw):
        i = calls["n"]
        calls["n"] += 1
        eff = side_effects[min(i, len(side_effects) - 1)]
        if isinstance(eff, Exception):
            raise eff
        return eff

    mod = types.ModuleType("backend.observability.production_health_notify")
    mod.dispatch = fake_dispatch
    monkeypatch.setitem(
        sys.modules,
        "backend.observability.production_health_notify",
        mod,
    )
    return calls


# ---------------------------------------------------------------------------
# pure env accessors
# ---------------------------------------------------------------------------


def test_loop_enabled_defaults_on(monkeypatch):
    monkeypatch.delenv(pht.ENV_ENABLED, raising=False)
    assert pht._loop_enabled() is True


def test_loop_enabled_off_switch(monkeypatch):
    for val in ("0", "false", "no", "off", "FALSE"):
        monkeypatch.setenv(pht.ENV_ENABLED, val)
        assert pht._loop_enabled() is False, val


def test_interval_defaults_to_15_min(monkeypatch):
    monkeypatch.delenv(pht.ENV_INTERVAL, raising=False)
    assert pht._interval_sec() == 900.0


def test_interval_env_override_respected(monkeypatch):
    monkeypatch.setenv(pht.ENV_INTERVAL, "60")
    assert pht._interval_sec() == 60.0


def test_interval_bad_env_falls_back_to_default(monkeypatch):
    monkeypatch.setenv(pht.ENV_INTERVAL, "not-a-number")
    assert pht._interval_sec() == 900.0


# ---------------------------------------------------------------------------
# start / stop lifecycle
# ---------------------------------------------------------------------------


def test_start_returns_none_when_master_disabled(monkeypatch):
    monkeypatch.setenv(pht.ENV_ENABLED, "0")

    async def _body():
        app = _FakeApp()
        task = await pht.start_production_health_loop(app)
        assert task is None
        assert app.state.production_health_task is None
        # sleep a moment to prove no rogue task got scheduled
        await asyncio.sleep(0.02)

    _run(_body())


def test_start_creates_task_and_stop_cancels_it(monkeypatch):
    monkeypatch.delenv(pht.ENV_ENABLED, raising=False)
    monkeypatch.setenv(pht.ENV_INTERVAL, "3600")  # long, we never wait
    _install_fake_dispatch(monkeypatch, {"action": "unchanged", "fingerprint": ""})

    # Shorten the initial-delay so start() creates a task that would run
    # promptly; we cancel it via stop() before it ever ticks.
    monkeypatch.setattr(pht, "_INITIAL_DELAY_SEC", 0.01)

    async def _body():
        app = _FakeApp()
        task = await pht.start_production_health_loop(app)
        assert task is not None
        assert not task.done()
        assert app.state.production_health_task is task
        assert task.get_name() == "samus.production_health_loop"

        # Idempotent -- second start returns the same running task.
        task2 = await pht.start_production_health_loop(app)
        assert task2 is task

        await pht.stop_production_health_loop(app)
        assert app.state.production_health_task is None
        assert task.cancelled() or task.done()

    _run(_body())


def test_stop_is_idempotent_when_never_started():
    async def _body():
        app = _FakeApp()
        # No task attached -- must not raise, must not create one.
        await pht.stop_production_health_loop(app)
        assert app.state.production_health_task is None

    _run(_body())


# ---------------------------------------------------------------------------
# tick fault must not kill the loop
# ---------------------------------------------------------------------------


def test_tick_fault_does_not_kill_the_loop(monkeypatch):
    """When the underlying assessment raises, the loop logs + keeps ticking.

    Dispatch raises on the first call and returns cleanly afterwards; we let
    the loop run through the fault + at least one healthy tick and assert the
    task is still alive.
    """
    monkeypatch.delenv(pht.ENV_ENABLED, raising=False)
    monkeypatch.setattr(pht, "_INITIAL_DELAY_SEC", 0.0)
    monkeypatch.setenv(pht.ENV_INTERVAL, "0.02")

    calls = _install_fake_dispatch_sequence(
        monkeypatch,
        [
            RuntimeError("simulated assessment failure"),
            {"action": "unchanged", "fingerprint": "abc"},
            {"action": "unchanged", "fingerprint": "abc"},
        ],
    )

    async def _body():
        app = _FakeApp()
        task = await pht.start_production_health_loop(app)
        assert task is not None
        try:
            for _ in range(60):
                await asyncio.sleep(0.02)
                if calls["n"] >= 2 and not task.done():
                    break
            n = calls["n"]
            assert n >= 2, "loop stopped ticking after fault (calls=" + str(n) + ")"
            assert not task.done(), "loop died on tick fault"
        finally:
            await pht.stop_production_health_loop(app)

    _run(_body())


def test_tick_dispatch_actions_are_logged(monkeypatch, caplog):
    """Alerted / recovered / send_failed all log at INFO/WARN; unchanged is DEBUG."""
    monkeypatch.delenv(pht.ENV_ENABLED, raising=False)
    monkeypatch.setattr(pht, "_INITIAL_DELAY_SEC", 0.0)
    monkeypatch.setenv(pht.ENV_INTERVAL, "0.02")

    calls = _install_fake_dispatch_sequence(
        monkeypatch,
        [
            {"action": "alerted", "fingerprint": "aa", "to": "ops@example.com"},
            {"action": "recovered", "fingerprint": "", "to": "ops@example.com"},
            {"action": "send_failed", "fingerprint": "bb", "error": "boom"},
            {"action": "unchanged", "fingerprint": "bb"},
        ],
    )

    caplog.set_level("DEBUG", logger="samus.gateway.production_health_task")

    async def _body():
        app = _FakeApp()
        task = await pht.start_production_health_loop(app)
        assert task is not None
        try:
            for _ in range(80):
                await asyncio.sleep(0.02)
                if calls["n"] >= 4:
                    break
        finally:
            await pht.stop_production_health_loop(app)

    _run(_body())

    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "alerted" in text
    assert "recovered" in text
    assert "send_failed" in text
