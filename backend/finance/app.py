"""Finance workcell FastAPI service."""
from __future__ import annotations

import logging
import os
import threading
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, HTTPException, Request

from backend.common.app_factory import create_base_app
from backend.common.capabilities import check_capability
from backend.common.models import TaskEnvelope
from backend.common.rate_limit import rate_limit_dependency

from .metering import report_call_minutes
from .models import (
    ActionsSummaryRequest,
    DebtsRequest,
    DeclinesRequest,
    HardshipRequest,
    InfoGapsRequest,
    LiabilitiesRequest,
    MeterEventRequest,
    RunwayRequest,
    SnapshotRequest,
)
from .service import (
    confirm_customer_exists,
    get_actions_summary,
    get_codb_summary,
    get_customer_billing_summary,
    get_debt_portfolio,
    get_declines_summary,
    get_hardship_context,
    get_info_gaps_summary,
    get_liabilities_summary,
    get_payment_links,
    get_recent_payments,
    get_runway,
    get_snapshot,
    handle_stripe_webhook,
)
from .webhook import WebhookSignatureError


_LOG = logging.getLogger("samus.finance.app")

# Default cadence for the upsell-runner daemon thread. Overridable via env
# for tests / one-off operator runs; minimum 60s to keep accidental misconfig
# from hammering SendGrid in a busy loop.
_UPSELL_LOOP_INTERVAL_S_DEFAULT = 3600
_UPSELL_LOOP_INTERVAL_MIN_S = 60


def _upsell_loop_interval_s() -> int:
    raw = os.getenv("SAMUS_UPSELL_LOOP_INTERVAL_S", "").strip()
    if not raw:
        return _UPSELL_LOOP_INTERVAL_S_DEFAULT
    try:
        val = int(raw)
    except ValueError:
        return _UPSELL_LOOP_INTERVAL_S_DEFAULT
    return max(_UPSELL_LOOP_INTERVAL_MIN_S, val)


def _upsell_loop_enabled() -> bool:
    """Default ON. Set ``SAMUS_UPSELL_LOOP=0`` to skip (tests + dev shells)."""
    raw = os.getenv("SAMUS_UPSELL_LOOP", "1").strip().lower()
    return raw not in ("0", "false", "no", "off", "")


def _run_upsell_loop() -> None:  # pragma: no cover — daemon thread loop
    """Drain the upsell queue then sleep. Survives transient failures."""
    interval = _upsell_loop_interval_s()
    _LOG.info("upsell loop started; interval=%ds", interval)
    # Lazy import keeps the module-load dependency graph small for the
    # workcell when the loop is disabled (tests, CI).
    from .upsell_runner import process_upsell_queue
    while True:
        try:
            result = process_upsell_queue()
            if result.due > 0:
                _LOG.info(
                    "upsell pass: due=%d sent=%d failed=%d",
                    result.due, result.sent, result.failed,
                )
        except Exception as exc:  # noqa: BLE001 — never let the thread die
            _LOG.warning("upsell loop iter failed: %s", exc)
        time.sleep(interval)


def _parse(model_cls, payload: Any):
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail=f"expected_{model_cls.__name__}_object")
    try:
        return model_cls.model_validate(payload)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"invalid_{model_cls.__name__}: {exc}")


def create_app():
    @asynccontextmanager
    async def _lifespan(app):  # pragma: no cover — exercised via TestClient
        # Startup: spawn the upsell drain loop unless explicitly disabled.
        # Daemon=True so it dies with the container; no shutdown hook needed.
        if _upsell_loop_enabled():
            thread = threading.Thread(
                target=_run_upsell_loop,
                name="upsell_loop",
                daemon=True,
            )
            thread.start()
        else:
            _LOG.info("upsell loop disabled (SAMUS_UPSELL_LOOP=0)")
        yield

    # ``/stripe_webhook`` is carved out of VerifyHMACMiddleware: Stripe
    # signs its POSTs with ``Stripe-Signature`` (verified inside
    # handle_stripe_webhook -> WebhookSignatureError). Every other finance
    # route (/work, /snapshot, /runway, ...) still requires HMAC.
    app = create_base_app(
        service_name="finance",
        lifespan=_lifespan,
        hmac_exempt_paths=("/stripe_webhook",),
    )

    @app.post("/work")
    async def work(request: Request) -> dict[str, Any]:
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="expected_json_object")
        try:
            envelope = TaskEnvelope.model_validate(body)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"invalid_envelope: {exc}")
        action = (envelope.metadata or {}).get("action") or "snapshot"
        payload = envelope.payload if isinstance(envelope.payload, dict) else {}
        if action == "snapshot":
            check_capability("finance", "snapshot")
            return get_snapshot(SnapshotRequest.model_validate(payload)).model_dump()
        if action == "codb_summary":
            check_capability("finance", "codb_summary")
            return get_codb_summary().model_dump()
        if action == "runway":
            check_capability("finance", "runway")
            req = RunwayRequest.model_validate(payload)
            return get_runway(req.override_balance_usd).model_dump()
        if action == "liabilities":
            check_capability("finance", "liabilities")
            LiabilitiesRequest.model_validate(payload)
            return get_liabilities_summary().model_dump()
        if action == "declines":
            check_capability("finance", "declines")
            req = DeclinesRequest.model_validate(payload)
            return get_declines_summary(req.window_days).model_dump()
        if action == "debts":
            check_capability("finance", "debts")
            DebtsRequest.model_validate(payload)
            return get_debt_portfolio().model_dump()
        if action == "actions":
            check_capability("finance", "actions")
            ActionsSummaryRequest.model_validate(payload)
            return get_actions_summary().model_dump()
        if action == "info_gaps":
            check_capability("finance", "info_gaps")
            InfoGapsRequest.model_validate(payload)
            return get_info_gaps_summary().model_dump()
        if action == "hardship":
            check_capability("finance", "hardship")
            HardshipRequest.model_validate(payload)
            return get_hardship_context().model_dump()
        if action == "payment_links":
            check_capability("finance", "payment_links")
            active = bool((payload or {}).get("active", True))
            return get_payment_links(active=active).model_dump()
        if action == "customer_summary":
            check_capability("finance", "customer_summary")
            email = str((payload or {}).get("email") or "").strip()
            if not email:
                raise HTTPException(status_code=422, detail="email_required")
            charges_limit = int((payload or {}).get("charges_limit") or 5)
            return get_customer_billing_summary(
                email, charges_limit=charges_limit,
            ).model_dump()
        if action == "report_meter_event":
            check_capability("finance", "report_meter_event")
            req = _parse(MeterEventRequest, payload)
            return report_call_minutes(
                stripe_customer_id=req.stripe_customer_id,
                call_id=req.call_id,
                duration_seconds=req.duration_seconds,
                sku_id=req.sku_id,
            ).model_dump()
        if action == "customer_exists":
            check_capability("finance", "customer_exists")
            cid = str((payload or {}).get("stripe_customer_id") or "").strip()
            return {"exists": confirm_customer_exists(cid)}
        raise HTTPException(status_code=400, detail=f"unknown_action: {action}")

    @app.post("/snapshot")
    async def snapshot(request: Request) -> dict[str, Any]:
        check_capability("finance", "snapshot")
        body = await request.json()
        return get_snapshot(_parse(SnapshotRequest, body)).model_dump()

    @app.get("/snapshot")
    async def snapshot_get() -> dict[str, Any]:
        """Convenience GET — default limits, no body required."""
        check_capability("finance", "snapshot")
        return get_snapshot(SnapshotRequest()).model_dump()

    @app.get("/codb_summary")
    async def codb_summary() -> dict[str, Any]:
        check_capability("finance", "codb_summary")
        return get_codb_summary().model_dump()

    @app.post("/runway")
    async def runway(request: Request) -> dict[str, Any]:
        check_capability("finance", "runway")
        body = await request.json()
        req = _parse(RunwayRequest, body)
        return get_runway(req.override_balance_usd).model_dump()

    @app.get("/runway")
    async def runway_get() -> dict[str, Any]:
        check_capability("finance", "runway")
        return get_runway().model_dump()

    @app.get("/liabilities")
    async def liabilities_get() -> dict[str, Any]:
        check_capability("finance", "liabilities")
        return get_liabilities_summary().model_dump()

    @app.get("/declines")
    async def declines_get(window_days: int = 30) -> dict[str, Any]:
        check_capability("finance", "declines")
        return get_declines_summary(window_days).model_dump()

    @app.get("/debts")
    async def debts_get() -> dict[str, Any]:
        check_capability("finance", "debts")
        return get_debt_portfolio().model_dump()

    @app.get("/actions")
    async def actions_get() -> dict[str, Any]:
        check_capability("finance", "actions")
        return get_actions_summary().model_dump()

    @app.get("/info_gaps")
    async def info_gaps_get() -> dict[str, Any]:
        check_capability("finance", "info_gaps")
        return get_info_gaps_summary().model_dump()

    @app.get("/hardship")
    async def hardship_get() -> dict[str, Any]:
        check_capability("finance", "hardship")
        return get_hardship_context().model_dump()

    @app.get("/payment_links")
    async def payment_links_get(active: bool = True) -> dict[str, Any]:
        check_capability("finance", "payment_links")
        return get_payment_links(active=active).model_dump()

    @app.post("/stripe_webhook")
    async def stripe_webhook_post(request: Request) -> dict[str, Any]:
        """Stripe POSTs here. NO HMAC middleware applies — Stripe owns the
        auth check via its own signature scheme verified in handle_stripe_webhook.
        """
        check_capability("finance", "stripe_webhook")
        payload_bytes = await request.body()
        sig = request.headers.get("stripe-signature", "")
        try:
            result = handle_stripe_webhook(payload_bytes, sig)
        except WebhookSignatureError as exc:
            raise HTTPException(status_code=400, detail=f"signature_error: {exc}")
        return result.model_dump()

    @app.get("/recent_payments")
    async def recent_payments_get(window_days: int = 7) -> dict[str, Any]:
        check_capability("finance", "recent_payments")
        return get_recent_payments(window_days).model_dump()

    # M7 — /meter-event writes a Stripe metered-billing usage record. A
    # replay loop here directly inflates a customer's invoice, so it gets a
    # per-caller rate limit on top of the route's own idempotency on call_id.
    # Env-tunable via SAMUS_RATE_LIMIT_FINANCE_METER_EVENT_PER_MINUTE.
    @app.post(
        "/meter-event",
        dependencies=[Depends(rate_limit_dependency("finance_meter_event"))],
    )
    async def meter_event_post(request: Request) -> dict[str, Any]:
        """Report one inbound receptionist call's usage to a Stripe Meter.

        Posted by the voice workcell after an inbound call ends (signed-HMAC,
        like every finance route except /stripe_webhook). Keeping the Stripe
        write in the finance workcell means the Stripe secret key stays in
        one container. ``report_call_minutes`` never raises — a metering
        failure returns ``ok=false`` with an ``error`` for the operator.
        """
        check_capability("finance", "report_meter_event")
        body = await request.json()
        req = _parse(MeterEventRequest, body)
        return report_call_minutes(
            stripe_customer_id=req.stripe_customer_id,
            call_id=req.call_id,
            duration_seconds=req.duration_seconds,
            sku_id=req.sku_id,
        ).model_dump()

    @app.post("/customer-exists")
    async def customer_exists_post(request: Request) -> dict[str, Any]:
        """Confirm a Stripe customer id maps to a live customer (FIN-08).

        Posted by the voice workcell (signed-HMAC, like every finance route
        except /stripe_webhook) before it meters a receptionist's calls. The
        Stripe secret key lives only in this container, so the existence check
        is delegated here. Fail-closed: ``exists=true`` ONLY when Stripe
        affirmatively confirms a live customer with this id; a malformed id, a
        404, an unset key, or any Stripe error all return ``exists=false`` —
        the voice side disables metering on false, which is the safe
        direction (never bill an unverified id). ``confirm_customer_exists``
        never raises.
        """
        check_capability("finance", "customer_exists")
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="expected_json_object")
        cid = str(body.get("stripe_customer_id") or "").strip()
        return {"exists": confirm_customer_exists(cid)}

    @app.get("/service_costs")
    async def service_costs_get(force: bool = False) -> dict[str, Any]:
        """Live vendor balance/spend snapshot (OpenAI, Anthropic, Twilio, Vapi).

        Returns per-vendor balance, period spend, and alerts when any
        service drops below its configured threshold.
        """
        check_capability("finance", "codb_summary")
        from dataclasses import asdict
        from .service_cost_monitor import service_cost_snapshot
        return asdict(service_cost_snapshot(force=force))

    @app.get("/customer_summary")
    async def customer_summary_get(
        email: str, charges_limit: int = 5,
    ) -> dict[str, Any]:
        """Per-customer billing posture (paid? subscribed? what amount?).

        Consumed by the intake Gmail poller to enrich inbound-email CRM
        artifacts so the operator sees billing context next to the reply.
        Read-only — uses the same restricted Stripe key as the rest of
        the finance workcell. Always returns a typed summary (state
        reflects unknown / lookup_failed cases).
        """
        check_capability("finance", "customer_summary")
        if not (email or "").strip():
            raise HTTPException(status_code=422, detail="email_required")
        return get_customer_billing_summary(
            email, charges_limit=charges_limit,
        ).model_dump()

    return app


app = create_app()
