"""Intake workcell FastAPI service.

Endpoints:

  - ``POST /intake/onboarding``  — public, CORS-allowed from
    settings.intake_allowed_origins. Body matches the HTML form on
    hustleforge.tech/onboarding/. Returns ``OnboardingLeadResult``.
  - ``GET  /intake/form-schema`` — public, CORS-allowed. Canonical
    schema (labels, placeholders, accepted enum values) that the static
    site renders. Single source of truth for the form definition; pairs
    with the receiver above so a field rename / option change lands once
    in the backend and the site picks it up on next deploy.
  - ``GET  /intake/leads``        — operator-only paginated list.
  - ``POST /work``                — TaskEnvelope parity for gateway / future
    SQS worker. Same submit_lead path under the hood.

CORS is wired explicitly via Starlette's CORSMiddleware so the cross-origin
fetch from ``https://hustleforge.tech`` -> ``https://api.hustleforge.tech``
gets the preflight OPTIONS handshake it needs. Allowed origins come from
settings (overridable per environment).
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import FastAPI, HTTPException, Request
from starlette.middleware.cors import CORSMiddleware

from backend.common.app_factory import create_base_app
from backend.common.capabilities import check_capability
from backend.common.config import get_settings
from backend.common.models import TaskEnvelope

from .captcha import captcha_required, verify_captcha
from .form_schema import FormSchemaResponse, get_form_schema
from .models import (
    LeadListResult,
    OnboardingLeadRequest,
    OnboardingLeadResult,
    TelemetryEventRequest,
    TelemetryEventResult,
)
from .gmail_poller import drain_once
from .rate_limit import check_rate_limit
from .service import list_recent_leads, submit_lead
from .telemetry import record_event as record_telemetry_event


_LOG = logging.getLogger("samus.intake.app")


def _client_ip(request: Request) -> str:
    """Resolve the originating client IP, trusting only proxy-appended XFF.

    The leftmost ``X-Forwarded-For`` entries are fully attacker-controlled — a
    client can send ``X-Forwarded-For: 1.2.3.4`` and the proxy chain simply
    appends its own hop after it. Trusting the *first* entry therefore poisons
    audit logs and lets an attacker forge an arbitrary key for the rate
    limiter.

    The reverse-proxy chain in front of this workcell appends a fixed number
    of trusted hops (Cloud Run / a single Caddy each append exactly one). The
    real client IP is the entry that many positions from the RIGHT of the XFF
    list. We take ``xff[-hops]`` — the address handed to the outermost
    trusted proxy. When XFF has fewer entries than the configured hop count
    (direct hit, or a misconfigured proxy), we fall back to the direct socket
    peer ``request.client.host``, which cannot be spoofed.
    """
    direct = (request.client.host if request.client else "") or ""
    xff = request.headers.get("x-forwarded-for")
    if not xff:
        return direct
    entries = [part.strip() for part in xff.split(",") if part.strip()]
    if not entries:
        return direct
    hops = max(1, int(get_settings().intake_trusted_proxy_hops))
    # The trusted proxies are the last ``hops`` entries; the address they
    # received is at index -hops. With exactly ``hops`` entries the client
    # connected straight to the outermost proxy (entries[0]); with fewer than
    # ``hops`` entries the XFF is shorter than the trusted chain so we cannot
    # trust any entry — fall back to the unspoofable socket peer.
    if len(entries) < hops:
        return direct
    return entries[-hops]


def _parse(model_cls, payload: Any):
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail=f"expected_{model_cls.__name__}_object")
    try:
        return model_cls.model_validate(payload)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"invalid_{model_cls.__name__}: {exc}")


@asynccontextmanager
async def _intake_lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Intake-only lifespan: spawn/stop the in-container Gmail poll loop.

    Internalizes the ``Samus Inbox Poll`` Windows scheduled task — the
    always-on intake workcell is where the Gmail credentials + CRM writes
    already live, so the drain lives here too. Same shape as the
    gateway's control-tick + morning-ritual lifespan hooks. Fail-soft on
    both start and stop; a poll loop hiccup must never block the intake
    HTTP surface (public onboarding form + telemetry beacon).
    """
    try:
        from backend.intake.gmail_poll_task import start_gmail_poll_loop

        await start_gmail_poll_loop(app)
    except Exception:  # noqa: BLE001 — poll loop must never block intake boot
        _LOG.exception("gmail_poll loop failed to start; continuing without it")
    try:
        from backend.intake.calendar_poll_task import start_calendar_poll_loop

        await start_calendar_poll_loop(app)
    except Exception:  # noqa: BLE001 — calendar sync must never block boot
        _LOG.exception("calendar_poll loop failed to start; continuing without it")
    try:
        yield
    finally:
        try:
            from backend.intake.gmail_poll_task import stop_gmail_poll_loop

            await stop_gmail_poll_loop(app)
        except Exception:  # noqa: BLE001 — best-effort teardown
            _LOG.exception("gmail_poll loop stop raised")
        try:
            from backend.intake.calendar_poll_task import stop_calendar_poll_loop

            await stop_calendar_poll_loop(app)
        except Exception:  # noqa: BLE001 — best-effort teardown
            _LOG.exception("calendar_poll loop stop raised")


def create_app():
    # ``/intake/onboarding`` and ``/intake/form-schema`` are carved out of
    # VerifyHMACMiddleware: both are PUBLIC endpoints called from the
    # static marketing site (CORS-controlled, no signing scheme — the
    # browser is the caller, there's nothing to sign with). Internal
    # routes (/intake/leads, /work, /intake/poll-inbox) still require
    # HMAC. The CORS middleware below + capability_check + dedup window
    # remain the boundary contract for the public paths.
    app = create_base_app(
        service_name="intake",
        lifespan=_intake_lifespan,
        hmac_exempt_paths=(
            "/intake/onboarding",
            "/intake/form-schema",
            "/intake/telemetry",
        ),
    )

    # CORS middleware MUST be added before the routes so OPTIONS preflight
    # gets the right headers without hitting capability checks.
    settings = get_settings()
    allowed = list(settings.intake_allowed_origins or [])
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed,
        allow_origin_regex=None,
        allow_credentials=False,  # public form, no cookies/credentials
        # GET added so the static site can fetch /intake/form-schema from
        # the browser at runtime; POST stays for the receiver; OPTIONS for
        # the preflight on either.
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type"],
        max_age=600,  # 10min preflight cache; cuts OPTIONS chatter
    )

    @app.post("/intake/onboarding", response_model=OnboardingLeadResult)
    async def onboarding_post(request: Request) -> OnboardingLeadResult:
        check_capability("intake", "submit_lead")

        # Resolve the (corrected, non-spoofable) client IP once — used for
        # the rate-limit key, the CAPTCHA cross-check, and the stored lead.
        source_ip = _client_ip(request)

        # Finding 1 — per-source-IP rate limiting. CORS does not protect this
        # endpoint (it is browser-only; curl / scripts ignore it), so a flood
        # would otherwise burn DynamoDB writes + SendGrid quota + the
        # operator inbox. The limiter is DynamoDB-backed so it holds across
        # Cloud Run instances, and fails OPEN — a backend hiccup logs but
        # never blocks a legitimate lead.
        decision = check_rate_limit(source_ip)
        if not decision.allowed:
            raise HTTPException(
                status_code=429,
                detail={
                    "error": "rate_limited",
                    "scope": decision.scope,
                    "limit": decision.limit,
                    "retry_after_seconds": decision.retry_after_seconds,
                    "message": (
                        "Too many onboarding submissions. Please wait a "
                        "moment and try again."
                    ),
                },
                headers={"Retry-After": str(decision.retry_after_seconds)},
            )
        if decision.backend_error:
            _LOG.warning(
                "intake rate limiter degraded (failed open): %s",
                decision.backend_error,
            )

        try:
            body = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"invalid_json: {exc}") from exc

        # Finding 1 — optional Cloudflare Turnstile CAPTCHA. The OnboardingLead
        # request model is ``extra="forbid"``, so the CAPTCHA token (which is
        # NOT part of the persisted lead schema) is popped from the raw body
        # BEFORE validation — this keeps the public lead schema unchanged.
        # CAPTCHA is skipped entirely when no secret is configured (default);
        # when configured, verification fails CLOSED.
        captcha_token = ""
        if isinstance(body, dict):
            captcha_token = str(body.pop("captcha_token", "") or "")
        if captcha_required():
            captcha = verify_captcha(captcha_token, source_ip=source_ip)
            if not captcha.ok:
                raise HTTPException(
                    status_code=400,
                    detail={
                        "error": "captcha_failed",
                        "reason": captcha.detail,
                        "message": "CAPTCHA verification failed.",
                    },
                )

        req = _parse(OnboardingLeadRequest, body)
        return submit_lead(
            req,
            source_ip=source_ip,
            user_agent=(request.headers.get("user-agent") or "")[:512],
        )

    @app.post("/intake/telemetry", response_model=TelemetryEventResult)
    async def telemetry_post(request: Request) -> TelemetryEventResult:
        """Public, CORS-allowed site-telemetry beacon.

        Gated at the service layer on ``intake_telemetry_ingest_enabled``. When
        the flag is OFF the endpoint still returns 200 with
        ``status='dropped_disabled'`` so the site beacon never looks broken —
        arming just switches persistence on. The per-IP rate limiter is shared
        with the onboarding endpoint (same source_ip key) so a rogue beacon
        loop can't burn the operator's write budget.
        """
        source_ip = _client_ip(request)
        decision = check_rate_limit(source_ip)
        if not decision.allowed:
            raise HTTPException(
                status_code=429,
                detail={
                    "error": "rate_limited",
                    "scope": decision.scope,
                    "retry_after_seconds": decision.retry_after_seconds,
                },
                headers={"Retry-After": str(decision.retry_after_seconds)},
            )
        try:
            body = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"invalid_json: {exc}") from exc
        req = _parse(TelemetryEventRequest, body)
        return record_telemetry_event(
            req,
            source_ip=source_ip,
            user_agent=(request.headers.get("user-agent") or "")[:512],
        )

    @app.get("/intake/form-schema", response_model=FormSchemaResponse)
    async def form_schema_get() -> FormSchemaResponse:
        """Return the canonical onboarding-form schema.

        Public, no capability check — the static site fetches this at
        deploy / page-load time to render the form against a single
        source of truth. Field names + enum values match the receiver
        at ``/intake/onboarding`` exactly so a 200 here implies a 200
        on the matching POST (modulo per-field validation).
        """
        return get_form_schema()

    @app.get("/intake/leads", response_model=LeadListResult)
    async def leads_list(limit: int = 50) -> LeadListResult:
        check_capability("intake", "list_leads")
        return list_recent_leads(limit=limit)

    @app.post("/work")
    async def work(request: Request) -> dict[str, Any]:
        """TaskEnvelope route for gateway / future SQS worker."""
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="expected_json_object")
        try:
            envelope = TaskEnvelope.model_validate(body)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"invalid_envelope: {exc}")
        action = (envelope.metadata or {}).get("action") or "submit_lead"
        payload = envelope.payload if isinstance(envelope.payload, dict) else {}
        if action == "submit_lead":
            check_capability("intake", "submit_lead")
            req = OnboardingLeadRequest.model_validate(payload)
            return submit_lead(
                req,
                source_ip=_client_ip(request),
                user_agent=(request.headers.get("user-agent") or "")[:512],
            ).model_dump()
        if action == "list_leads":
            check_capability("intake", "list_leads")
            limit = int(payload.get("limit") or 50)
            return list_recent_leads(limit=limit).model_dump()
        if action == "poll_inbox":
            check_capability("intake", "poll_inbox")
            # Drain one batch of unseen Gmail messages -> CRM artifacts + tasks.
            # Synchronous on purpose: the dispatcher (Task Scheduler, gateway,
            # or manual /work POST) gets the per-pass counts in the response.
            result = drain_once()
            return {
                "enabled": result.enabled,
                "fetched": result.fetched,
                "processed": result.processed,
                "duplicates": result.duplicates,
                "failed": result.failed,
                "connect_error": result.connect_error,
                "handled_count": len(result.handled),
            }
        raise HTTPException(status_code=400, detail=f"unknown_action: {action}")

    @app.post("/intake/poll-inbox")
    async def poll_inbox_post() -> dict[str, Any]:
        """Manual trigger for one Gmail-inbox drain pass.

        Same drain logic as ``/work`` + ``action=poll_inbox`` and as the
        ``Poll-Inbox.ps1`` Task Scheduler entry. Returns the per-pass
        counters so an operator hitting curl can confirm the result.
        Capability-gated; safe to call on any cadence (idempotent on the
        message ledger).
        """
        check_capability("intake", "poll_inbox")
        result = drain_once()
        return {
            "enabled": result.enabled,
            "fetched": result.fetched,
            "processed": result.processed,
            "duplicates": result.duplicates,
            "failed": result.failed,
            "connect_error": result.connect_error,
            "handled_count": len(result.handled),
        }

    return app


app = create_app()