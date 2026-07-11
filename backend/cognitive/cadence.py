"""Autonomous cognition cadence — the ACTIVE tick loop wrapping ``run_one_cycle``.

This is the operator-directed step that follows Phase F/G: instead of being
driven only by a manual gateway POST, the cognitive stack now self-drives on a
timer from INSIDE the always-on gateway container, modelled EXACTLY on
:mod:`backend.gateway.control_tick_task` (the canonical "background tick"
pattern in this codebase).

The cadence is **ACTIVE by default** per operator direction (env
``SAMUS_COGNITION_CADENCE_ENABLED`` defaults True), but each commercial sub-
behaviour STILL honours its own flag. Concretely:

* ``cognition_cadence_enabled`` (this module's kill-switch, default True) ─
  when False the cadence task does not start at all (the lifespan logs +
  returns immediately).
* ``cognitive_loop_enabled`` (the Phase-F MASTER switch, default False) ─
  re-checked **every tick** so the operator can arm/disarm the loop without
  restarting the gateway. When False the tick is a pure no-op: NO runtime is
  built, NO ``run_one_cycle`` is invoked, NO CRM/finance/LLM backend is
  touched. The cadence loop stays alive (a future arm flips the switch and the
  next tick wakes the stack).
* ``autonomy_meta_enabled`` / ``cognitive_act_proposals_enabled`` /
  ``cognition_proposal_promotion_enabled`` / persona — each independently
  gated INSIDE the runner. The cadence does not read them; the runner does.
  So disarming any one layer doesn't take the cadence loop down.

Safety rails (non-negotiable; tests prove each):

* **Single-flight.** If a prior cycle is still running when the next tick
  arrives, the new tick is SKIPPED, not overlapped. A skipped tick is logged
  at info level so the operator can see if the LLM/broker is wedged.
* **Exception isolation.** Any exception inside a cycle (or its construction)
  is caught + logged; the cadence loop continues. ``run_one_cycle`` is itself
  fail-closed and returns a degraded summary even on a wiring fault — this is
  defence in depth.
* **Broker backoff (circuit cool-off).** If N consecutive cycles return
  ``errors`` containing ``BudgetExceeded`` / ``GlobalBudgetExceeded`` /
  ``broker_unreachable`` / ``LlmCallError``, the cadence multiplies its
  interval by a backoff factor (capped at 10×) so the loop stops hammering a
  wedged broker. The first clean cycle resets backoff.
* **Clean shutdown.** ``stop`` sets a shutdown flag, cancels the asyncio
  task, and awaits it — a cycle in flight either completes naturally or
  surfaces ``CancelledError`` from the inner ``sleep`` and exits the loop.

Telemetry: each tick emits one structured log line
``samus.cognition.cadence.tick`` carrying
``{plan_token, ok, compliance_blocked, elapsed_ms, promotion_attempted,
errors_count, skipped, backoff_state}``.

PRESERVATION: this module is **additive**. It imports nothing from
``recovery/*``, never touches ``backend/common/autonomy.py``,
``backend/identity/autonomy_layer.py``, the domain modules, or
``control_tick_task.py`` itself. It only consumes the public surface of the
Phase F/G runner (``run_one_cycle`` + ``loop_enabled``).
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
import uuid
from typing import Any, Optional

_LOG = logging.getLogger("samus.cognition.cadence")

# Defaults mirror the build spec. The cadence is a *secondary* heartbeat
# (the revenue heartbeat is :mod:`control_tick_task`); the 60s base interval
# keeps it busy without saturating the LLM broker.
_DEFAULT_INTERVAL_SEC = 60
_DEFAULT_JITTER_SEC = 10
_DEFAULT_INITIAL_DELAY_SEC = 5.0  # short settle so the gateway finishes boot

# Backoff parameters (constants — internal, not env-tunable for now). The
# cap is intentionally modest: 10× a 60s base means at most a ~10min cool-off
# before the next probe, so a transient broker outage self-recovers without
# operator intervention but a persistent one is plainly visible in the log.
_BACKOFF_TRIP_THRESHOLD = 5  # consecutive broker-failure cycles before tripping
_BACKOFF_MULTIPLIER = 2.0    # interval *= mult on each step past the trip
_BACKOFF_MAX_MULT = 10.0     # cap (so 60s base never grows past 600s)

# Substrings that flag a broker / budget failure inside a cycle's ``errors``
# list. Matches the shape :class:`CognitiveLoop._record_error` produces
# (``"REASON:BudgetExceeded:..."`` / ``"REASON:LlmCallError:..."``).
_BROKER_ERROR_TOKENS = (
    "BudgetExceeded",
    "GlobalBudgetExceeded",
    "LlmCallError",
    "broker_unreachable",
)


# ---------------------------------------------------------------------------
# settings resolvers — fail-closed to safe defaults on any settings error
# ---------------------------------------------------------------------------
def cadence_enabled() -> bool:
    """Resolve the cadence kill-switch ``cognition_cadence_enabled`` (default True).

    Default True per operator direction. Any settings error degrades to True
    so a misconfiguration cannot silently dormancy the loop — the operator
    explicitly opts out by setting ``SAMUS_COGNITION_CADENCE_ENABLED=0``.
    """
    try:
        from backend.common.config import get_settings

        return bool(getattr(get_settings(), "cognition_cadence_enabled", True))
    except Exception:  # noqa: BLE001 — fail to ACTIVE (operator opted in by default)
        return True


def cadence_interval_seconds() -> float:
    """Resolve the base cadence interval (default 60s)."""
    try:
        from backend.common.config import get_settings

        v = int(getattr(get_settings(), "cognition_cadence_interval_seconds", _DEFAULT_INTERVAL_SEC))
        return float(max(1, v))  # never 0 (would tight-loop); never negative
    except Exception:  # noqa: BLE001
        return float(_DEFAULT_INTERVAL_SEC)


def cadence_jitter_seconds() -> float:
    """Resolve the per-tick jitter window (default 10s; 0 disables jitter)."""
    try:
        from backend.common.config import get_settings

        v = int(getattr(get_settings(), "cognition_cadence_jitter_seconds", _DEFAULT_JITTER_SEC))
        return float(max(0, v))
    except Exception:  # noqa: BLE001
        return float(_DEFAULT_JITTER_SEC)


def _looks_like_broker_failure(errors: Any) -> bool:
    """True iff any entry in ``errors`` carries a broker/budget failure token."""
    if not errors:
        return False
    try:
        for e in errors:
            s = str(e)
            for tok in _BROKER_ERROR_TOKENS:
                if tok in s:
                    return True
    except Exception:  # noqa: BLE001 — telemetry only; never break the tick
        return False
    return False


# ---------------------------------------------------------------------------
# The cadence task — single-flight, isolated, backoff-aware, cleanly stoppable
# ---------------------------------------------------------------------------
class CognitionCadenceTask:
    """One asyncio task that ticks ``run_one_cycle`` on a base+jitter cadence.

    Modelled on :mod:`backend.gateway.control_tick_task` for shutdown
    semantics: a ``_shutdown`` flag plus ``task.cancel()`` + ``await task``
    in ``stop()``. The inner loop only ``await``s on ``asyncio.sleep`` and
    ``run_one_cycle``; any ``CancelledError`` from either propagates out of
    the loop body cleanly.

    Single-flight is enforced WITHOUT a lock: the loop's body is sequential —
    each iteration runs the cycle to completion BEFORE sleeping for the next
    interval. So the only way to "overlap" would be to spawn a concurrent
    cycle, which the cadence never does. A separate ``_in_flight`` boolean is
    maintained for telemetry/observability.
    """

    def __init__(
        self,
        *,
        interval_seconds: Optional[float] = None,
        jitter_seconds: Optional[float] = None,
        initial_delay_seconds: float = _DEFAULT_INITIAL_DELAY_SEC,
    ) -> None:
        # ``None`` => resolve from settings each loop iteration (so an env-driven
        # interval change is picked up on the next tick without a restart).
        self._configured_interval = interval_seconds
        self._configured_jitter = jitter_seconds
        self._initial_delay = max(0.0, float(initial_delay_seconds))

        self._task: Optional[asyncio.Task] = None
        self._shutdown = False
        self._in_flight = False

        # Backoff state. ``_consecutive_broker_failures`` counts cycles whose
        # errors looked like a broker/budget failure; the multiplier kicks in
        # once it crosses the trip threshold.
        self._consecutive_broker_failures = 0
        self._backoff_mult = 1.0

        # Last-tick snapshot for the /api/samus/cognition/health probe (Slice B).
        # Populated at the end of every completed tick. Empty until the first
        # tick fires. NOT persisted — restart resets it, matching the ephemeral
        # nature of the cadence itself.
        self._last_tick: dict = {}

    # --------- public lifecycle (mirrors control_tick_task.start/stop) ------
    async def start(self, app: Any) -> Optional[asyncio.Task]:
        """Schedule the cadence loop. Idempotent. Returns the task or None."""
        if not cadence_enabled():
            _LOG.info(
                "cognition cadence disabled (SAMUS_COGNITION_CADENCE_ENABLED=0); not starting",
            )
            return None
        existing = getattr(app.state, "cognition_cadence_task", None)
        if existing is not None and not existing.done():
            return existing
        self._shutdown = False
        task = asyncio.create_task(self._run_forever(), name="samus.cognition_cadence")
        self._task = task
        app.state.cognition_cadence_task = task
        # Surface the runtime instance too so tests + ops can inspect state
        # (in-flight flag, backoff posture) without reaching into private vars.
        app.state.cognition_cadence_runtime = self
        _LOG.info(
            "cognition cadence started (interval=%.0fs jitter=%.0fs default-on)",
            cadence_interval_seconds(),
            cadence_jitter_seconds(),
        )
        return task

    async def stop(self, app: Any) -> None:
        """Cancel + await the cadence task. Idempotent + best-effort."""
        self._shutdown = True
        task = getattr(app.state, "cognition_cadence_task", None) or self._task
        if task is None:
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001 — teardown swallows
            pass
        app.state.cognition_cadence_task = None
        # Leave ``cognition_cadence_runtime`` set for post-mortem inspection.
        _LOG.info("cognition cadence stopped")

    # --------- the inner loop --------------------------------------------------
    async def _run_forever(self) -> None:
        """Sleep -> tick -> repeat. Exceptions inside a tick never kill the loop."""
        # Short settle delay so the gateway finishes boot before the first tick.
        try:
            await asyncio.sleep(self._initial_delay)
        except asyncio.CancelledError:
            raise

        while not self._shutdown:
            await self._run_one_tick_safely()
            # Compute the next sleep AFTER the tick so a backoff trip applies
            # to the next gap. Jitter is uniform [0, J]; the master interval
            # is read fresh so a runtime env change is picked up.
            base = self._configured_interval if self._configured_interval is not None else cadence_interval_seconds()
            jitter_max = self._configured_jitter if self._configured_jitter is not None else cadence_jitter_seconds()
            jitter = random.uniform(0.0, jitter_max) if jitter_max > 0 else 0.0
            sleep_for = (base * self._backoff_mult) + jitter
            try:
                await asyncio.sleep(sleep_for)
            except asyncio.CancelledError:
                raise

    async def _run_one_tick_safely(self) -> None:
        """Run ONE tick with single-flight + master-switch gate + exception isolation."""
        # Single-flight: structurally impossible to overlap given the
        # sequential body, but we keep the flag for telemetry. If somehow a
        # prior tick is still in flight, skip + log.
        if self._in_flight:
            _LOG.info(
                "cognition cadence tick SKIPPED (prior cycle still in flight; broker slow?)",
            )
            return

        # MASTER-SWITCH GATE — checked EVERY tick (live arm/disarm without restart).
        # When False, do not import the runner's heavy deps or build any runtime.
        try:
            from backend.cognitive.runner import loop_enabled
        except Exception as exc:  # noqa: BLE001 — defensive; cadence stays alive
            _LOG.warning("cognition cadence: runner import failed: %s", exc)
            return

        if not loop_enabled():
            # Pure no-op tick. The loop continues so a runtime arm wakes it.
            # Logged at debug to avoid noise when the operator left the master
            # switch off.
            _LOG.debug(
                "cognition cadence tick: master switch OFF; no-op (cadence stays alive)",
            )
            return

        self._in_flight = True
        started = time.perf_counter()
        plan_token = ""
        ok = False
        compliance_blocked = False
        promotion_attempted = False
        errors: list = []

        try:
            # Build a default CycleInput tagged for the cadence. ``plan_token``
            # gives every tick an addressable id in downstream telemetry.
            plan_token = f"cadence-{uuid.uuid4().hex[:12]}"
            inp = self._build_default_input(plan_token)
            summary = await self._invoke_run_one_cycle(inp)

            # Project the summary onto the local telemetry locals. The cadence
            # never raises on a degraded summary — ``run_one_cycle`` is itself
            # fail-closed and returns a CycleResultSummary even on a wiring
            # fault.
            ok = bool(getattr(summary, "ok", False))
            compliance_blocked = bool(getattr(summary, "compliance_blocked", False))
            errors = list(getattr(summary, "errors", []) or [])
            promotion_attempted = bool(getattr(summary, "promotion_attempted", False))
            # Prefer the summary's plan_token (echoed by the loop); fall back
            # to the one we generated.
            sp = getattr(summary, "plan_token", "") or ""
            if sp:
                plan_token = sp
        except asyncio.CancelledError:
            # A shutdown during a cycle propagates so _run_forever exits.
            self._in_flight = False
            raise
        except Exception as exc:  # noqa: BLE001 — exception isolation; never kill loop
            _LOG.exception("cognition cadence tick raised; isolating and continuing")
            errors = [f"cadence_tick:{type(exc).__name__}:{exc}"]
        finally:
            self._in_flight = False

        elapsed_ms = (time.perf_counter() - started) * 1000.0

        # ---- backoff bookkeeping ---------------------------------------------
        # A "broker failure" cycle is one whose ``errors`` carry a broker/budget
        # token. A non-error cycle (or an error that's not broker-shaped) resets
        # the streak + the multiplier.
        broker_failure = _looks_like_broker_failure(errors)
        backoff_state = "steady"
        if broker_failure:
            self._consecutive_broker_failures += 1
            if self._consecutive_broker_failures >= _BACKOFF_TRIP_THRESHOLD:
                # First trip step doubles; subsequent steps keep doubling up to cap.
                new_mult = max(self._backoff_mult, 1.0) * _BACKOFF_MULTIPLIER
                self._backoff_mult = min(_BACKOFF_MAX_MULT, new_mult)
                backoff_state = f"tripped:x{self._backoff_mult:.1f}"
            else:
                backoff_state = f"warming:{self._consecutive_broker_failures}/{_BACKOFF_TRIP_THRESHOLD}"
        else:
            if self._backoff_mult > 1.0 or self._consecutive_broker_failures > 0:
                backoff_state = "reset"
            self._consecutive_broker_failures = 0
            self._backoff_mult = 1.0

        # Update the last-tick snapshot BEFORE emitting the structured log so a
        # health probe firing immediately after a tick sees consistent state.
        self._last_tick = {
            "at_epoch_s": time.time(),
            "plan_token": plan_token,
            "ok": ok,
            "compliance_blocked": compliance_blocked,
            "elapsed_ms": round(elapsed_ms, 1),
            "promotion_attempted": promotion_attempted,
            "errors_count": len(errors),
            "backoff_state": backoff_state,
        }

        _LOG.info(
            "samus.cognition.cadence.tick plan_token=%s ok=%s compliance_blocked=%s "
            "elapsed_ms=%.1f promotion_attempted=%s errors_count=%d backoff_state=%s",
            plan_token,
            ok,
            compliance_blocked,
            elapsed_ms,
            promotion_attempted,
            len(errors),
            backoff_state,
        )

    # --------- seams (overridable for tests) ----------------------------------
    def _build_default_input(self, plan_token: str) -> Any:
        """Build the default ``CycleInput`` shaped for an autonomous tick.

        Lazy import keeps the cycle_models module out of the cadence import
        graph until the first tick fires.
        """
        from backend.cognitive.cycle_models import CycleInput

        return CycleInput(
            user_text=(
                "Autonomous cognition tick. Considering the current business "
                "state, name the single highest-leverage next action. Answer in "
                "one or two concise sentences."
            ),
            channel="cognition_tick",
            plan_token=plan_token,
            metadata={"source": "cadence"},
        )

    async def _invoke_run_one_cycle(self, inp: Any) -> Any:
        """Invoke the runner. Isolated so tests can replace it without monkey-
        patching the import system."""
        from backend.cognitive.runner import run_one_cycle

        return await run_one_cycle(inp)

    # --------- ops surface ----------------------------------------------------
    def snapshot(self) -> dict:
        """Compact, JSON-able runtime state for the /health probe.

        Reads only in-memory fields already maintained by the tick loop — no I/O,
        no LLM, no lock. Safe to call from any thread/async context.
        """
        return {
            "in_flight": bool(self._in_flight),
            "backoff_mult": round(float(self._backoff_mult), 3),
            "consecutive_broker_failures": int(self._consecutive_broker_failures),
            "shutdown": bool(self._shutdown),
            "last_tick": dict(self._last_tick),
        }


# ---------------------------------------------------------------------------
# module-level helpers that mirror control_tick_task's import surface
# ---------------------------------------------------------------------------
async def start_cognition_cadence(
    app: Any,
    *,
    interval_seconds: Optional[float] = None,
    jitter_seconds: Optional[float] = None,
) -> Optional[asyncio.Task]:
    """Construct + start a :class:`CognitionCadenceTask` on ``app.state``.

    Mirrors :func:`backend.gateway.control_tick_task.start_control_tick_loop`:
    idempotent (returns the existing task if still running), respects the
    kill-switch, logs a one-line "started"/"disabled" outcome.
    """
    existing = getattr(app.state, "cognition_cadence_task", None)
    if existing is not None and not existing.done():
        return existing
    runtime = CognitionCadenceTask(
        interval_seconds=interval_seconds,
        jitter_seconds=jitter_seconds,
    )
    return await runtime.start(app)


async def stop_cognition_cadence(app: Any) -> None:
    """Stop the cadence (idempotent + best-effort). Mirrors control_tick_task."""
    runtime = getattr(app.state, "cognition_cadence_runtime", None)
    if runtime is not None:
        await runtime.stop(app)
        return
    # Fallback: a stray task without its runtime — cancel + await.
    task = getattr(app.state, "cognition_cadence_task", None)
    if task is None:
        return
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        pass
    app.state.cognition_cadence_task = None


__all__ = [
    "CognitionCadenceTask",
    "start_cognition_cadence",
    "stop_cognition_cadence",
    "cadence_enabled",
    "cadence_interval_seconds",
    "cadence_jitter_seconds",
]
