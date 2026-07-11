"""Cadence tests — the ACTIVE autonomous cognition cadence wrapping ``run_one_cycle``.

Covers the build-spec test bullets:

  (a) **flag OFF** ⇒ task starts but never invokes ``run_one_cycle`` —
      proven with a raising spy on the runner.
  (b) **cadence ON + MASTER OFF** ⇒ task starts, ticks fire, but
      ``run_one_cycle`` is NOT invoked (the per-tick ``loop_enabled`` gate).
      The cadence stays alive across ticks (live arm path).
  (c) **cadence ON + MASTER ON** ⇒ at least one tick invokes ``run_one_cycle``
      within a short test window. Uses a tiny interval + zero jitter so the
      window is deterministic.
  (d) **overlap prevention** ⇒ a stub ``run_one_cycle`` that blocks longer
      than the interval is never re-entered (the second tick is skipped, not
      overlapped); only ONE call is in flight at any time.
  (e) **per-cycle exception isolated** ⇒ when one tick raises, the cadence
      survives and the next tick still fires.
  (f) **broker backoff** ⇒ N consecutive cycles whose ``errors`` carry a
      broker token (e.g. ``BudgetExceeded``) trip the backoff multiplier; a
      single clean cycle resets it.
  (g) **clean shutdown** ⇒ ``stop()`` cancels the task, awaits it, and leaves
      no orphan in ``app.state``.

Every test uses STUBS (no real LM Studio / CRM / finance / EFH backend) and a
fake ``app`` object (``types.SimpleNamespace`` with a ``state`` namespace) so
nothing is constructed beyond the cadence task itself.

All async bodies are driven via ``asyncio.run`` to match the existing test
pattern in ``test_phase_f_runner.py`` / ``test_phase_g_promoter.py`` — this
suite does NOT depend on pytest-asyncio.
"""

from __future__ import annotations

import asyncio
import types

from backend.cognitive.cadence import CognitionCadenceTask


# ---------------------------------------------------------------------------
# flag helpers + tiny utilities
# ---------------------------------------------------------------------------
def _reload():
    from backend.common.settings import reload_settings

    reload_settings()


def _arm_cadence(monkeypatch, enabled=True):
    monkeypatch.setenv("SAMUS_COGNITION_CADENCE_ENABLED", "true" if enabled else "false")
    _reload()


def _arm_master(monkeypatch, enabled=True):
    monkeypatch.setenv("SAMUS_COGNITIVE_LOOP_ENABLED", "true" if enabled else "false")
    _reload()


def _fake_app() -> types.SimpleNamespace:
    return types.SimpleNamespace(state=types.SimpleNamespace())


def _make_summary(
    *, ok=True, errors=None, compliance_blocked=False, plan_token="t", promotion_attempted=False
):
    """Build a minimal CycleResultSummary-shaped object the cadence consumes."""
    return types.SimpleNamespace(
        ok=ok,
        errors=list(errors or []),
        compliance_blocked=compliance_blocked,
        plan_token=plan_token,
        promotion_attempted=promotion_attempted,
        proposal_written=False,
        proposal=None,
    )


def _run(coro):
    """Drive an async test body. Mirrors the helper in test_phase_f_runner.py."""
    return asyncio.run(coro)


# ===========================================================================
# (a) cadence kill-switch OFF ⇒ task does not start, runner never called
# ===========================================================================
def test_cadence_off_does_not_start_and_never_invokes_runner(monkeypatch):
    _arm_cadence(monkeypatch, enabled=False)
    _arm_master(monkeypatch, enabled=True)  # master ON to prove cadence flag dominates

    async def _body():
        runtime = CognitionCadenceTask(interval_seconds=0.01, jitter_seconds=0)

        # Raising spy on the runner: if start() somehow invokes it, the test fails.
        async def _boom(_inp):  # pragma: no cover — must NEVER be hit
            raise AssertionError("run_one_cycle must NOT be invoked when cadence flag is OFF")

        runtime._invoke_run_one_cycle = _boom  # type: ignore[assignment]

        app = _fake_app()
        task = await runtime.start(app)

        # When the kill-switch is off, start() returns None and registers no task.
        assert task is None
        assert getattr(app.state, "cognition_cadence_task", None) is None

        # Give the event loop a few ticks to surface any rogue scheduling.
        await asyncio.sleep(0.05)

    _run(_body())


# ===========================================================================
# (b) cadence ON + master OFF ⇒ ticks fire, runner is NOT invoked
# ===========================================================================
def test_cadence_on_master_off_ticks_fire_but_runner_not_invoked(monkeypatch):
    _arm_cadence(monkeypatch, enabled=True)
    _arm_master(monkeypatch, enabled=False)  # MASTER OFF — the live arm gate

    invocations = {"n": 0}

    async def _body():
        runtime = CognitionCadenceTask(
            interval_seconds=0.01, jitter_seconds=0, initial_delay_seconds=0.0
        )

        async def _spy(_inp):  # pragma: no cover — must NEVER be hit while master OFF
            invocations["n"] += 1
            return _make_summary()

        runtime._invoke_run_one_cycle = _spy  # type: ignore[assignment]

        app = _fake_app()
        task = await runtime.start(app)
        assert task is not None

        # Let several tick boundaries pass so the master-OFF gate is exercised many
        # times. The cadence MUST stay alive across ticks.
        await asyncio.sleep(0.2)

        # And the cadence task is still alive.
        assert not task.done(), "cadence task must stay alive while master is off"

        await runtime.stop(app)

    _run(_body())

    # NO invocations of run_one_cycle even after multiple ticks.
    assert invocations["n"] == 0, (
        "run_one_cycle was invoked despite cognitive_loop_enabled=False — "
        "the per-tick master gate is not holding"
    )


# ===========================================================================
# (c) cadence ON + master ON ⇒ at least one tick invokes run_one_cycle
# ===========================================================================
def test_cadence_on_master_on_invokes_run_one_cycle(monkeypatch):
    _arm_cadence(monkeypatch, enabled=True)
    _arm_master(monkeypatch, enabled=True)

    invocations: list = []

    async def _body():
        runtime = CognitionCadenceTask(
            interval_seconds=0.05, jitter_seconds=0, initial_delay_seconds=0.0
        )

        async def _spy(inp):
            invocations.append(inp)
            return _make_summary(plan_token=getattr(inp, "plan_token", "t"))

        runtime._invoke_run_one_cycle = _spy  # type: ignore[assignment]

        app = _fake_app()
        task = await runtime.start(app)
        assert task is not None

        # Window long enough to clear initial_delay (0) + first sleep cycle.
        await asyncio.sleep(0.25)

        await runtime.stop(app)

    _run(_body())

    assert len(invocations) >= 1, (
        f"expected ≥1 run_one_cycle invocation within window; got {len(invocations)}"
    )
    # The cadence builds CycleInputs tagged source=cadence + channel=cognition_tick.
    inp = invocations[0]
    assert getattr(inp, "channel", "") == "cognition_tick"
    md = getattr(inp, "metadata", {}) or {}
    assert md.get("source") == "cadence"
    # Each tick gets a fresh plan_token prefixed "cadence-".
    assert str(getattr(inp, "plan_token", "")).startswith("cadence-")


# ===========================================================================
# (d) overlap prevention ⇒ blocking cycle keeps only ONE in flight at a time
# ===========================================================================
def test_cadence_overlap_prevention_only_one_in_flight(monkeypatch):
    _arm_cadence(monkeypatch, enabled=True)
    _arm_master(monkeypatch, enabled=True)

    state = {"concurrent": 0, "max_concurrent": 0, "completed": 0}

    async def _body():
        runtime = CognitionCadenceTask(
            interval_seconds=0.01, jitter_seconds=0, initial_delay_seconds=0.0
        )

        async def _slow_cycle(_inp):
            state["concurrent"] += 1
            state["max_concurrent"] = max(state["max_concurrent"], state["concurrent"])
            try:
                # Block much longer than the cadence interval; if the cadence were
                # to overlap, max_concurrent would climb past 1.
                await asyncio.sleep(0.08)
            finally:
                state["concurrent"] -= 1
                state["completed"] += 1
            return _make_summary()

        runtime._invoke_run_one_cycle = _slow_cycle  # type: ignore[assignment]

        app = _fake_app()
        task = await runtime.start(app)
        assert task is not None

        # Run long enough for several would-be tick boundaries while a cycle is in
        # flight.
        await asyncio.sleep(0.3)

        await runtime.stop(app)

    _run(_body())

    assert state["max_concurrent"] == 1, (
        f"cadence MUST be single-flight; observed max_concurrent={state['max_concurrent']}"
    )
    # At least one cycle completed despite the slow body.
    assert state["completed"] >= 1


# ===========================================================================
# (e) per-cycle exception isolated ⇒ next tick still fires
# ===========================================================================
def test_cadence_per_cycle_exception_is_isolated(monkeypatch):
    _arm_cadence(monkeypatch, enabled=True)
    _arm_master(monkeypatch, enabled=True)

    calls = {"n": 0}

    async def _body():
        runtime = CognitionCadenceTask(
            interval_seconds=0.02, jitter_seconds=0, initial_delay_seconds=0.0
        )

        async def _flaky(_inp):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("simulated transient fault")
            return _make_summary()

        runtime._invoke_run_one_cycle = _flaky  # type: ignore[assignment]

        app = _fake_app()
        task = await runtime.start(app)
        assert task is not None

        await asyncio.sleep(0.3)

        # And the task is still alive after the exception.
        assert not task.done()

        await runtime.stop(app)

    _run(_body())

    # The cadence survived the exception and re-entered the cycle at least once.
    assert calls["n"] >= 2, (
        f"per-cycle exception did not isolate; only {calls['n']} call(s) made before loop died"
    )


# ===========================================================================
# (f) broker backoff ⇒ N consecutive broker-error cycles trip the multiplier;
#     first clean cycle resets it.
# ===========================================================================
def test_cadence_broker_backoff_trips_then_resets(monkeypatch):
    _arm_cadence(monkeypatch, enabled=True)
    _arm_master(monkeypatch, enabled=True)

    async def _body():
        runtime = CognitionCadenceTask(
            interval_seconds=0.01, jitter_seconds=0, initial_delay_seconds=0.0
        )

        # Drive the per-tick path directly so we don't have to race a real loop.
        summaries = [
            _make_summary(ok=False, errors=["REASON:BudgetExceeded:over quota"]),
            _make_summary(ok=False, errors=["REASON:BudgetExceeded:over quota"]),
            _make_summary(ok=False, errors=["REASON:LlmCallError:timeout"]),
            _make_summary(ok=False, errors=["REASON:BudgetExceeded:over quota"]),
            _make_summary(ok=False, errors=["REASON:GlobalBudgetExceeded:dollar cap"]),
        ]

        async def _from_queue(_inp):
            return summaries.pop(0)

        runtime._invoke_run_one_cycle = _from_queue  # type: ignore[assignment]

        # First 4 broker-failure ticks → "warming" state, multiplier still 1.0.
        for _ in range(4):
            await runtime._run_one_tick_safely()
        assert runtime._backoff_mult == 1.0, "must not trip before threshold"
        assert runtime._consecutive_broker_failures == 4

        # 5th consecutive broker failure trips backoff (×2.0 by default).
        await runtime._run_one_tick_safely()
        assert runtime._backoff_mult >= 2.0, (
            f"backoff should have tripped on the 5th broker failure; mult={runtime._backoff_mult}"
        )
        assert runtime._consecutive_broker_failures == 5

        # Now a clean cycle → backoff resets immediately on the first non-broker tick.
        async def _clean(_inp):
            return _make_summary(ok=True, errors=[])

        runtime._invoke_run_one_cycle = _clean  # type: ignore[assignment]
        await runtime._run_one_tick_safely()

        assert runtime._backoff_mult == 1.0, "first clean cycle must reset multiplier"
        assert runtime._consecutive_broker_failures == 0

        # And a non-broker error (something other than the broker tokens) also
        # counts as a reset rather than as a trip step.
        async def _other_err(_inp):
            return _make_summary(ok=False, errors=["DECIDE:ValueError:bad input"])

        runtime._invoke_run_one_cycle = _other_err  # type: ignore[assignment]
        await runtime._run_one_tick_safely()
        assert runtime._backoff_mult == 1.0
        assert runtime._consecutive_broker_failures == 0

    _run(_body())


# ===========================================================================
# (g) clean shutdown ⇒ stop() cancels + awaits; no orphan in app.state
# ===========================================================================
def test_cadence_clean_shutdown_no_orphan_task(monkeypatch):
    _arm_cadence(monkeypatch, enabled=True)
    _arm_master(monkeypatch, enabled=True)

    async def _body():
        runtime = CognitionCadenceTask(
            interval_seconds=0.05, jitter_seconds=0, initial_delay_seconds=0.0
        )

        async def _spy(_inp):
            return _make_summary()

        runtime._invoke_run_one_cycle = _spy  # type: ignore[assignment]

        app = _fake_app()
        task = await runtime.start(app)
        assert task is not None and not task.done()

        # Let it tick once or twice so a real cycle is potentially in flight.
        await asyncio.sleep(0.12)

        await runtime.stop(app)

        # The task is finished (cancelled or returned cleanly) and unregistered.
        assert task.done()
        assert getattr(app.state, "cognition_cadence_task", None) is None

        # And a second stop() is a safe no-op (idempotent).
        await runtime.stop(app)

    _run(_body())


# ===========================================================================
# (h) module-level helpers: start_cognition_cadence is idempotent on app.state
# ===========================================================================
def test_module_helpers_start_and_stop_are_idempotent(monkeypatch):
    _arm_cadence(monkeypatch, enabled=True)
    _arm_master(monkeypatch, enabled=False)  # master OFF — keep ticks no-op

    async def _body():
        from backend.cognitive.cadence import (
            start_cognition_cadence,
            stop_cognition_cadence,
        )

        app = _fake_app()
        # Pass tiny interval so the cadence isn't sleeping a full minute during
        # teardown.
        t1 = await start_cognition_cadence(app, interval_seconds=0.05, jitter_seconds=0)
        assert t1 is not None
        # Calling start again returns the same task (idempotent — does NOT spawn
        # a 2nd loop).
        t2 = await start_cognition_cadence(app, interval_seconds=0.05, jitter_seconds=0)
        assert t2 is t1

        await stop_cognition_cadence(app)
        assert getattr(app.state, "cognition_cadence_task", None) is None

        # Stopping again is a safe no-op.
        await stop_cognition_cadence(app)

    _run(_body())
