"""In-stack daily prospecting cadence (ADR-014, ACCEPTED 2026-06-18) — dormant by default.

ADR-014 (ACCEPTED 2026-06-18) converges the daily prospecting fire off the host
PowerShell producer (``scripts/Run-ProspectingDaily.ps1``) and onto the
already-running ``samus-prospecting`` container. This module is that in-stack
driver: an asyncio loop in the prospecting app lifespan, mirroring the proven
gateway control-tick loop (``backend/gateway/control_tick_task.py``) shape —

  * deferred imports so the prospecting import graph stays light,
  * a short settle delay before the first fire, then a fixed interval,
  * fault-isolated ticks (a tick fault is logged and never kills the loop),
  * idempotent start + best-effort stop bound to ``app.state``.

Each tick composes the active cumulative zipcode set from the ported geo-ring
catalog (``backend/prospecting/geo_ring.py``) and runs the SAME in-container
discover path the gateway dispatches to (``service.process_discovery`` with
``persist_prospects=True``), so CRM persistence + the call-list / morning-list
artifacts happen with the container's own wiring (``CRM_URL``, the shared HMAC
key, AWS creds) — which is the entire point of ADR-014. After a successful
tick it records the run onto the geo-state and persists it (ring/day/history),
exactly as the host script's state-update tail did.

GATED OFF BY DEFAULT. The loop only starts when the operator arms
``SAMUS_PROSPECTING_IN_STACK_CADENCE_ENABLED`` (Settings field
``prospecting_in_stack_cadence_enabled``, default False; resolved at runtime
through the flag store so an operator flip takes effect without a restart).
When OFF — the default, and the state on merge — ``start_cadence_loop`` logs
and returns ``None`` without scheduling anything: ZERO behaviour change. The
host scheduled task remains the live producer; nothing here auto-advances the
ring (that stays an explicit operator action, per the PS1's ``-AdvanceRing``).

Cadence knobs (read directly from env, matching the control_tick_task
convention so they are tunable without a settings field):
  * ``SAMUS_PROSPECTING_CADENCE_INTERVAL_SEC`` — seconds between fires
    (default 86400 = daily).
  * ``SAMUS_PROSPECTING_CADENCE_INITIAL_DELAY_SEC`` — settle delay before the
    first fire (default 300s).
  * ``SAMUS_PROSPECTING_CADENCE_INDUSTRIES`` — comma-separated industry
    keywords (default mirrors the PS1 Tier A+B verticals).
  * ``SAMUS_PROSPECTING_CADENCE_MAX_PER_ZIP`` — per-zip Places cap (default 25,
    the PS1 default).
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Optional

_LOG = logging.getLogger("samus.prospecting.cadence_task")

# Daily by default. The control-tick loop uses 30 min for the revenue
# heartbeat; the prospecting fire is a once-a-day job (Places quota + the
# operator's morning call list), so the default interval is 24h.
_DEFAULT_INTERVAL_SEC = 86400.0
_DEFAULT_INITIAL_DELAY_SEC = 300.0  # let boot churn settle before the first fire
_DEFAULT_MAX_PER_ZIP = 25

# Default industry keyword set — byte-for-byte the PS1 ``$Industries`` default
# (Tier A+B verticals derived from the workflow_rescue + ai_ops_partner
# example docs). Operator override via SAMUS_PROSPECTING_CADENCE_INDUSTRIES.
_DEFAULT_INDUSTRIES: tuple[str, ...] = (
    "real estate agency",
    "dentist",
    "hvac contractor",
    "plumber",
    "roofing contractor",
    "accounting firm",
    "car dealer",
)

ENV_INTERVAL = "SAMUS_PROSPECTING_CADENCE_INTERVAL_SEC"
ENV_INITIAL_DELAY = "SAMUS_PROSPECTING_CADENCE_INITIAL_DELAY_SEC"
ENV_INDUSTRIES = "SAMUS_PROSPECTING_CADENCE_INDUSTRIES"
ENV_MAX_PER_ZIP = "SAMUS_PROSPECTING_CADENCE_MAX_PER_ZIP"
# Fresh-yield floor: a fire producing fewer NEW-to-pipeline prospects than
# this treats the current ring as exhausted (auto-advance / wrap).
ENV_MIN_NEW_YIELD = "SAMUS_RING_MIN_NEW_YIELD"
_DEFAULT_MIN_NEW_YIELD = 5

# Max chained fires per cadence invocation when a fire exhausts its ring and
# auto-advances — the newly-active ring is immediately re-fired instead of
# waiting the full cadence interval (~24h) for a productive call list. Bounded
# so a rare all-rings-exhausted day still terminates. 0 = single-fire (legacy).
ENV_MAX_RETICKS = "SAMUS_RING_MAX_RETICKS_PER_FIRE"
_DEFAULT_MAX_RETICKS = 2


def _min_new_yield() -> int:
    try:
        return max(0, int(os.environ.get(ENV_MIN_NEW_YIELD, "") or _DEFAULT_MIN_NEW_YIELD))
    except ValueError:
        return _DEFAULT_MIN_NEW_YIELD


def _max_reticks() -> int:
    try:
        return max(0, int(os.environ.get(ENV_MAX_RETICKS, "") or _DEFAULT_MAX_RETICKS))
    except ValueError:
        return _DEFAULT_MAX_RETICKS


# The flag this loop gates on. Name + binding live in
# backend/common/config.py (Settings field) + settings.py (env) + the
# flags catalog; resolved here through flags.runtime so an operator flip is
# honoured without a process restart.
FLAG_NAME = "prospecting_in_stack_cadence_enabled"


def cadence_enabled() -> bool:
    """Resolve the dormant-by-default arming flag (default OFF).

    Resolution order is the canonical Samus runtime-flag idiom: a persisted
    operator override wins, else the boot-time Settings field (which itself
    binds SAMUS_PROSPECTING_IN_STACK_CADENCE_ENABLED, default False), else —
    if the flag store is not even initialised (e.g. a bare unit test) — the
    settings fallback is returned verbatim. Net: OFF unless explicitly armed.
    """
    from backend.common.config import get_settings
    from backend.common.flags.runtime import is_enabled

    settings = get_settings()
    fallback = bool(getattr(settings, FLAG_NAME, False))
    return is_enabled(FLAG_NAME, fallback)


def _interval_sec() -> float:
    try:
        return float(os.environ.get(ENV_INTERVAL, "") or _DEFAULT_INTERVAL_SEC)
    except ValueError:
        return _DEFAULT_INTERVAL_SEC


def _initial_delay_sec() -> float:
    try:
        return float(os.environ.get(ENV_INITIAL_DELAY, "") or _DEFAULT_INITIAL_DELAY_SEC)
    except ValueError:
        return _DEFAULT_INITIAL_DELAY_SEC


def _max_per_zip() -> int:
    try:
        return int(os.environ.get(ENV_MAX_PER_ZIP, "") or _DEFAULT_MAX_PER_ZIP)
    except ValueError:
        return _DEFAULT_MAX_PER_ZIP


def _industries() -> list[str]:
    raw = os.environ.get(ENV_INDUSTRIES, "")
    if not raw.strip():
        return list(_DEFAULT_INDUSTRIES)
    parsed = [piece.strip() for piece in raw.split(",") if piece.strip()]
    return parsed or list(_DEFAULT_INDUSTRIES)


def _run_single_fire() -> dict[str, Any]:
    """One in-stack discovery fire against the currently-active geo-ring.

    Extracted from ``run_prospecting_tick`` so the tick can chain re-fires when
    a fire exhausts its ring — see ``run_prospecting_tick`` for the loop and
    ``ENV_MAX_RETICKS`` for the bound.

    Steps mirror the host producer:

      1. Load geo-state (ring/day/history) from the artifact-root JSON.
      2. Compose the cumulative zipcode set for the current ring.
      3. Run process_discovery with persist_prospects=True (the container's
         own CRM/AWS wiring does the persist + writes the call-list artifacts).
      4. Record the run onto the geo-state and persist it.

    Returns a small structured summary. Ring-exhaustion detection + the
    advance/wrap decision live here; the outer loop reads ``ring_action`` to
    decide whether to re-fire.
    """
    from . import geo_ring
    from . import service
    from .models import DiscoveryRequest

    state = geo_ring.load_state()
    zipcodes = state.active_zipcodes()
    industries = _industries()
    ring_idx = geo_ring.clamp_ring_index(state.current_ring)
    ring_label = state.current_ring_name()

    _LOG.info(
        "prospecting cadence tick: ring='%s' (index %d/%d) zipcodes=%d industries=%d",
        ring_label,
        ring_idx,
        geo_ring.top_ring_index(),
        len(zipcodes),
        len(industries),
    )

    req = DiscoveryRequest(
        campaign_name="daily_call_list",
        zipcodes=zipcodes,
        industries=industries,
        max_results_per_zip=_max_per_zip(),
        # PS1 default keeps no-website prospects (strongest web-design lead);
        # the operator drops them only with -ExcludeNoWebsite.
        must_have_website=False,
        # The whole point of ADR-014: persist into CRM from inside the
        # container so the cash engine has emailable rows. Best-effort in
        # service.py — a missing CRM url / HMAC key short-circuits cleanly.
        persist_prospects=True,
    )

    # Call via the module (not a local symbol) so the in-container discover
    # path is the single source of truth — and so tests can patch
    # service.process_discovery.
    result = service.process_discovery(req)

    # Stamp the run onto the geo-state + persist (ring/day/history), mirroring
    # the PS1 state-update tail. exit_code 0 == a completed fire.
    geo_ring.record_run(
        state,
        exit_code=0,
        zipcodes_count=len(zipcodes),
    )

    # Ring exhaustion check: low FRESH yield means this territory has been
    # mined out for now — advance (or wrap) so tomorrow's fire covers new
    # ground instead of re-promoting the same businesses.
    fresh = int(getattr(result, "fresh_count", 0) or 0)
    prospect_count = int(getattr(result, "prospect_count", 0) or 0)
    ring_action = "held"
    # Ring exhaustion is inferred from a LOW fresh yield — but ONLY when the
    # territory was actually mined this fire. A fire that returned zero
    # businesses (prospect_count == 0) is INCONCLUSIVE, not exhausted: a Google
    # Places daily-quota exhaustion, transport failure, access-block, or a
    # masked tier all surface as prospect_count == 0 (service.py discovery
    # path), indistinguishable from a genuinely empty ring. Counting that as
    # exhaustion — then multiplying it through the retick chain — would burn
    # several productive rings on a single bad-API tick (fresh==0 → advance →
    # re-fire → advance …). Fail conservative: hold the ring on an empty fire.
    # A persistently empty ring is a visible operator signal (empty call list),
    # not a silent ring-burn.
    if fresh < _min_new_yield():
        if prospect_count <= 0:
            ring_action = "held_empty_fire_inconclusive"
            _LOG.warning(
                "ring '%s' fire returned no businesses (prospect_count=0, "
                "fresh=%d) — INCONCLUSIVE (quota/transport/empty); holding ring "
                "instead of advancing to avoid burning rings via reticks",
                ring_label,
                fresh,
            )
        elif state.at_top_ring():
            state.current_ring = 0
            state.days_at_ring = 0
            ring_action = "wrapped_to_ring_0_recycle_pass"
            _LOG.info(
                "ring '%s' exhausted (fresh=%d < %d) at TOP ring — wrapped to "
                "ring 0; recycle-cooldown expiry now drives re-qualification",
                ring_label,
                fresh,
                _min_new_yield(),
            )
        else:
            geo_ring.advance_ring(state)
            ring_action = f"advanced_to_{state.current_ring_name()}"
            _LOG.info(
                "ring '%s' exhausted (fresh=%d < %d) — auto-advanced to '%s'",
                ring_label,
                fresh,
                _min_new_yield(),
                state.current_ring_name(),
            )
    state_path = geo_ring.save_state(state)

    summary = {
        "ok": True,
        "ring_index": ring_idx,
        "ring_name": ring_label,
        "zipcodes": len(zipcodes),
        "prospect_count": result.prospect_count,
        "fresh_count": fresh,
        "recycled_held_count": int(getattr(result, "recycled_held_count", 0) or 0),
        "ring_action": ring_action,
        "persisted_count": result.persisted_count,
        "csv_path": result.csv_path,
        "txt_path": result.txt_path,
        "state_path": str(state_path),
        "day_at_ring": state.days_at_ring,
    }
    _LOG.info(
        "prospecting cadence fire complete: prospects=%d persisted=%d csv=%s day_at_ring=%d",
        result.prospect_count,
        result.persisted_count,
        result.csv_path,
        state.days_at_ring,
    )
    return summary


def run_prospecting_tick() -> dict[str, Any]:
    """Fire discovery against the active geo-ring, chaining re-fires when a
    ring exhausts so the call list reflects the *productive* ring the same
    day instead of waiting 24h for the next cadence interval.

    RING LIFECYCLE (operator-ratified 2026-07-03, extended 2026-07-08):
    when a fire's FRESH yield (new-to-pipeline prospects after the promotion
    cooldown gate — see service.py step 3.8) falls below
    ``SAMUS_RING_MIN_NEW_YIELD`` (default 5), the ring is EXHAUSTED and
    auto-advanced (or wrapped to ring 0 at the top). Waiting for the next
    24h cadence to fire the newly-active ring left the operator with a
    near-empty call list all day; instead we immediately re-fire against
    the freshly-advanced ring, capped at ``SAMUS_RING_MAX_RETICKS_PER_FIRE``
    (default 2) so an all-rings-exhausted day still terminates.

    Returns the summary of the FINAL fire in the chain, augmented with:
      * ``retick_count``   — how many re-fires ran after the initial fire
      * ``retick_chain``   — ``ring_action`` of each fire, oldest first
    """
    budget = _max_reticks()
    chain: list[str] = []
    summary = _run_single_fire()
    chain.append(summary["ring_action"])
    retick_count = 0

    while budget > 0 and _is_advance_action(summary.get("ring_action", "")):
        budget -= 1
        retick_count += 1
        _LOG.info(
            "prospecting re-tick %d/%d against '%s' (prior ring exhausted: %s)",
            retick_count,
            _max_reticks(),
            _current_ring_label(),
            summary["ring_action"],
        )
        try:
            summary = _run_single_fire()
        except Exception:  # noqa: BLE001 — fault-isolate re-ticks like the outer loop
            _LOG.exception("prospecting re-tick faulted; keeping prior summary")
            break
        chain.append(summary["ring_action"])

    summary["retick_count"] = retick_count
    summary["retick_chain"] = chain
    return summary


def _is_advance_action(action: str) -> bool:
    """True if a fire's ring_action indicates the ring changed — the trigger
    for an immediate re-fire against the new ring."""
    return action.startswith("advanced_to_") or action.startswith("wrapped_to_")


def _current_ring_label() -> str:
    """Best-effort read of the on-disk geo-state's ring name for the re-tick
    log line. Silent-fail to a placeholder — logging must never fault a fire.
    """
    try:
        from . import geo_ring

        return geo_ring.load_state().current_ring_name()
    except Exception:  # noqa: BLE001
        return "?"


async def _cadence_loop(interval: float, initial_delay: float) -> None:
    # Settle delay, then fire on the cadence. Each tick is best-effort and
    # fault-isolated — a discovery fault is logged and never kills the loop.
    try:
        await asyncio.sleep(initial_delay)
    except asyncio.CancelledError:
        raise
    while True:
        try:
            run_prospecting_tick()
        except Exception:  # noqa: BLE001 — a tick fault never kills the loop
            _LOG.exception("prospecting cadence tick faulted; continuing")
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            raise


async def start_cadence_loop(app: Any) -> Optional[asyncio.Task]:
    """Schedule the in-container daily prospecting loop. Idempotent.

    DEFAULT OFF: when the arming flag is not set (the merge-time state), this
    logs and returns ``None`` WITHOUT scheduling anything — zero behaviour
    change. Only an armed flag starts the loop.
    """
    if not cadence_enabled():
        _LOG.info(
            "prospecting cadence loop disabled (flag '%s' OFF) — dormant; "
            "host producer remains the live path",
            FLAG_NAME,
        )
        return None
    existing = getattr(app.state, "prospecting_cadence_task", None)
    if existing is not None and not existing.done():
        return existing
    interval = _interval_sec()
    initial_delay = _initial_delay_sec()
    task = asyncio.create_task(
        _cadence_loop(interval, initial_delay),
        name="samus.prospecting_cadence_loop",
    )
    app.state.prospecting_cadence_task = task
    _LOG.info(
        "prospecting cadence loop started (interval=%.0fs initial_delay=%.0fs)",
        interval,
        initial_delay,
    )
    return task


async def stop_cadence_loop(app: Any) -> None:
    """Cancel + await the loop. Idempotent + best-effort."""
    task = getattr(app.state, "prospecting_cadence_task", None)
    if task is None:
        return
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):  # noqa: BLE001 — teardown swallows
        pass
    app.state.prospecting_cadence_task = None
    _LOG.info("prospecting cadence loop stopped")


__all__ = [
    "start_cadence_loop",
    "stop_cadence_loop",
    "run_prospecting_tick",
    "cadence_enabled",
    "FLAG_NAME",
    "ENV_INTERVAL",
    "ENV_INITIAL_DELAY",
    "ENV_INDUSTRIES",
    "ENV_MAX_PER_ZIP",
    "ENV_MIN_NEW_YIELD",
    "ENV_MAX_RETICKS",
]
