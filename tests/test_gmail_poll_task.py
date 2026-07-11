"""Intake-internal gmail poll loop -- start/stop, gating, fault tolerance.

Mirrors ``tests/test_control_tick_loop.py`` -- same asyncio start/stop
+ tick-fault-survival contract, targeted at the new
``backend.intake.gmail_poll_task`` module. Every test collapses the
_INITIAL_DELAY_SEC to 0 so the loop first tick fires immediately.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from backend.intake import gmail_poll_task as gpt


@dataclass
class _FakeResult:
    """Stand-in for backend.intake.gmail_poller.DrainPassResult."""

    enabled: bool = True
    fetched: int = 0
    processed: int = 0
    duplicates: int = 0
    failed: int = 0
    connect_error: str = ""


@pytest.fixture(autouse=True)
def _fast_initial_delay(monkeypatch):
    # The real loop waits 60s before the first tick; collapse it for tests.
    monkeypatch.setattr(gpt, "_INITIAL_DELAY_SEC", 0.0)


def _install_fake_drain(monkeypatch, drain):
    """Patch the deferred-import target (backend.intake.gmail_poller.drain_once).

    The loop imports drain_once lazily inside each tick, so the patch must
    live on the source module (backend.intake.gmail_poller), not on
    gmail_poll_task.
    """
    from backend.intake import gmail_poller

    monkeypatch.setattr(gmail_poller, "drain_once", drain)


# ---------------------------------------------------------------------------
# Master switch
# ---------------------------------------------------------------------------


def test_disabled_env_returns_no_task(monkeypatch):
    """SAMUS_GMAIL_POLL_ENABLED=0 -> start returns None, no task on app.state."""
    monkeypatch.setenv(gpt.ENV_ENABLED, "0")
    app = SimpleNamespace(state=SimpleNamespace())

    async def _run():
        return await gpt.start_gmail_poll_loop(app)

    assert asyncio.run(_run()) is None
    assert getattr(app.state, "gmail_poll_task", None) is None


def test_default_on_starts_and_stops_task(monkeypatch):
    """No env set -> default ON -> task attached; stop cancels + clears it."""
    monkeypatch.delenv(gpt.ENV_ENABLED, raising=False)
    monkeypatch.setenv(gpt.ENV_INTERVAL, "0.01")
    _install_fake_drain(monkeypatch, lambda: _FakeResult(enabled=False))

    async def _run():
        app = SimpleNamespace(state=SimpleNamespace())
        task = await gpt.start_gmail_poll_loop(app)
        assert task is not None and not task.done()
        await gpt.stop_gmail_poll_loop(app)
        assert getattr(app.state, "gmail_poll_task") is None
        return True

    assert asyncio.run(_run()) is True


def test_start_is_idempotent(monkeypatch):
    monkeypatch.delenv(gpt.ENV_ENABLED, raising=False)
    monkeypatch.setenv(gpt.ENV_INTERVAL, "5")

    async def _run():
        app = SimpleNamespace(state=SimpleNamespace())
        t1 = await gpt.start_gmail_poll_loop(app)
        t2 = await gpt.start_gmail_poll_loop(app)
        same = t1 is t2
        await gpt.stop_gmail_poll_loop(app)
        return same

    assert asyncio.run(_run()) is True


def test_interval_env_override_is_honoured(monkeypatch):
    """A non-default ``SAMUS_GMAIL_POLL_INTERVAL_SEC`` reaches the loop.

    We can't observe the sleep directly, but we can confirm the numeric
    parser returns the operator's override and not the built-in default.
    """
    monkeypatch.setenv(gpt.ENV_INTERVAL, "42.5")
    assert gpt._float_env(gpt.ENV_INTERVAL, gpt._DEFAULT_INTERVAL_SEC) == 42.5

    monkeypatch.setenv(gpt.ENV_INTERVAL, "not-a-number")
    assert (
        gpt._float_env(
            gpt.ENV_INTERVAL,
            gpt._DEFAULT_INTERVAL_SEC,
        )
        == gpt._DEFAULT_INTERVAL_SEC
    )


def test_loop_actually_invokes_drain_once(monkeypatch):
    monkeypatch.delenv(gpt.ENV_ENABLED, raising=False)
    monkeypatch.setenv(gpt.ENV_INTERVAL, "0.01")
    calls: list[int] = []

    import backend.intake.gmail_poller as gp

    def _fake_drain(*args, **kwargs):
        calls.append(1)
        return gp.DrainPassResult(enabled=True, fetched=0, processed=0)

    monkeypatch.setattr(gp, "drain_once", _fake_drain)

    async def _run():
        app = SimpleNamespace(state=SimpleNamespace())
        await gpt.start_gmail_poll_loop(app)
        for _ in range(50):
            await asyncio.sleep(0.01)
            if calls:
                break
        await gpt.stop_gmail_poll_loop(app)
        return len(calls)

    assert asyncio.run(_run()) >= 1


def test_tick_fault_does_not_kill_loop(monkeypatch):
    """``drain_once`` raising is caught + logged; the next tick still fires."""
    monkeypatch.delenv(gpt.ENV_ENABLED, raising=False)
    monkeypatch.setenv(gpt.ENV_INTERVAL, "0.01")
    calls: list[int] = []

    import backend.intake.gmail_poller as gp

    def _flaky(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("first drain boom")
        return gp.DrainPassResult(enabled=True, fetched=0)

    monkeypatch.setattr(gp, "drain_once", _flaky)

    async def _run():
        app = SimpleNamespace(state=SimpleNamespace())
        await gpt.start_gmail_poll_loop(app)
        for _ in range(80):
            await asyncio.sleep(0.01)
            if len(calls) >= 2:
                break
        await gpt.stop_gmail_poll_loop(app)
        return len(calls)

    assert asyncio.run(_run()) >= 2


def test_run_drain_pass_survives_import_error(monkeypatch):
    """A raise from the drain path becomes a structured connect_error, not a crash."""
    import backend.intake.gmail_poller as gp

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated ddb outage")

    monkeypatch.setattr(gp, "drain_once", _boom)
    summary = gpt._run_drain_pass()
    assert summary["enabled"] is False
    assert "drain_raised" in summary["connect_error"]


def test_stop_when_never_started_is_noop():
    """Calling stop with no task attached returns cleanly."""

    async def _run():
        app = SimpleNamespace(state=SimpleNamespace())
        await gpt.stop_gmail_poll_loop(app)
        return True

    assert asyncio.run(_run()) is True
