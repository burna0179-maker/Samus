"""Samus gateway service — capability-gated dispatch + autonomy + DLQ peek.

Per doc §4. Built on the shared ``create_base_app`` factory so it inherits
correlation middleware, /health, /metrics, and structured logging.

Endpoints:
  POST /dispatch/{target}     dispatch capability — SQS if configured else HTTP
  POST /autonomy/plan         autonomy_plan capability — risk-gated MAPE-K cycle
  GET  /dlq/{service}         dlq_read capability — pending failures
  GET  /dlq/archive           dlq_read capability — replayed/archived failures
  GET  /admin/llm_budgets     budget_admin capability — per-workcell token snapshot
  GET  /admin/tasks           list_tasks capability — CRM operator-task queue proxy
  GET  /admin/conversion_funnel  budget_admin capability — CRM funnel leak proxy
  GET  /admin/journey/{prospect_id}  journey_read capability — unified business-event journey
  GET  /api/crm/stats         crm_stats capability — Samus HUD daily roll-up
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool

from backend.common.net_limits import (
    INTER_WORKCELL_MAX_BYTES,
    ResponseTooLarge,
    check_httpx_size,
)

from backend.common import autonomy, control_tick_ledger, dlq, governance, llm_budget
from backend.common.app_factory import create_base_app
from backend.common.capabilities import check_capability
from backend.common.llm_budget import compute_quota
from backend.common.models import TaskEnvelope
from backend.common.settings import get_settings
from backend.cash_engine import service as cash_engine_service
from backend.cash_engine.models import RevenueTriggerRequest
from backend.standard.inter_agent import (
    get_subscriber,
    is_subscribe_disabled,
    register_handler,
    register_quorum_vote_route,
)
from backend.standard.inter_agent.event_handler import _default_log_handler

# Workcells the operator-facing budget summary enumerates. Matches the LLM
# callers wired today (prospecting, seo); update when new callers land.
_LLM_WORKCELLS = ("prospecting", "seo")

# /api/crm/stats — the Samus HUD pulls this on every page render, and the
# underlying CRM scan is a full-table walk on samus_call-State. A short
# in-memory cache keeps the hot path off DDB without making "stale by a
# day" possible: keyed by UTC date so the entry self-invalidates at
# midnight, with a 30 s TTL inside the day.
_CRM_STATS_TTL_SEC: float = 30.0
_CRM_STATS_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_CRM_STATS_CACHE_LOCK = threading.Lock()

# Operator goals — surfaced on /api/crm/stats so the HUD can render
# "12 / 30 calls today" without a second config read. Operator-tunable via
# env (SAMUS_CRM_CALLS_GOAL / SAMUS_CRM_EMAILS_GOAL); the 30 / 40 defaults
# mirror the values the forge-ui fallback path already shows.
_CRM_STATS_CALLS_GOAL_DEFAULT = 30
_CRM_STATS_EMAILS_GOAL_DEFAULT = 40

# Outreach audit ledger path inside the gateway container. Matches the
# default outreach.service writes to (SAMUS_OUTREACH_AUDIT_PATH); the
# samus-data volume is mounted on both workcells so reading what outreach
# wrote is a same-volume open.
_CRM_STATS_OUTREACH_AUDIT_DEFAULT = "/opt/samus/data/outreach/outreach_audit.jsonl"

from . import control_tick as control_tick_mod
from . import service as gateway_service
from . import sqs_dispatch
from .router import resolve_target

_LOG = logging.getLogger("samus.gateway.app")


def _parse_envelope(body: Any) -> TaskEnvelope:
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="expected_json_object")
    try:
        return TaskEnvelope.model_validate(body)
    except Exception as exc:  # pydantic ValidationError
        raise HTTPException(status_code=400, detail=f"invalid_envelope: {exc}")


def _enforce_simulation_gate(envelope: TaskEnvelope) -> None:
    """Refuse an external-effect dispatch that lacks a passing simulation.

    HOTL Tranche 5 mandatory gate. Reads ``action`` + ``decision_id`` from the
    envelope metadata and, when the action is external-effect, requires a
    recorded simulation (HIGH/CRITICAL also require a simulation-pass). A
    non-external-effect action is a no-op, so the hundreds of existing internal
    dispatches are unaffected. Refusal is a 409 (a precondition was not met),
    with the structured reason so callers can self-correct by simulating first.
    """
    from backend.common import governance, simulation

    action = str(envelope.metadata.get("action") or "") or None
    if not simulation.is_external_effect(action):
        return
    decision_id = str(
        envelope.metadata.get("decision_id")
        or envelope.metadata.get("task_id")
        or envelope.task_id
        or ""
    )
    risk_level, _ = governance.classify_risk(action or "", [action or ""])
    try:
        simulation.gate_dispatch(action, decision_id=decision_id, risk_level=risk_level)
    except simulation.SimulationRequired as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "simulation_required",
                "reason": exc.reason,
                "action": action,
                "decision_id": decision_id,
                "message": str(exc),
            },
        )


def _stats_goal(env_key: str, default: int) -> int:
    """Operator-tunable integer goal from the environment, default on bad input."""
    raw = (os.getenv(env_key) or "").strip()
    if not raw:
        return default
    try:
        val = int(raw)
    except (TypeError, ValueError):
        return default
    return val if val >= 0 else default


def _crm_stats_cache_get(today: str) -> dict[str, Any] | None:
    """Return the cached /api/crm/stats body for ``today`` if fresh."""
    with _CRM_STATS_CACHE_LOCK:
        # Stale-date entries are evicted on every miss so the cache cannot
        # silently keep yesterday's numbers alive past UTC midnight.
        for key in list(_CRM_STATS_CACHE.keys()):
            if key != today:
                _CRM_STATS_CACHE.pop(key, None)
        hit = _CRM_STATS_CACHE.get(today)
        if hit is None:
            return None
        stored_at, body = hit
        if (time.monotonic() - stored_at) < _CRM_STATS_TTL_SEC:
            return dict(body)
        _CRM_STATS_CACHE.pop(today, None)
        return None


def _crm_stats_cache_set(today: str, body: dict[str, Any]) -> None:
    with _CRM_STATS_CACHE_LOCK:
        _CRM_STATS_CACHE[today] = (time.monotonic(), dict(body))


async def _crm_stats_fetch_call_state(today: str) -> dict[str, Any]:
    """Proxy CRM ``GET /crm/metrics/daily-stats`` for ``today``.

    Returns a normalised ``{"calls_today", "booked_today", "followups_today",
    "error"}`` dict — ``error`` is set to a short string when CRM is
    unreachable, returns a non-2xx, or hands back a malformed body; the
    counts stay zero in every degraded case so the caller can render a
    "no data" tile rather than 500.

    The CRM workcell mounts ``VerifyHMACMiddleware`` on every route, so the
    proxy MUST sign the call. We use the shared GET-signing helper which
    signs path-only (no query string). To keep the canonical aligned with
    what the server reconstructs, we drop the ``?today=`` query and rely on
    the CRM route's own default — both containers compute UTC today the
    same way, so the day matches; the ``today`` parameter is kept on the
    function signature for the cache key and gateway-side bookkeeping.
    """
    settings = get_settings()
    crm_base = (settings.gateway_urls.get("crm") or "").strip()
    if not crm_base:
        return {"calls_today": 0, "booked_today": 0, "followups_today": 0,
                "error": "crm_url_not_configured"}
    crm_path = "/crm/metrics/daily-stats"
    try:
        # Lazy import to keep the gateway boot light and avoid pulling
        # http_client's chain when the route is never hit.
        from backend.common.http_client import _build_signed_get_request
        url, headers = _build_signed_get_request(
            crm_base, crm_path, secret=None,
        )
    except RuntimeError as exc:  # HMAC key unset
        _LOG.warning("crm_stats sign-prep failed: %s", exc)
        return {"calls_today": 0, "booked_today": 0, "followups_today": 0,
                "error": f"crm_sign_unconfigured: {exc.__class__.__name__}"}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, headers=headers)
    except (httpx.TimeoutException, httpx.ConnectError) as exc:
        _LOG.warning("crm_stats CRM proxy failed: %s", exc)
        return {"calls_today": 0, "booked_today": 0, "followups_today": 0,
                "error": f"crm_unreachable: {exc.__class__.__name__}"}
    try:
        check_httpx_size(
            resp, max_bytes=INTER_WORKCELL_MAX_BYTES, source="crm_stats",
        )
        body = resp.json()
    except ResponseTooLarge as exc:
        _LOG.warning("crm_stats CRM body over cap: %s", exc)
        return {"calls_today": 0, "booked_today": 0, "followups_today": 0,
                "error": "crm_response_too_large"}
    except ValueError:
        return {"calls_today": 0, "booked_today": 0, "followups_today": 0,
                "error": f"crm_bad_response: status={resp.status_code}"}
    if not isinstance(body, dict):
        return {"calls_today": 0, "booked_today": 0, "followups_today": 0,
                "error": "crm_non_object_response"}
    err = body.get("ddb_error") or None
    return {
        "calls_today": int(body.get("calls_today") or 0),
        "booked_today": int(body.get("booked_today") or 0),
        "followups_today": int(body.get("followups_today") or 0),
        "error": str(err) if err else "",
    }


def _crm_stats_count_outreach_today(today: str) -> tuple[int, str]:
    """Count ``send_message`` audit events whose ``ts`` starts with ``today``.

    Reads the outreach audit JSONL line-by-line so a multi-MB ledger doesn't
    pull into memory. Counts events with ``action == "send_message"`` AND
    ``status == "completed"`` — the success branch in
    :func:`backend.outreach.service.send_message`. Returns ``(count, error)``
    where ``error`` is "" on success / a short tag on failure; a missing
    file is "" (no rows, no error — outreach simply hasn't sent anything).
    """
    path = Path(os.getenv("SAMUS_OUTREACH_AUDIT_PATH",
                          _CRM_STATS_OUTREACH_AUDIT_DEFAULT))
    if not path.is_file():
        return 0, ""
    count = 0
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or not line.startswith("{"):
                    continue
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(ev, dict):
                    continue
                if ev.get("action") != "send_message":
                    continue
                if ev.get("status") != "completed":
                    continue
                ts = str(ev.get("ts") or "")
                if ts.startswith(today):
                    count += 1
    except OSError as exc:
        _LOG.warning("crm_stats outreach audit read failed: %s", exc)
        return 0, f"outreach_audit_unreadable: {exc.__class__.__name__}"
    return count, ""


def _crm_stats_count_batch_sends_today(today: str) -> int:
    """Sends recorded in today's morning_batch ledger (status == "sent").

    The idle-drive's autonomous email channel sends via morning_batch, which
    bypasses outreach ``send_message`` — so before 2026-07-03 the HUD showed
    ``emails_today: 0`` on a day with real SendGrid deliveries. Best-effort:
    any fault reads as 0 rather than failing the HUD.
    """
    from backend.common import storage

    count = 0
    try:
        root = storage.root()
        for base in (root / "artifacts" / "outreach", root / "outreach"):
            path = base / f"morning_batch_{today}.jsonl"
            if not path.is_file():
                continue
            with path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line.startswith("{"):
                        continue
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(row, dict) and row.get("status") == "sent":
                        count += 1
            break  # first existing ledger wins; the two are alternate mounts
    except Exception as exc:  # noqa: BLE001 — HUD telemetry must never raise
        _LOG.warning("crm_stats batch ledger read failed: %s", exc)
    return count


@asynccontextmanager
async def _gateway_lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Gateway-only lifespan: spawn/stop the cross-agent hub subscriber.

    Subscribes the gateway to the cross-agent Quorum Hub SSE stream so
    Samus receives governance events published by peer agents. The
    subscriber is launched on the gateway specifically (not every
    workcell) because the gateway is the operator-facing surface and
    one connection per Samus process is enough for visibility.

    Failure modes:
      * ``QUORUM_HUB_SUBSCRIBE_DISABLED=1`` → skip start, log + return.
      * Hub unreachable at boot → subscriber's own loop retries with
        backoff; boot is never blocked.
      * Subscriber raises during start() → log + swallow; gateway boots
        without the subscriber rather than crashing.
    """
    # Ecosystem-core skeleton boot step: signed charter/identity + immutable
    # integrity gate + the shared _shared.autonomy contract (ECO-ADR-0001).
    # ADDITIVE + DORMANT BY DEFAULT — the only path that can abort boot is the
    # operator-armed SAMUS_IMMUTABLE_GATE_MODE=enforce drift/tamper case; that
    # abort is INTENTIONALLY allowed to propagate (fail-closed). Every other
    # outcome is logged and boot continues exactly as before.
    try:
        from backend.identity.boot import ImmutableGateAbort, run_identity_boot

        run_identity_boot(app)
    except ImmutableGateAbort:
        _LOG.critical(
            "samus.gateway.identity_boot: immutable gate ENFORCE abort — "
            "refusing to bring the gateway up on a drifted/tampered baseline."
        )
        raise
    except Exception:  # noqa: BLE001 — identity boot is never on the critical path
        _LOG.exception("samus.gateway.identity_boot failed (non-fatal); continuing")

    # Cross-agent operator-relief forwarder: mirror stale pending-stake deals
    # to Anita's /api/relief/intake so they reach the Agora when the operator
    # is away. Started regardless of the hub-subscriber flag (independent
    # concern); dormant unless samus_agora_relief_forward_enabled. Fail-soft.
    try:
        from backend.common.config import get_settings as _get_settings
        from backend.standard.inter_agent.relief import start_relief_forwarder

        await start_relief_forwarder(app, _get_settings())
    except Exception:  # noqa: BLE001 — relief forwarder must never block gateway
        _LOG.exception("relief forwarder failed to start; continuing without it")

    # In-container control-tick loop: the autonomous revenue heartbeat. Drives
    # the stack control tick (stale sweep + staked-deal re-walk + cash-engine
    # enqueue) on an interval from INSIDE the always-on gateway, so the
    # revenue loop no longer depends on a host Windows scheduled task that
    # needs an interactive login (2026-06-13: that dependency cost a full
    # autonomous day of zero revenue actions). Default ON; fail-soft.
    try:
        from backend.gateway.control_tick_task import start_control_tick_loop

        await start_control_tick_loop(app)
    except Exception:  # noqa: BLE001 — heartbeat must never block the gateway
        _LOG.exception("control_tick loop failed to start; continuing without it")

    # In-container morning ritual (operator directive 2026-07-06): Samus
    # attests + emails the operator brief once per business day inside the
    # container, instead of relying on a Windows Task Scheduler entry. Same
    # rationale as the control tick above -- the stack has ONE boot-time
    # scheduled task (Launch_svc_Samus); every recurring behaviour lives in
    # the always-on container where the state is. Default ON; fail-soft.
    try:
        from backend.gateway.morning_ritual_task import start_morning_ritual_loop

        await start_morning_ritual_loop(app)
    except Exception:  # noqa: BLE001 -- ritual must never block the gateway
        _LOG.exception("morning_ritual loop failed to start; continuing without it")

    # In-container production-health tripwire (operator directive 2026-07-07):
    # Samus watches itself. The host-side "Samus Production Health" scheduled
    # task fires every ~15 min and wraps
    # ``python -m backend.observability.production_health_notify``; that entry
    # point is state-change throttled (persistent failure emails ONCE, recovery
    # emails once, repeats silent). Same rationale as the sibling drivers:
    # detection reads artifacts the gateway writes, so scheduling the tripwire
    # in-container removes the external orchestrator + interactive-login
    # dependency. Dedup + rate-limit stay in the underlying dispatch layer's
    # alert-state ledger -- this loop is a pure driver, no second cooldown.
    # Default ON; fail-soft.
    try:
        from backend.gateway.production_health_task import start_production_health_loop

        await start_production_health_loop(app)
    except Exception:  # noqa: BLE001 -- tripwire must never block the gateway
        _LOG.exception("production_health loop failed to start; continuing without it")

    # Production pulse (operator directive 2026-07-03): WATCH the idle signal
    # instead of sampling it every 30 minutes — when production goes quiet
    # during business hours, the idle-drive reasoning fires within a pulse
    # (default 120s) rather than waiting for the next control tick. Gated by
    # SAMUS_PRODUCTION_PULSE_ENABLED (default OFF); fail-soft.
    try:
        from backend.gateway.production_pulse_task import start_production_pulse_loop

        await start_production_pulse_loop(app)
    except Exception:  # noqa: BLE001 — pulse must never block the gateway
        _LOG.exception("production pulse failed to start; continuing without it")

    # In-container cold-dial pass (operator directive 2026-07-07; the cold-dial
    # lane had been dead 5 days). The daily cold call list used to be live-dialed
    # by a host cron running backend/voice/dialer.py; that cron was retired
    # 2026-07-03 and nothing in-container replaced the EXECUTION — the idle-drive
    # voice lane is consent-routed, so the cold Google-Places prospects fell
    # through to voicemail drafts and were never live-dialed again. This loop is
    # the in-container replacement (same shape as the control tick / morning
    # ritual above): during the dial window, when autonomous production is ARMED
    # and the business day is ATTESTED, it DECIDES here and delegates the dial to
    # the samus-voice workcell (POST /voice/dial_call_list over the signed mesh) —
    # voice holds the Vapi creds, the gateway does not (per-workcell secret
    # isolation). Voice runs dialer.dial_call_list(dry_run=False) with its own
    # TCPA / DNC / cooldown / cap fences, NOT a parallel dialer. Default ON;
    # fail-soft.
    try:
        from backend.gateway.cold_dial_task import start_cold_dial_loop

        await start_cold_dial_loop(app)
    except Exception:  # noqa: BLE001 — cold-dial loop must never block the gateway
        _LOG.exception("cold_dial loop failed to start; continuing without it")

    # In-container voice transcript-ingest cadence (voice -> UCB1 bandit learning
    # loop; the dead MIDDLE, 2026-07-07). The dialer stamps a variant_arm_id and
    # transcript_analyzer already flows each analyzed call's reward to the bandit
    # (attribution.record_outcome), but analyze_transcript only runs inside
    # run_ingest_pipeline, and nothing in-container pulled completed Vapi
    # transcripts into that pipeline's staging dir — so the reward never flowed
    # and the bandit never learned. Same root cause as the cold-dial lane: the
    # host cron that drove the post-call sweep was retired 2026-07-03. This loop
    # is the in-container replacement (same shape as the drivers above): on a
    # cadence it pulls recently-completed Vapi transcripts and, only when there
    # is new data, runs run_ingest_pipeline over them — turning the bandit's
    # learning ON. Read-only Vapi (GET /call); places no calls, so no dial-window
    # / armed / attested gate. Default ON; fail-soft.
    try:
        from backend.gateway.voice_ingest_task import start_voice_ingest_loop

        await start_voice_ingest_loop(app)
    except Exception:  # noqa: BLE001 — ingest loop must never block the gateway
        _LOG.exception("voice_ingest loop failed to start; continuing without it")

    # In-container ACTIVE cognition cadence: the autonomous tick loop that
    # wraps the Phase-F/G runner (backend/cognitive/cadence.py). Default ON
    # per operator direction; honours the MASTER switch (cognitive_loop_enabled)
    # every tick, so a runtime arm/disarm requires no gateway restart. Each
    # commercial sub-behaviour (meta / ACT-proposals / promotion / persona)
    # still honours its own flag inside the runner — disabling any one layer
    # does not stop the cadence loop. Fail-soft: a wiring fault here never
    # blocks the gateway.
    try:
        from backend.cognitive.cadence import start_cognition_cadence

        await start_cognition_cadence(app)
    except Exception:  # noqa: BLE001 — cadence must never block the gateway
        _LOG.exception("cognition cadence failed to start; continuing without it")

    # Nightly memory-consolidation timer (Tranche 3): distill -> promote ->
    # calibrate -> compress at ~02:00 local, inside the always-on container
    # (same shape as the control-tick loop). Default ON; fail-soft.
    try:
        from backend.cognitive.consolidation_task import start_consolidation_loop

        await start_consolidation_loop(app)
    except Exception:  # noqa: BLE001 — consolidation must never block the gateway
        _LOG.exception("consolidation loop failed to start; continuing without it")

    # In-container planning timer (Tranche 4): ensures the goal tree + daily
    # plans exist, then evaluates each active plan's assumptions against the
    # unified event stream every tick — a violated assumption regenerates the
    # plan (Plan B) automatically (escalating to the operator only when a replan
    # would breach budget posture / risk tier). Same shape as the control-tick
    # loop; default ON; fail-soft.
    try:
        from backend.planning.planner_task import start_planner_loop

        await start_planner_loop(app)
    except Exception:  # noqa: BLE001 — planner must never block the gateway
        _LOG.exception("planner loop failed to start; continuing without it")

    # In-container end-of-day timer: Samus closes out its own day (the three-way
    # EOD triangulation -> gameplan) in the operator's evening window, replacing
    # the retired ``Samus EOD Review`` Windows task. Same shape as morning_ritual;
    # default ON; fail-soft. Gated once-per-day by the eod_review artifact.
    try:
        from backend.gateway.eod_task import start_eod_loop

        await start_eod_loop(app)
    except Exception:  # noqa: BLE001 — eod must never block the gateway
        _LOG.exception("eod loop failed to start; continuing without it")

    async def _stop_relief() -> None:
        try:
            from backend.standard.inter_agent.relief import stop_relief_forwarder

            await stop_relief_forwarder(app)
        except Exception:  # noqa: BLE001 — best-effort teardown
            _LOG.exception("relief forwarder stop raised")
        try:
            from backend.gateway.control_tick_task import stop_control_tick_loop

            await stop_control_tick_loop(app)
        except Exception:  # noqa: BLE001 — best-effort teardown
            _LOG.exception("control_tick loop stop raised")
        try:
            from backend.gateway.morning_ritual_task import stop_morning_ritual_loop

            await stop_morning_ritual_loop(app)
        except Exception:  # noqa: BLE001 -- best-effort teardown
            _LOG.exception("morning_ritual loop stop raised")
        try:
            from backend.gateway.production_health_task import stop_production_health_loop

            await stop_production_health_loop(app)
        except Exception:  # noqa: BLE001 -- best-effort teardown
            _LOG.exception("production_health loop stop raised")
        try:
            from backend.gateway.production_pulse_task import stop_production_pulse_loop

            await stop_production_pulse_loop(app)
        except Exception:  # noqa: BLE001 — best-effort teardown
            _LOG.exception("production pulse stop raised")
        try:
            from backend.gateway.cold_dial_task import stop_cold_dial_loop

            await stop_cold_dial_loop(app)
        except Exception:  # noqa: BLE001 — best-effort teardown
            _LOG.exception("cold_dial loop stop raised")
        try:
            from backend.gateway.transcript_ingest_task import stop_transcript_ingest_loop

            await stop_transcript_ingest_loop(app)
        except Exception:  # noqa: BLE001 — best-effort teardown
            _LOG.exception("transcript_ingest loop stop raised")
        try:
            from backend.cognitive.cadence import stop_cognition_cadence

            await stop_cognition_cadence(app)
        except Exception:  # noqa: BLE001 — best-effort teardown
            _LOG.exception("cognition cadence stop raised")
        try:
            from backend.cognitive.consolidation_task import stop_consolidation_loop

            await stop_consolidation_loop(app)
        except Exception:  # noqa: BLE001 — best-effort teardown
            _LOG.exception("consolidation loop stop raised")
        try:
            from backend.planning.planner_task import stop_planner_loop

            await stop_planner_loop(app)
        except Exception:  # noqa: BLE001 — best-effort teardown
            _LOG.exception("planner loop stop raised")
        try:
            from backend.gateway.eod_task import stop_eod_loop

            await stop_eod_loop(app)
        except Exception:  # noqa: BLE001 — best-effort teardown
            _LOG.exception("eod loop stop raised")

    # ---- Canon L1 canonical memory (required core dependency) ---------------
    try:
        from _shared.memory.harness import wire_canonical_memory
        from backend.common.config import get_settings as _cm_settings
        from pathlib import Path as _CMPath
        _cm_s = _cm_settings()
        _samus_data = _CMPath(getattr(_cm_s, "samus_data_root", "D:/Hustleforge/Samus/data"))
        app.state.canonical_memory = wire_canonical_memory(
            agent_id="samus",
            data_root=_samus_data / "canonical_memory",
            app=app,
        )
    except Exception:  # noqa: BLE001
        _LOG.exception("canonical_memory: init failed (non-fatal)")

    if is_subscribe_disabled():
        _LOG.info("hub subscriber disabled via QUORUM_HUB_SUBSCRIBE_DISABLED")
        try:
            yield
        finally:
            await _stop_relief()
        return

    # Always register the default log handler so SSE arrivals are visible
    # in the gateway log even with no other handlers wired. Idempotent.
    register_handler(_default_log_handler)

    # Ingest Major's RBL band broadcasts into the commercial_wrap cache so the
    # RBL consumer sees a real band even without a live HTTP status endpoint.
    # Idempotent; failure to wire must not block the gateway boot.
    try:
        from backend.standard.inter_agent.rbl_band_handler import (
            register_rbl_band_handler,
        )
        register_rbl_band_handler()
    except Exception:  # noqa: BLE001 — RBL ingest is advisory; never block boot
        _LOG.exception("samus.gateway.rbl_band_handler.wire_failed")

    subscriber = get_subscriber()
    try:
        await subscriber.start()
        _LOG.info("hub subscriber started at %s", subscriber.url)
    except Exception:  # noqa: BLE001 — subscriber boot must not block gateway
        _LOG.exception("hub subscriber failed to start; continuing without it")

    try:
        yield
    finally:
        try:
            await subscriber.stop()
        except Exception:  # noqa: BLE001 — best-effort teardown
            _LOG.exception("hub subscriber stop raised")
        await _stop_relief()


def create_app():
    app = create_base_app(service_name="gateway", lifespan=_gateway_lifespan)

    # Tranche 3: experiment-registry operator routes (GET/POST /admin/experiments).
    from backend.experiments.routes import register_routes as _register_experiment_routes
    _register_experiment_routes(app)

    # Deliberation router: value-of-computation depth decision (POST /admin/deliberate).
    from backend.common.deliberation_routes import register_routes as _register_deliberation_routes
    _register_deliberation_routes(app)

    # Fail fast at startup if required secrets are missing in non-development
    # environments. Development keeps the per-request 503 path so tests and
    # local dev work without a real HMAC key. Staging/production must boot
    # configured or not at all.
    _settings = get_settings()
    if _settings.env != "development":
        _settings.require_env("shared_hmac_key")
        # The gateway is the HMAC signer, so inbound requests are NOT
        # verified by VerifyHMACMiddleware. The only inbound auth layer is
        # check_capability() — which is a complete no-op when authz_mode is
        # "off". Booting in that configuration means every gateway endpoint
        # (/dispatch/*, /autonomy/plan, /admin/*, /dlq/*) is unauthenticated.
        if getattr(_settings, "authz_mode", "off") == "off":
            raise RuntimeError(
                "samus.gateway.authz_required: SAMUS_AUTHZ_MODE must be set "
                "to 'audit' or 'enforce' in non-development environments. "
                "The gateway skips HMAC verification on inbound requests; "
                "capability checks are the only auth layer and are disabled "
                "when authz_mode='off'."
            )

    # v0.5/v0.6 Directed Capability Protocol layer wiring. Wrapped in
    # try/except: a broken governance boot must NOT block the gateway
    # (Samus is the revenue-bearing agent; failing the lifespan here
    # would freeze commerce).
    try:
        from backend.governance import get_protocol_layer

        _layer = get_protocol_layer()

        @app.on_event("startup")
        async def _start_protocol_layer() -> None:  # pragma: no cover - lifespan
            try:
                _layer.start_background_tasks(interval_sec=60)
                _LOG.info("samus.governance.protocol_layer.started: %s", _layer.status())
            except Exception as exc:  # noqa: BLE001
                _LOG.error("samus.governance.protocol_layer.startup_failed: %s", exc)

        @app.on_event("shutdown")
        async def _stop_protocol_layer() -> None:  # pragma: no cover - lifespan
            try:
                _layer.stop_background_tasks()
            except Exception as exc:  # noqa: BLE001
                _LOG.error("samus.governance.protocol_layer.shutdown_failed: %s", exc)

        @app.get("/governance/protocol_status")
        async def protocol_status() -> dict[str, Any]:
            return _layer.status()

    except Exception as exc:  # noqa: BLE001
        _LOG.error("samus.governance.protocol_layer.wire_failed: %s", exc)

    @app.post("/dispatch/{target}")
    async def dispatch(target: str, request: Request) -> dict[str, Any]:
        check_capability("gateway", "dispatch")

        settings = get_settings()
        if not settings.shared_hmac_key:
            raise HTTPException(status_code=503, detail="shared_hmac_key_unset")

        body = await request.json()
        envelope = _parse_envelope(body)

        # Mandatory simulation gate (HOTL T5): an external-effect action
        # (send/call/payment/publish) is refused unless a simulation was
        # recorded for its decision_id; HIGH/CRITICAL actions additionally
        # require the simulation to have predicted success. Internal actions
        # are not gated. Fail-closed: no recorded simulation -> 409 refusal.
        _enforce_simulation_gate(envelope)

        queue_url = sqs_dispatch.QUEUE_URLS.get(target)
        if queue_url:
            try:
                result = sqs_dispatch.enqueue_dispatch(
                    target,
                    task_id=envelope.task_id,
                    action=str(envelope.metadata.get("action") or "work"),
                    payload=envelope.payload,
                    metadata=envelope.metadata,
                    trace_id=str(envelope.metadata.get("trace_id") or "") or None,
                    idempotency_key=str(envelope.metadata.get("idempotency_key") or "") or None,
                )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc))
            return result

        # HTTP fallback
        try:
            base_url = resolve_target(target)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"unknown target: {target}")

        status, body_out = await gateway_service.dispatch_to_target(
            base_url, target, envelope.model_dump()
        )
        return {"status": status, "target": target, "task_id": envelope.task_id, "response": body_out}

    @app.post("/api/samus/review_opportunity")
    async def review_opportunity(request: Request) -> dict[str, Any]:
        """Cash Engine front door — the HALT-and-escalate revenue ingress.

        The single, hyper-specific entry the Automated Opportunity Review
        knocks on. HMAC + nonce are enforced by ``VerifyHMACMiddleware``
        (this path is deliberately NOT in the exempt set), so only callers
        holding the shared/per-service key reach it. The handler does not
        execute a revenue action: it validates the
        :class:`RevenueTriggerRequest`, hands it to the one
        ``review_opportunity`` logic entry which throws up the Codex Gate
        (Stake Sentence present? action Codex-clean?), and returns the
        structured verdict. A blocked gate escalates with the failing
        protocol named; only a clean pass enqueues the downstream sequence.

        The Stake Sentence is NOT read from the request — it is resolved
        server-side from the prospect's Opportunity, so a caller cannot
        assert its way past the human's signature.
        """
        check_capability("gateway", "dispatch")

        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="expected_json_object")
        try:
            req = RevenueTriggerRequest.model_validate(body)
        except Exception as exc:  # pydantic ValidationError
            raise HTTPException(status_code=400, detail=f"invalid_request: {exc}")

        result = cash_engine_service.review_opportunity(req)
        return result.model_dump()

    @app.post("/api/samus/cash_engine/drain")
    async def cash_engine_drain(request: Request) -> dict[str, Any]:
        """Pump the local cash-engine job queue through the staged sequence.

        The consumer side of the front door, for the logic-first (mock-queue)
        phase: an operator- or cron-triggered pass that walks every enqueued
        job through audit -> proposal -> contact -> outreach, Codex-gating each
        handoff, and returns the per-status summary. Modelled on
        ``/admin/control-tick``. Capability-gated (``control_tick``); body is
        optional (``{"limit": int}`` to bound the batch). When real SQS lands,
        a queue consumer replaces this manual pump.
        """
        check_capability("gateway", "control_tick")
        from backend.cash_engine import worker as cash_worker

        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 — empty/non-JSON body -> defaults
            body = {}
        limit: int | None = None
        if isinstance(body, dict) and body.get("limit") is not None:
            try:
                limit = int(body["limit"])
            except (TypeError, ValueError):
                limit = None
        return cash_worker.drain(limit=limit)

    @app.post("/api/samus/crm/reengagement_sweep")
    async def crm_reengagement_sweep_route(request: Request) -> dict[str, Any]:
        """Run the soft-no re-engagement sweep.

        Scans CRM CallStates for prospects whose last_outcome was
        ``not_interested`` past the cooldown window and fires a Cash Engine
        review with ``trigger_source="reengagement"`` for each. The cash
        engine outreach stage then routes through the promotional template
        rather than the cold opener.

        Dormant by default — when ``samus_reengagement_enabled`` is False the
        sweep returns an empty result. Capability-gated (``control_tick``);
        body is optional (``{"cooldown_days": int, "max_per_run": int}`` to
        override settings for this run).
        """
        check_capability("gateway", "control_tick")
        from backend.crm.reengagement_sweep import scan_for_reengagement_triggers

        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 — empty/non-JSON body -> defaults
            body = {}
        cooldown_days: int | None = None
        max_per_run: int | None = None
        if isinstance(body, dict):
            if body.get("cooldown_days") is not None:
                try:
                    cooldown_days = int(body["cooldown_days"])
                except (TypeError, ValueError):
                    cooldown_days = None
            if body.get("max_per_run") is not None:
                try:
                    max_per_run = int(body["max_per_run"])
                except (TypeError, ValueError):
                    max_per_run = None
        result = scan_for_reengagement_triggers(
            cooldown_days=cooldown_days, max_per_run=max_per_run,
        )
        return {
            "scanned": result.scanned,
            "eligible": result.eligible,
            "enqueued": result.enqueued,
            "escalated": result.escalated,
            "skipped_in_flight": result.skipped_in_flight,
            "skipped_hard_no": result.skipped_hard_no,
            "skipped_within_cooldown": result.skipped_within_cooldown,
            "skipped_no_opportunity": result.skipped_no_opportunity,
            "capped": result.capped,
            "cooldown_days": result.cooldown_days,
        }

    @app.post("/api/samus/cognition/cycle")
    async def cognition_cycle(request: Request) -> dict[str, Any]:
        """Phase-F FIRST LIVE CALLER of the cognitive stack — gated DORMANT.

        The single manual entrypoint that runs ONE cycle of the
        ``MetaCognitionEngine``-wrapped ``CognitiveLoop``. WIRED but DORMANT:

          * **Master-switch gate.** Gated by ``cognitive_loop_enabled``
            (env ``SAMUS_COGNITIVE_LOOP_ENABLED``, default OFF). When OFF this
            handler constructs/runs NOTHING — it returns a structured
            ``{"enabled": false, ...}`` no-op with HTTP 503 BEFORE importing the
            runner's heavy deps, so no CognitiveLoop / MetaCognitionEngine /
            LiveDomainProvider / LLM reasoner is ever built and no
            CRM/finance/LLM backend is touched.
          * **Auth gate.** Capability-gated like the other operator-triggered
            ``/api/samus/*`` pass routes (``control_tick``); on the gateway
            workcell that is the operator surface, and the route is additionally
            covered by VerifyHMACMiddleware (not in the exempt set), so only a
            signed caller reaches it.

        When ARMED it runs exactly one ``run_one_cycle(inp)`` and returns the
        cycle summary (reply, ok, compliance_blocked, stage names, and the
        propose-only proposal id/record ACT wrote). ACT is propose-only — NO
        effector / gate / send / network fires beyond the read-only PERCEIVE and
        the single budget-gated REASON LLM call. Each sub-behaviour still honours
        its own flag (meta / ACT-proposals / persona), so arming the master
        switch alone keeps the commercial sub-behaviours dormant.

        Body (all optional): ``{"user_text", "system_prompt", "channel",
        "plan_token", "metadata"}`` — shaped into a ``CycleInput``.

        NOTE: this is the ONLY caller wired in Phase F. No autonomous cadence is
        scheduled here — that is a later operator-gated step (see
        ``backend/cognitive/runner.py::_future_cadence_attach_point``).
        """
        check_capability("gateway", "control_tick")

        # Master-switch gate FIRST — before importing the runner's heavy deps,
        # so the dormant path constructs nothing and touches no backend.
        from backend.cognitive.runner import loop_enabled

        if not loop_enabled():
            # Pure no-op: nothing constructed, nothing run. 503 = the live caller
            # is wired but not armed (operator must set SAMUS_COGNITIVE_LOOP_ENABLED).
            raise HTTPException(
                status_code=503,
                detail={
                    "enabled": False,
                    "reason": "cognitive_loop_disabled",
                    "hint": "set SAMUS_COGNITIVE_LOOP_ENABLED=1 to arm the live caller",
                },
            )

        # Armed: build + run exactly one cycle. Imports are deferred to here so
        # the disabled path above never loads the cognitive stack.
        from backend.cognitive.cycle_models import CycleInput
        from backend.cognitive.runner import run_one_cycle

        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 — empty/non-JSON body -> all defaults
            body = {}
        if not isinstance(body, dict):
            body = {}

        metadata = body.get("metadata")
        inp = CycleInput(
            user_text=str(body.get("user_text") or ""),
            system_prompt=str(body.get("system_prompt") or ""),
            channel=str(body.get("channel") or "chat"),
            plan_token=str(body.get("plan_token") or ""),
            metadata=metadata if isinstance(metadata, dict) else {},
        )

        summary = await run_one_cycle(inp)
        return summary.to_dict()

    @app.get("/api/samus/cognition/health")
    async def cognition_health(request: Request) -> dict[str, Any]:
        """Cognitive stack health probe (Slice B — 2026-07-06).

        Read-only surface exposing the flag matrix + the cadence's in-memory
        state so operators can verify the loop is armed, the wrapper is live,
        and the cadence is ticking without POSTing a real cycle. Fail-safe:
        every field degrades to a sentinel on any error so the probe never 5xxs
        the caller during a partial outage.

        Fields:
          * ``flags`` — current effective settings the runner + cadence read.
          * ``cadence`` — task-alive flag + runtime snapshot (in_flight,
            backoff_mult, last_tick).
          * ``wrapper_mode`` — ``"active"`` when the meta wrapper runs its
            perception/reflection stages, ``"passthrough"`` when
            ``autonomy_meta_enabled`` is off (loop.cycle byte-identical to no
            wrapper), ``"disabled"`` when the master switch is off.
        """
        check_capability("gateway", "control_tick")

        flags: dict[str, Any] = {}
        try:
            from backend.common.config import get_settings

            s = get_settings()
            flags = {
                "cognitive_loop_enabled": bool(getattr(s, "cognitive_loop_enabled", False)),
                "autonomy_meta_enabled": bool(getattr(s, "autonomy_meta_enabled", False)),
                "cognitive_act_proposals_enabled": bool(getattr(s, "cognitive_act_proposals_enabled", False)),
                "cognition_proposal_promotion_enabled": bool(getattr(s, "cognition_proposal_promotion_enabled", False)),
                "cognition_cadence_enabled": bool(getattr(s, "cognition_cadence_enabled", False)),
                "cognition_cadence_interval_seconds": int(getattr(s, "cognition_cadence_interval_seconds", 0)),
                "cognition_cadence_jitter_seconds": int(getattr(s, "cognition_cadence_jitter_seconds", 0)),
                "autonomy_reinforcement_enabled": bool(getattr(s, "autonomy_reinforcement_enabled", False)),
                "autonomy_autotuner_enabled": bool(getattr(s, "autonomy_autotuner_enabled", False)),
                "autonomy_upgrade_enabled": bool(getattr(s, "autonomy_upgrade_enabled", False)),
                "persona_frame_enabled": bool(getattr(s, "persona_frame_enabled", False)),
                "persona_self_model_enabled": bool(getattr(s, "persona_self_model_enabled", False)),
            }
        except Exception as exc:  # noqa: BLE001 — probe never 5xxs
            flags = {"error": f"{type(exc).__name__}:{exc}"}

        if not flags.get("cognitive_loop_enabled"):
            wrapper_mode = "disabled"
        elif flags.get("autonomy_meta_enabled"):
            wrapper_mode = "active"
        else:
            wrapper_mode = "passthrough"

        cadence: dict[str, Any] = {"task_alive": False, "runtime": None}
        try:
            task = getattr(request.app.state, "cognition_cadence_task", None)
            runtime = getattr(request.app.state, "cognition_cadence_runtime", None)
            cadence["task_alive"] = bool(task is not None and not task.done())
            if runtime is not None and hasattr(runtime, "snapshot"):
                cadence["runtime"] = runtime.snapshot()
        except Exception as exc:  # noqa: BLE001
            cadence["error"] = f"{type(exc).__name__}:{exc}"

        return {
            "flags": flags,
            "wrapper_mode": wrapper_mode,
            "cadence": cadence,
        }

    @app.get("/api/samus/cognition/guidance")
    async def cognition_guidance_list(request: Request) -> dict[str, Any]:
        """Operator triage surface — list guidance recs with their lifecycle state.

        The externally-sourced recommendations (day-start briefing, EOD review,
        CODB reasoner, gameplan corroboration) have no automated triage path;
        this read-only route surfaces them so an operator (via forge-ui Control
        Center) can accept/reject them through the sibling POST routes below.

        Capability-gated (``control_tick``) like the sibling cognition routes
        (``cognition_health`` / ``cognition_cycle``). Read-only: scans the
        ledger and returns the latest state of each rec plus the effectiveness
        summary. Never mutates.

        Query params:
          * ``status`` — filter to one status (e.g. ``proposed``). Default
            ``open`` returns every non-terminal rec (the triage backlog);
            ``all`` returns everything.
          * ``limit`` — cap the number of returned recs (default 200, newest
            first by ``updated_ts``).
        """
        check_capability("gateway", "control_tick")
        from backend.cognitive.guidance import GuidanceLedger

        status = (request.query_params.get("status") or "open").strip().lower()
        try:
            limit = int(request.query_params.get("limit") or 200)
        except ValueError:
            limit = 200
        limit = max(1, min(limit, 1000))

        def _load() -> dict[str, Any]:
            led = GuidanceLedger()
            recs = led.all_latest()
            if status == "open":
                recs = [r for r in recs if not r.is_terminal()]
            elif status != "all":
                recs = [r for r in recs if r.status == status]
            recs.sort(key=lambda r: str(r.updated_ts), reverse=True)
            return {
                "status_filter": status,
                "count": len(recs),
                "items": [r.to_dict() for r in recs[:limit]],
                "summary": led.effectiveness_summary(),
            }

        return await run_in_threadpool(_load)

    @app.post("/api/samus/cognition/guidance/{recommendation_id}/accept")
    async def cognition_guidance_accept(
        recommendation_id: str, request: Request
    ) -> dict[str, Any]:
        """Operator ACCEPT of one guidance rec -> ACCEPTED (deliberate transition).

        Capability-gated (``control_tick``) like the sibling cognition routes.
        This is the wire-not-arm acceptance seam: it flips ONE rec's status to ``accepted``
        (optionally refining its action plan) so it flows into
        ``active_guidance_context()``. It does NOT execute the recommendation —
        acceptance and execution stay separate deliberate transitions.

        Body (optional): ``{"action_plan": ["step", ...]}`` to refine the plan.
        404 if the id is unknown.
        """
        check_capability("gateway", "control_tick")
        from backend.cognitive.guidance import GuidanceLedger

        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 — empty/non-JSON body -> defaults
            body = {}
        if not isinstance(body, dict):
            body = {}
        plan = body.get("action_plan")
        action_plan = (
            [str(s).strip() for s in plan if str(s).strip()]
            if isinstance(plan, list)
            else None
        )

        def _accept():
            return GuidanceLedger().accept(recommendation_id, action_plan=action_plan)

        rec = await run_in_threadpool(_accept)
        if rec is None:
            raise HTTPException(status_code=404, detail="unknown_recommendation_id")
        return {"ok": True, "record": rec.to_dict()}

    @app.post("/api/samus/cognition/guidance/{recommendation_id}/reject")
    async def cognition_guidance_reject(
        recommendation_id: str, request: Request
    ) -> dict[str, Any]:
        """Operator REJECT of one guidance rec -> REJECTED (terminal, no effector).

        Capability-gated (``control_tick``) like the sibling cognition routes.
        Marks ONE rec terminally rejected with the operator's reason recorded in
        ``outcome``; touches no effector. 404 if the id is unknown.

        Body (optional): ``{"reason": "why"}``.
        """
        check_capability("gateway", "control_tick")
        from backend.cognitive.guidance import GuidanceLedger

        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        reason = None
        if isinstance(body, dict) and body.get("reason"):
            reason = str(body["reason"]).strip() or None

        def _reject():
            return GuidanceLedger().reject(recommendation_id, reason=reason)

        rec = await run_in_threadpool(_reject)
        if rec is None:
            raise HTTPException(status_code=404, detail="unknown_recommendation_id")
        return {"ok": True, "record": rec.to_dict()}

    @app.post("/api/gateway/pre_shift_briefing")
    async def pre_shift_briefing_route(request: Request) -> dict[str, Any]:
        """Pre-Shift Strategic Briefing — the morning report of the intelligence cycle.

        Samus composes its intelligence package from live production state,
        consults OpenAI for strategic guidance, and INGESTS that guidance into
        the durable guidance ledger (classify -> assess -> tier -> track). Run on
        a Cloud Scheduler cadence before production activities begin.

        Capability-gated (``control_tick``). Fail-safe: an LLM failure yields a
        partial result with ``ingested=0`` rather than an error status, so the
        scheduled job records a clean outcome. Returns the briefing + ingestion
        counts + the current ledger effectiveness summary (the briefing text
        itself is omitted from the response to keep it compact).
        """
        check_capability("gateway", "control_tick")
        from backend.cognitive.intelligence_cycle import run_pre_shift_briefing

        result = await run_in_threadpool(run_pre_shift_briefing)
        return {
            "briefing_id": result.get("briefing_id"),
            "ingested": result.get("ingested", 0),
            "summary": result.get("summary", {}),
            "error": result.get("error"),
        }

    @app.post("/api/gateway/end_of_day_review")
    async def end_of_day_review_route(request: Request) -> dict[str, Any]:
        """End-of-Day Operational Review — the evening report of the intelligence cycle.

        Samus scores the day's guidance effectiveness and composes the
        Operational Review locally (zero LLM cost), then consults OpenAI once for
        tomorrow's guidance (ingested). Run on a Cloud Scheduler cadence after
        production activities conclude.

        Capability-gated (``control_tick``). Fail-safe. Body (optional):
        ``{"consult_openai": bool}`` to skip the tomorrow-guidance call.
        """
        check_capability("gateway", "control_tick")
        from backend.cognitive.intelligence_cycle import run_end_of_day_review

        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        consult = True
        if isinstance(body, dict) and body.get("consult_openai") is not None:
            consult = bool(body["consult_openai"])

        result = await run_in_threadpool(lambda: run_end_of_day_review(consult_openai=consult))
        return {
            "review_id": result.get("review_id"),
            "effectiveness": result.get("effectiveness", {}),
            "ingested": result.get("ingested", 0),
            "error": result.get("error"),
        }

    @app.post("/autonomy/plan")
    async def autonomy_plan(request: Request) -> dict[str, Any]:
        check_capability("gateway", "autonomy_plan")

        body = await request.json()
        envelope = _parse_envelope(body)

        payload = envelope.payload or {}
        objective = str(payload.get("objective") or "")
        actions = payload.get("actions") or []
        if not isinstance(actions, list):
            raise HTTPException(status_code=400, detail="actions_must_be_list")

        approvals = envelope.metadata.get("approvals") or []
        if not isinstance(approvals, list):
            approvals = []

        # 1) Pre-MAPE-K risk classification.
        level, reasons = governance.classify_risk(objective, actions)

        decision = governance.approval_decision(objective, actions, approvals)

        if level in ("high", "critical") and not decision.approved:
            return {
                "blocked": True,
                "task_id": envelope.task_id,
                "governance": {
                    "approved": False,
                    "risk_level": decision.risk_level,
                    "reasons": list(decision.reasons),
                    "required_approvals": list(decision.required_approvals),
                },
            }

        # 2) Run autonomy cycle (MAPE-K).
        result = autonomy.run_cycle(
            task_id=envelope.task_id,
            objective=objective,
            inputs=payload,
        )
        if not isinstance(result, dict):
            result = {"value": result}

        result["governance"] = {
            "approved": decision.approved,
            "risk_level": decision.risk_level,
            "reasons": list(decision.reasons),
            "required_approvals": list(decision.required_approvals),
        }
        return result

    # ---- Marketing self-campaign routes ------------------------------------
    #
    # Agent-facing surface for the self-marketing pipeline. Lets Samus invoke
    # its own brand brief refresh + monthly campaign cycle without operator
    # hand-holding. All three routes capability-gated (`control_tick` for
    # write paths, `list_tasks` for the read path).

    @app.post("/api/samus/marketing/campaign/run")
    async def marketing_campaign_run(request: Request) -> dict[str, Any]:
        """Run one monthly self-marketing campaign cycle.

        Body fields (all optional): ``month``, ``year`` (default = today),
        ``dry_run`` (default false), ``campaign`` (default "hustleforge").
        Idempotent: re-running for the same (campaign, month, year) resumes
        from the persisted plan.json, skipping steps already done.
        """
        check_capability("gateway", "control_tick")
        from datetime import datetime, timezone
        from backend.marketing.campaign_cycle import run_monthly_campaign
        from backend.marketing.self_campaign import HUSTLEFORGE_CAMPAIGN

        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        if not isinstance(body, dict):
            body = {}

        now = datetime.now(timezone.utc)
        month = int(body.get("month") or now.month)
        year = int(body.get("year") or now.year)
        dry_run = bool(body.get("dry_run", False))
        campaign_name = str(body.get("campaign") or "hustleforge").lower()
        if campaign_name not in ("hustleforge",):
            raise HTTPException(status_code=400, detail=f"unknown_campaign: {campaign_name}")

        result = run_monthly_campaign(
            month=month, year=year, campaign=HUSTLEFORGE_CAMPAIGN, dry_run=dry_run,
        )
        return {
            "ok": result.ok,
            "campaign_id": result.campaign_id,
            "cycle_month": result.cycle_month,
            "plan_path": result.plan_path,
            "steps": [
                {"id": s.id, "type": s.type, "status": s.status,
                 "elapsed_ms": s.elapsed_ms, "detail": s.detail}
                for s in result.steps
            ],
            "summary": result.summary,
        }

    @app.get("/api/samus/marketing/campaign/status")
    async def marketing_campaign_status(
        campaign: str = "hustleforge",
        month: int | None = None,
        year: int | None = None,
    ) -> dict[str, Any]:
        """Read the persisted plan.json for one (campaign, month, year).

        Returns ``{exists: false, ...}`` when nothing has run yet for that
        cycle, so the agent can decide whether to invoke /run.
        """
        check_capability("gateway", "list_tasks")
        import json
        from datetime import datetime, timezone
        from pathlib import Path

        now = datetime.now(timezone.utc)
        m = int(month or now.month)
        y = int(year or now.year)
        campaign_id = campaign.lower().replace(" ", "_")
        cycle_month = f"{y:04d}-{m:02d}"

        try:
            from backend.common import storage
            base = storage.root()
        except Exception:  # noqa: BLE001
            base = Path("/opt/samus/data/artifacts")
        plan_path = base / "marketing" / "campaigns" / campaign_id / cycle_month / "plan.json"
        if not plan_path.exists():
            return {
                "exists": False,
                "campaign_id": campaign_id,
                "cycle_month": cycle_month,
                "plan_path": str(plan_path),
            }
        try:
            raw = json.loads(plan_path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=f"plan_read_failed: {exc}")
        return {
            "exists": True,
            "campaign_id": campaign_id,
            "cycle_month": cycle_month,
            "plan_path": str(plan_path),
            "plan": raw,
        }

    @app.post("/api/samus/marketing/brief/refresh")
    async def marketing_brief_refresh(request: Request) -> dict[str, Any]:
        """Regenerate the brand brief for a campaign and persist it.

        Body fields (all optional): ``campaign`` (default "hustleforge"),
        ``extra_facts`` (list of website-derived strings). Falls back to the
        deterministic template (no LLM cost) when no API key is available.
        """
        check_capability("gateway", "control_tick")
        import json
        import os
        from dataclasses import asdict
        from pathlib import Path
        from backend.marketing.brand_brief import generate_brand_brief
        from backend.marketing.self_campaign import HUSTLEFORGE_CAMPAIGN

        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        if not isinstance(body, dict):
            body = {}
        campaign_name = str(body.get("campaign") or "hustleforge").lower()
        if campaign_name not in ("hustleforge",):
            raise HTTPException(status_code=400, detail=f"unknown_campaign: {campaign_name}")
        extra_facts = body.get("extra_facts") or []
        if not isinstance(extra_facts, list):
            extra_facts = []

        key = os.environ.get("ANTHROPIC_API_KEY")
        brief = generate_brand_brief(
            HUSTLEFORGE_CAMPAIGN, anthropic_api_key=key, extra_facts=list(extra_facts),
        )

        try:
            from backend.common import storage
            base = storage.root()
        except Exception:  # noqa: BLE001
            base = Path("/opt/samus/data/artifacts")
        campaign_id = HUSTLEFORGE_CAMPAIGN.brand.lower().replace(" ", "_")
        brief_dir = base / "marketing" / "briefs" / campaign_id
        brief_dir.mkdir(parents=True, exist_ok=True)
        brief_path = brief_dir / "brand_brief.json"
        brief_path.write_text(json.dumps(asdict(brief), indent=2), encoding="utf-8")

        return {
            "ok": True,
            "campaign_id": campaign_id,
            "brief_path": str(brief_path),
            "used_llm": brief.used_llm,
            "llm_cost_usd": brief.llm_cost_usd,
            "brief": asdict(brief),
        }

    @app.get("/dlq/archive")
    async def dlq_archive(limit: int = 100) -> dict[str, Any]:
        check_capability("gateway", "dlq_read")
        items = dlq.read_archive(limit=limit)
        return {"limit": limit, "items": items}

    @app.get("/dlq/{service}")
    async def dlq_pending(service: str, limit: int = 50) -> dict[str, Any]:
        check_capability("gateway", "dlq_read")
        try:
            items = dlq.read_pending(service, limit=limit)
        except ValueError as exc:
            # dlq rejects a path-traversal / malformed service name — CWE-22.
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"service": service, "limit": limit, "items": items}

    @app.get("/admin/llm_budgets")
    async def llm_budgets() -> dict[str, Any]:
        """Per-workcell LLM token budget snapshot.

        Returns the daily counters + EMA-driven adaptive quota for every
        workcell that calls Anthropic. Pure read; safe to hit from
        operator dashboards on any cadence. Gracefully degrades when the
        budget store backend is unreachable — fields stay zeroed rather
        than raising.
        """
        check_capability("gateway", "budget_admin")
        store = llm_budget.get_store()
        rows: list[dict[str, Any]] = []
        for workcell in _LLM_WORKCELLS:
            try:
                snap = store.snapshot(workcell)
            except Exception as exc:  # noqa: BLE001 - never break this view
                rows.append({
                    "workcell": workcell,
                    "error": f"snapshot_failed: {exc}",
                })
                continue
            quota = compute_quota(
                store.base_token_budget,
                efficiency_ema=snap.efficiency_ema,
                efficiency_call_count=snap.efficiency_call_count,
                floor_pct=store.floor_pct,
            )
            remaining = max(0, quota - snap.total_tokens_today)
            rows.append({
                "workcell": workcell,
                "bucket_day": snap.bucket_day,
                "quota_tokens": quota,
                "used_tokens": snap.total_tokens_today,
                "remaining_tokens": remaining,
                "input_tokens_today": snap.input_tokens_today,
                "output_tokens_today": snap.output_tokens_today,
                "call_count_today": snap.call_count_today,
                "success_count_today": snap.success_count_today,
                "failure_count_today": snap.failure_count_today,
                "error_count_today": snap.error_count_today,
                "efficiency_ema": round(snap.efficiency_ema, 4),
                "efficiency_call_count": snap.efficiency_call_count,
                "last_updated": snap.last_updated,
            })
        return {
            "base_token_budget": store.base_token_budget,
            "ema_alpha": store.ema_alpha,
            "floor_pct": store.floor_pct,
            "workcells": rows,
        }

    @app.get("/admin/tasks")
    async def admin_tasks(
        status: str | None = "open", limit: int = 50,
    ) -> dict[str, Any]:
        """Operator-facing view of the CRM operator-task queue.

        Proxies ``GET /crm/operator-tasks`` on the ``samus-crm`` workcell.
        Returns the raw OperatorTaskList shape so the operator can pull it
        from one URL without remembering each workcell's internal path.

        The CRM workcell mounts ``VerifyHMACMiddleware`` on every route, so
        the proxy MUST sign the call. We use the shared GET-signing helper
        which signs path-only (no query string); the gateway's ``status``
        and ``limit`` query params are accepted on this route for backward
        compatibility but are NOT forwarded — the CRM route's own defaults
        (``status="open"``, ``limit=50``) win. Callers needing other values
        must hit the CRM workcell directly.

        Capability-gated (``list_tasks``). Degrades to a structured error
        body (still 200) when CRM is unreachable so dashboards don't have
        to special-case transport failures.
        """
        check_capability("gateway", "list_tasks")
        settings = get_settings()
        crm_base = settings.gateway_urls.get("crm")
        if not crm_base:
            raise HTTPException(
                status_code=503, detail="crm_url_not_configured",
            )
        crm_path = "/crm/operator-tasks"
        try:
            from backend.common.http_client import _build_signed_get_request
            url, headers = _build_signed_get_request(
                crm_base, crm_path, secret=None,
            )
        except RuntimeError as exc:  # HMAC key unset
            _LOG.warning("admin_tasks sign-prep failed: %s", exc)
            return {
                "tasks": [], "count": 0, "scan_truncated": False,
                "ddb_error": f"crm_sign_unconfigured: {exc.__class__.__name__}",
            }
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(url, headers=headers)
        except (httpx.TimeoutException, httpx.ConnectError) as exc:
            _LOG.warning("admin_tasks upstream failed: %s", exc)
            return {
                "tasks": [], "count": 0, "scan_truncated": False,
                "ddb_error": f"crm_unreachable: {exc.__class__.__name__}",
            }
        # S3: post-hoc bound the upstream body so a misbehaving CRM workcell
        # can't OOM the gateway (the revenue-bearing surface). The response is
        # otherwise decoded with resp.json() exactly as before.
        try:
            check_httpx_size(
                resp, max_bytes=INTER_WORKCELL_MAX_BYTES, source="crm_tasks",
            )
            body = resp.json()
        except ResponseTooLarge as exc:
            _LOG.warning("admin_tasks upstream over cap: %s", exc)
            return {
                "tasks": [], "count": 0, "scan_truncated": False,
                "ddb_error": "crm_response_too_large",
            }
        except ValueError:
            return {
                "tasks": [], "count": 0, "scan_truncated": False,
                "ddb_error": f"crm_bad_response: status={resp.status_code}",
            }
        if not isinstance(body, dict):
            return {
                "tasks": [], "count": 0, "scan_truncated": False,
                "ddb_error": "crm_non_object_response",
            }
        return body

    @app.get("/admin/conversion_funnel")
    async def admin_conversion_funnel() -> dict[str, Any]:
        """Operator-facing conversion-funnel leak analysis.

        Proxies ``GET /crm/metrics/funnel`` on the ``samus-crm`` workcell so
        the operator can pull the funnel roll-up (per-stage counts +
        adjacent-stage conversion rates showing where deals leak) from the
        same ``/admin/*`` surface as the LLM-budget + task views.

        The CRM workcell mounts ``VerifyHMACMiddleware`` on every route, so
        the proxy MUST sign the call. We use the shared GET-signing helper
        which signs path-only (no query string); the funnel route takes no
        query params, so signing is straightforward.

        Capability-gated (``budget_admin`` — the operator-metrics capability
        already used by ``/admin/llm_budgets``). Degrades to a structured
        error body (still 200) when CRM is unreachable so dashboards don't
        have to special-case transport failures.
        """
        check_capability("gateway", "budget_admin")
        settings = get_settings()
        crm_base = settings.gateway_urls.get("crm")
        if not crm_base:
            raise HTTPException(
                status_code=503, detail="crm_url_not_configured",
            )
        crm_path = "/crm/metrics/funnel"
        try:
            from backend.common.http_client import _build_signed_get_request
            url, headers = _build_signed_get_request(
                crm_base, crm_path, secret=None,
            )
        except RuntimeError as exc:  # HMAC key unset
            _LOG.warning("admin_conversion_funnel sign-prep failed: %s", exc)
            return {
                "stages": {}, "stage_order": [], "transitions": [],
                "overall_conversion_rate": 0.0, "total_events": 0,
                "error": f"crm_sign_unconfigured: {exc.__class__.__name__}",
            }
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(url, headers=headers)
        except (httpx.TimeoutException, httpx.ConnectError) as exc:
            _LOG.warning("admin_conversion_funnel upstream failed: %s", exc)
            return {
                "stages": {}, "stage_order": [], "transitions": [],
                "overall_conversion_rate": 0.0, "total_events": 0,
                "error": f"crm_unreachable: {exc.__class__.__name__}",
            }
        # S3: post-hoc bound the upstream body (see /admin/tasks).
        try:
            check_httpx_size(
                resp, max_bytes=INTER_WORKCELL_MAX_BYTES, source="crm_funnel",
            )
            body = resp.json()
        except ResponseTooLarge as exc:
            _LOG.warning("admin_conversion_funnel upstream over cap: %s", exc)
            return {
                "stages": {}, "stage_order": [], "transitions": [],
                "overall_conversion_rate": 0.0, "total_events": 0,
                "error": "crm_response_too_large",
            }
        except ValueError:
            return {
                "stages": {}, "stage_order": [], "transitions": [],
                "overall_conversion_rate": 0.0, "total_events": 0,
                "error": f"crm_bad_response: status={resp.status_code}",
            }
        if not isinstance(body, dict):
            return {
                "stages": {}, "stage_order": [], "transitions": [],
                "overall_conversion_rate": 0.0, "total_events": 0,
                "error": "crm_non_object_response",
            }
        return body

    @app.get("/admin/journey/{prospect_id}")
    async def admin_journey(prospect_id: str, limit: int = 1000) -> dict[str, Any]:
        """Chronological business-event journey for one prospect.

        Reads the unified business-event ledger
        (:mod:`backend.common.business_events`) directly — no workcell proxy
        needed, the ledger is a shared telemetry file. Returns oldest-first
        so the operator reads the journey top-to-bottom:
        lead.created -> lead.enriched -> email.sent -> call.placed -> ... ->
        payment.received.

        Capability-gated (``journey_read``). read_events never raises, so a
        ledger outage degrades to an empty journey rather than a 500.
        """
        check_capability("gateway", "journey_read")
        from backend.common.business_events import read_events
        events_out = read_events(prospect_id=prospect_id, limit=limit)
        return {
            "prospect_id": prospect_id,
            "events": events_out,
            "count": len(events_out),
        }

    @app.post("/admin/control-tick")
    async def control_tick(request: Request) -> dict[str, Any]:
        """Run one stack-level control-loop tick (observe -> decide).

        This is the producer the ``entropy`` + ``portfolio_controller``
        workcells were built for: a single pass that calls
        ``entropy.scan`` then ``portfolio_controller.run_rebalance``
        in-process, records the combined snapshot to the control-tick
        JSONL ledger, and returns it.

        Request body is optional; all fields default sensibly so an
        operator can ``curl -X POST`` it with an empty body:

          ``{"task_id": str?,
             "entropy_inputs": {EntropyScanRequest fields}?,
             "workcells": [WorkcellInput dicts]?}``

        Best-effort: a failure in either sub-call is captured as a
        structured ``error`` on that stage; the tick still returns 200
        with the partial snapshot and ``ok=False``. Idempotent and safe
        to call on any cadence — it records recommendations but does NOT
        enforce quota changes (see ``enforcement_note`` in the result).

        Modelled on ``intake``'s ``/intake/poll-inbox``: a manual /
        scheduled trigger returning per-pass results.
        """
        check_capability("gateway", "control_tick")

        # Body is optional — an empty POST is a valid all-defaults tick.
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 - empty/non-JSON body -> defaults
            body = {}
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="expected_json_object")

        task_id = body.get("task_id")
        entropy_inputs = body.get("entropy_inputs")
        workcells = body.get("workcells")
        if task_id is not None and not isinstance(task_id, str):
            raise HTTPException(status_code=422, detail="task_id_must_be_string")
        if entropy_inputs is not None and not isinstance(entropy_inputs, dict):
            raise HTTPException(
                status_code=422, detail="entropy_inputs_must_be_object",
            )
        if workcells is not None and not isinstance(workcells, list):
            raise HTTPException(
                status_code=422, detail="workcells_must_be_list",
            )

        return control_tick_mod.run_control_tick(
            task_id=task_id or "",
            entropy_inputs=entropy_inputs or {},
            workcells=workcells or [],
        )

    @app.get("/admin/control-ticks")
    async def control_ticks(limit: int = 50) -> dict[str, Any]:
        """Recent control-loop ticks from the JSONL ledger.

        Pure read; safe to hit from operator dashboards on any cadence.
        Tails the control-tick ledger (newest last). Degrades to an empty
        list + ``error`` string when the ledger is unreadable rather than
        raising, so dashboards don't have to special-case a telemetry
        outage. Capability-gated (``control_tick``).
        """
        check_capability("gateway", "control_tick")
        return control_tick_ledger.recent_ticks(limit=limit)

    # Economics surfaces (HOTL Tranche 2): GET /admin/roi + GET /admin/arbitration.
    __import__("backend.finance.roi", fromlist=["register_economics_admin_routes"]).register_economics_admin_routes(app)

    # Planning + explainability + command-center surfaces (HOTL Tranche 4):
    # GET /autonomy/plan (inspect), GET /admin/decisions[/{id}],
    # GET/POST /admin/approvals[/decide], GET /admin/command_center.
    from backend.planning.routes import register_planning_routes
    register_planning_routes(app)

    # Reputation surface (HOTL Tranche 5): GET /admin/reputation.
    __import__("backend.common.reputation", fromlist=["register_reputation_admin_routes"]).register_reputation_admin_routes(app)

    @app.get("/api/crm/stats")
    async def crm_stats() -> dict[str, Any]:
        """Daily CRM roll-up for the forge-ui Samus HUD.

        Composes two sources into one operator-facing snapshot:

          * CRM ``GET /crm/metrics/daily-stats`` — CallStates touched today,
            bucketed by ``last_outcome``. Covers EVERY call surface (operator
            hand-dialed, voice-agent end-of-call webhook, scheduled-send) —
            they all upsert through ``backend.crm.service.upsert_call_state``.
          * Outreach audit JSONL — count of ``send_message`` events with
            ``status == "completed"`` whose ``ts`` starts with today. The
            audit hashes the input/output payloads so channel can't be
            split here; in practice email is the only wired send path
            today (voice is gated by ``outreach_voice_send_enabled``).

        Returned shape (zeroed on transport failure rather than 500-ing —
        operator dashboards don't have to special-case):

          ``{"date", "calls_today", "calls_goal", "emails_today",
             "emails_goal", "booked_today", "followups_today",
             "connect_rate", "reengagement_queued_today"}``

        Cached per-UTC-date for 30 s so the HUD's per-render hit doesn't
        translate into a DDB scan + filesystem scan on every page load.
        Capability-gated (``crm_stats``).
        """
        check_capability("gateway", "crm_stats")
        # Pacific business day when armed (SAMUS_CRM_STATS_BUSINESS_TZ=1 on BOTH
        # gateway + crm), else the original UTC calendar day. The proxy signs
        # path-only, so the gateway can't send the day to CRM — both sides must
        # compute the SAME day independently; keeping this in lock-step with the
        # crm route's default is what makes the range line up. See daily_stats_route.
        if os.getenv("SAMUS_CRM_STATS_BUSINESS_TZ", "").strip().lower() in (
            "1", "true", "yes", "on",
        ):
            from backend.common.dates import business_today  # noqa: PLC0415
            today = business_today()
        else:
            today = _dt.datetime.utcnow().date().isoformat()

        cached = _crm_stats_cache_get(today)
        if cached is not None:
            return cached

        calls_goal = _stats_goal("SAMUS_CRM_CALLS_GOAL",
                                 _CRM_STATS_CALLS_GOAL_DEFAULT)
        emails_goal = _stats_goal("SAMUS_CRM_EMAILS_GOAL",
                                  _CRM_STATS_EMAILS_GOAL_DEFAULT)

        crm_part = await _crm_stats_fetch_call_state(today)
        emails_today, outreach_error = _crm_stats_count_outreach_today(today)
        # Autonomous batch sends live in a different ledger than operator
        # send_message events; sum both so the HUD reflects every real send.
        emails_today += _crm_stats_count_batch_sends_today(today)

        calls = int(crm_part.get("calls_today") or 0)
        booked = int(crm_part.get("booked_today") or 0)
        followups = int(crm_part.get("followups_today") or 0)
        connect_rate = (
            f"{round(booked / calls * 100)}%" if calls else "0%"
        )

        # Soft-no re-engagement counter — how many prospects the sweep
        # successfully requeued today (backend/crm/reengagement_sweep.py).
        # Cheap JSONL line-count over today's records; degrades to 0 on a
        # missing ledger so the HUD never 500s when the feature is dormant.
        try:
            from backend.crm.reengagement_sweep import count_queued_today
            reengagement_queued_today = count_queued_today(today=today)
        except Exception as exc:  # noqa: BLE001 — never break the HUD on this
            _LOG.warning("crm_stats: reengagement counter failed: %s", exc)
            reengagement_queued_today = 0

        errors: dict[str, str] = {}
        if crm_part.get("error"):
            errors["crm"] = str(crm_part["error"])
        if outreach_error:
            errors["outreach"] = outreach_error

        result: dict[str, Any] = {
            "date": today,
            "calls_today": calls,
            "calls_goal": calls_goal,
            "emails_today": emails_today,
            "emails_goal": emails_goal,
            "booked_today": booked,
            "followups_today": followups,
            "connect_rate": connect_rate,
            "reengagement_queued_today": reengagement_queued_today,
        }
        if errors:
            result["errors"] = errors

        _crm_stats_cache_set(today, result)
        return result

    # Canon §4 pack wiring. Samus has no manifest resolver / profile
    # JSON (unlike Major) -- each workcell is its own FastAPI app built
    # via ``create_base_app``, so the per-agent ``include_packs`` /
    # ``packs_in_order`` machinery would need to be invented just to
    # mount one pack. Instead the gateway, as the operator-facing
    # workcell (``/dispatch/*``, ``/autonomy/plan``, ``/admin/*``),
    # imports the pack directly and calls its top-level Canon §4
    # ``register(app)`` hook. The hook is kwargs-driven so it pulls
    # ``SAMUS_OPERATOR_TOKEN`` + ``SAMUS_DATA_ROOT`` from the
    # environment exactly as it would under a manifest-resolver loop.
    # A failure here aborts boot loudly -- silent skip would mask the
    # console being absent from a service that advertises it.
    import importlib  # noqa: PLC0415 -- deferred until after route block
    _pack_module = importlib.import_module("backend.packs.operator_console")
    _pack_register = getattr(_pack_module, "register", None)
    if _pack_register is None:
        raise RuntimeError(
            "pack 'operator_console' at 'backend.packs.operator_console' "
            "does not expose a top-level register(app) hook"
        )
    _pack_register(app)

    # Cross-agent Quorum VOTE protocol (2026-06-01 rework). Mount Samus's
    # VOTER endpoint POST /quorum/vote on the operator-facing gateway (the
    # same workcell that already runs the inter-agent hub subscriber). The
    # route is WIRED but DORMANT: it returns 503 before any work until the
    # operator sets SAMUS_QUORUM_VOTING_ENABLED=1, and it FAIL-CLOSED verifies
    # an inbound AgentEnvelope from the collector (Major) before reasoning.
    # Wrapped in try/except so a wiring failure cannot freeze the revenue-
    # bearing gateway (same defensive posture as the protocol-layer wiring).
    try:
        register_quorum_vote_route(app)
    except Exception as exc:  # noqa: BLE001
        _LOG.error("samus.gateway.quorum_vote_route.wire_failed: %s", exc)

    # Agora v2 contribution endpoint (POST /agora/contribute). WIRED but DORMANT:
    # gated by SN_AGORA_CONTRIBUTE_ENABLED (default off). Fail-closed verifies an
    # inbound HMAC AgentEnvelope from the Agora collector (Anita) before returning
    # Samus's unique commercial/resource evidence. Same defensive wrap so a wiring
    # failure cannot freeze the revenue-bearing gateway.
    try:
        from backend.standard.inter_agent.agora_contribute_route import (
            register as register_agora_contribute_route,
        )

        register_agora_contribute_route(app)
    except Exception as exc:  # noqa: BLE001
        _LOG.error("samus.gateway.agora_contribute_route.wire_failed: %s", exc)

    # Reward/harm readout endpoint (POST /inter_agent/reward-summary). WIRED but
    # DORMANT: gated by SN_REWARD_READOUT_ENABLED (default off). Fail-closed
    # verifies an inbound HMAC AgentEnvelope from Darwin before returning an
    # AGGREGATE reward/harm summary (no per-opportunity rows) — the W5 revenue-
    # feedback signal Darwin's EvolutionLoop pulls. Same defensive wrap so a
    # wiring failure cannot freeze the revenue-bearing gateway.
    try:
        from backend.standard.inter_agent.reward_readout_route import (
            register as register_reward_readout_route,
        )

        register_reward_readout_route(app)
    except Exception as exc:  # noqa: BLE001
        _LOG.error("samus.gateway.reward_readout_route.wire_failed: %s", exc)

    return app


app = create_app()
