"""Prospecting workcell FastAPI service.

Endpoints:
  POST /work        — TaskEnvelope wrapper (gateway dispatch path)
  POST /discover    — direct DiscoveryRequest body

The /work endpoint dispatches by ``envelope.metadata["action"]`` (or defaults
to ``"discover"`` when the key is absent for backwards compatibility):
  - "discover"                        -> process_discovery pipeline
  - "build_call_sheet"                -> process_discovery (alias; same pipeline)
  - "daily_supply"                    -> run ONE full in-stack daily prospecting
                                         fire (geo-ring -> discovery -> call-list
                                         artifacts), in the background. The
                                         self-supply seam: the cash engine calls
                                         this when the campaign portfolio starves
                                         on a missing daily call list, so input
                                         replenishment is reasoning-driven, not
                                         host-cron-driven. Idempotent: today's
                                         list already present -> no-op; a fire
                                         already in flight -> no second fire.
  - "analyze_business"                -> intelligence analysis + opportunity scoring
  - "score_deal"                      -> deal probability + tier classification
  - "generate_dynamic_script"         -> adaptive script from intel signals
  - "generate_dynamic_script_with_pivot" -> adaptive script + secondary pivot

Capability check runs before any business logic on every handler path.
"""
from __future__ import annotations

import logging
import threading
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import FastAPI, HTTPException, Request

from backend.common import feedback_store
from backend.common.app_factory import create_base_app
from backend.common.capabilities import check_capability
from backend.common.models import TaskEnvelope

from . import deal_scoring, dynamic_script, intelligence
from .models import DiscoveryRequest
from .service import process_discovery

_LOG = logging.getLogger("samus.prospecting.app")


def _parse_envelope(body: Any) -> TaskEnvelope:
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="expected_json_object")
    try:
        return TaskEnvelope.model_validate(body)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"invalid_envelope: {exc}")


# ---------------------------------------------------------------------------
# daily_supply — one-shot, idempotent, backgrounded call-list replenishment.
# A full prospecting fire takes minutes (Places calls per zip x industry), so
# the handler never blocks the /work request: it spawns a daemon thread and
# ACKs immediately. The caller (cash-engine self-supply) re-checks the
# call-list evidence on its next control tick instead of waiting here.
# ---------------------------------------------------------------------------
_SUPPLY_LOCK = threading.Lock()
_SUPPLY_STATE: dict[str, Any] = {"running": False, "started_ts": None, "last_result": None}


def _todays_call_list_path():
    from backend.common import storage
    from backend.common.us_timezones import business_today

    return storage.root() / "daily_calls" / f"call_list_{business_today().isoformat()}.csv"


def _run_supply_fire() -> None:
    """Daemon-thread body: one cadence tick, state cleared no matter what."""
    from .cadence_task import run_prospecting_tick

    try:
        result = run_prospecting_tick()
        with _SUPPLY_LOCK:
            _SUPPLY_STATE["last_result"] = result
        _LOG.info("daily_supply fire done: %s", result)
    except Exception as exc:  # noqa: BLE001 — a failed fire must release the slot
        with _SUPPLY_LOCK:
            _SUPPLY_STATE["last_result"] = {"error": str(exc)}
        _LOG.warning("daily_supply fire failed: %s", exc)
    finally:
        with _SUPPLY_LOCK:
            _SUPPLY_STATE["running"] = False


def _handle_daily_supply() -> dict[str, Any]:
    check_capability("prospecting", "discover")
    try:
        csv_path = _todays_call_list_path()
        if csv_path.is_file():
            return {"status": "already_supplied", "call_list": csv_path.name}
    except Exception as exc:  # noqa: BLE001 — evidence check is best-effort
        _LOG.warning("daily_supply evidence check failed: %s", exc)
    with _SUPPLY_LOCK:
        if _SUPPLY_STATE["running"]:
            return {
                "status": "in_flight",
                "started_ts": _SUPPLY_STATE["started_ts"],
            }
        _SUPPLY_STATE["running"] = True
        _SUPPLY_STATE["started_ts"] = time.time()
    threading.Thread(target=_run_supply_fire, name="daily-supply", daemon=True).start()
    return {"status": "started"}


def _parse_discovery_req(payload: Any) -> DiscoveryRequest:
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="expected_discovery_request_object")
    try:
        return DiscoveryRequest.model_validate(payload)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"invalid_discovery_request: {exc}")


def _handle_analyze_business(payload: Any) -> dict[str, Any]:
    """Run the full intelligence analysis pipeline and return combined result."""
    check_capability("prospecting", "analyze_business")
    if not isinstance(payload, dict):
        payload = {}
    _LOG.info("analyze_business action started")
    signals = intelligence.analyze_business(payload)
    scores = intelligence.score_opportunity(signals)
    products = intelligence.map_products(scores)
    angle = intelligence.determine_pitch_angle(
        signals, scores,
        learned_performance=feedback_store.get_angle_performance(),
    )
    result = {
        "signals":     signals,
        "scores":      scores,
        "products":    products,
        "pitch_angle": angle,
    }
    _LOG.info(
        "analyze_business complete",
        extra={"pitch_angle": angle, "primary_product": products.get("primary")},
    )
    return result


def _handle_score_deal(payload: Any) -> dict[str, Any]:
    """Score deal probability and classify tier from intel + optional engagement."""
    check_capability("prospecting", "score_deal")
    if not isinstance(payload, dict):
        payload = {}
    intel = payload.get("intel", payload)
    engagement = payload.get("engagement") or None
    _LOG.info("score_deal action started")
    result = deal_scoring.score_deal(intel, engagement)
    _LOG.info(
        "score_deal complete",
        extra={"tier": result.get("tier"), "probability": result.get("probability")},
    )
    return result


def _handle_generate_dynamic_script(payload: Any) -> dict[str, Any]:
    """Generate an adaptive call script from intel signals."""
    check_capability("prospecting", "generate_dynamic_script")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="expected_payload_object")
    company_name = payload.get("company_name")
    if not company_name or not isinstance(company_name, str):
        raise HTTPException(status_code=400, detail="missing_required_field: company_name")
    intel = payload.get("intel")
    if not isinstance(intel, dict):
        raise HTTPException(status_code=400, detail="missing_required_field: intel")
    _LOG.info("generate_dynamic_script action started company=%s", company_name)
    result = dynamic_script.generate_script(company_name, intel)
    _LOG.info(
        "generate_dynamic_script complete",
        extra={
            "company_name": company_name,
            "pitch_angle": result.get("pitch_angle"),
            "primary_product": result.get("primary_product"),
        },
    )
    return result


def _handle_generate_dynamic_script_with_pivot(payload: Any) -> dict[str, Any]:
    """Generate an adaptive call script + secondary-product pivot from intel signals."""
    check_capability("prospecting", "generate_dynamic_script_with_pivot")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="expected_payload_object")
    company_name = payload.get("company_name")
    if not company_name or not isinstance(company_name, str):
        raise HTTPException(status_code=400, detail="missing_required_field: company_name")
    intel = payload.get("intel")
    if not isinstance(intel, dict):
        raise HTTPException(status_code=400, detail="missing_required_field: intel")
    _LOG.info(
        "generate_dynamic_script_with_pivot action started company=%s", company_name
    )
    result = dynamic_script.generate_script_with_pivot(company_name, intel)
    _LOG.info(
        "generate_dynamic_script_with_pivot complete",
        extra={
            "company_name": company_name,
            "pitch_angle": result.get("pitch_angle"),
            "primary_product": result.get("primary_product"),
            "has_pivot": result.get("pivot") is not None,
        },
    )
    return result


@asynccontextmanager
async def _prospecting_lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Prospecting app lifespan: start/stop the in-stack daily cadence loop.

    ADR-014 (ACCEPTED 2026-06-18, forward-ported to samus 2026-06-23): the
    in-container daily discovery+persist cadence (cadence_task.py), mirroring
    the gateway control-tick loop. DORMANT BY DEFAULT — start_cadence_loop
    no-ops and returns None unless the operator arms
    SAMUS_PROSPECTING_IN_STACK_CADENCE_ENABLED, so on merge this lifespan adds
    nothing at runtime. Fail-soft: a start failure is logged and never blocks
    the workcell from serving its HTTP surface.
    """
    try:
        # Boot wiring: load the runtime flag store so the cadence's
        # arming-flag override is honoured. The prospecting app had no flag
        # consumer before the cadence, so this init was missing — without it
        # is_enabled falls back to the settings default and the loop stays
        # dormant even when armed via set_flag. Idempotent; fail-soft.
        # Skipped under pytest (matches create_base_app): loading the REAL
        # persisted flips into a unit-test process leaks operator arming
        # state into tests that assert dormant defaults.
        import os as _os

        if not _os.getenv("PYTEST_CURRENT_TEST"):
            from backend.common.flags.catalog import register_all
            from backend.common.flags.store import init_default_store

            init_default_store()
            register_all()

        from .cadence_task import start_cadence_loop

        await start_cadence_loop(app)
    except Exception:  # noqa: BLE001 — cadence must never block the workcell boot
        _LOG.exception("prospecting cadence loop failed to start; continuing")
    try:
        yield
    finally:
        try:
            from .cadence_task import stop_cadence_loop

            await stop_cadence_loop(app)
        except Exception:  # noqa: BLE001 — best-effort teardown
            _LOG.exception("prospecting cadence loop stop raised")


def create_app():
    app = create_base_app(
        service_name="prospecting", lifespan=_prospecting_lifespan,
    )

    @app.post("/work")
    async def work(request: Request) -> dict[str, Any]:
        body = await request.json()
        envelope = _parse_envelope(body)

        # Action resolved from metadata (gateway convention); default "discover".
        action = str(envelope.metadata.get("action") or "discover").lower()

        if action in ("discover", "build_call_sheet"):
            check_capability("prospecting", "discover")
            req = _parse_discovery_req(envelope.payload)
            result = process_discovery(req, task_id=envelope.task_id)
            return result.model_dump()

        if action == "daily_supply":
            return _handle_daily_supply()

        if action == "analyze_business":
            return _handle_analyze_business(envelope.payload)

        if action == "score_deal":
            return _handle_score_deal(envelope.payload)

        if action == "generate_dynamic_script":
            return _handle_generate_dynamic_script(envelope.payload)

        if action == "generate_dynamic_script_with_pivot":
            return _handle_generate_dynamic_script_with_pivot(envelope.payload)

        raise HTTPException(
            status_code=400,
            detail=f"unknown_action: {action}",
        )

    @app.post("/discover")
    async def discover(request: Request) -> dict[str, Any]:
        check_capability("prospecting", "discover")
        body = await request.json()
        req = _parse_discovery_req(body)
        result = process_discovery(req)
        return result.model_dump()

    return app


app = create_app()
