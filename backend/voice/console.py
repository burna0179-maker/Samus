"""Operator voice console — a browser UI for placing single Vapi calls.

The voice workcell is normally driven by the operator PowerShell scripts
(``Start-MorningDial.ps1`` autodialer, ``Log-Call.ps1``) and the inter-workcell
HMAC mesh. This module adds a thin browser console for the one-off case: an
operator who wants to place a single test / sales call and watch its status
from a phone or laptop, without a terminal.

Why it does NOT reuse the HMAC-signed ``/voice/*`` routes
---------------------------------------------------------
Those routes require an HMAC-SHA256 signature; a browser cannot hold the
shared HMAC key without exposing it client-side — which would defeat the
boundary contract. So the console exposes its own small surface
(``/console`` + ``/console/api/*``) that is carved OUT of
``VerifyHMACMiddleware`` (via ``hmac_exempt_paths`` in :mod:`backend.voice.app`)
and is instead gated by a single operator bearer token,
``SAMUS_VOICE_CONSOLE_TOKEN``, sent in the ``X-Console-Token`` header.

The console routes call the in-process service functions directly
(:func:`initiate_call`, :func:`get_call_status`, ...), so no HTTP signature is
involved at all and the Vapi API key never leaves the server — it lives only
in ``get_settings()`` and is used by ``VapiClient`` inside the service layer.

Fail-closed: with ``SAMUS_VOICE_CONSOLE_TOKEN`` unset, every ``/console/api``
route returns 503. No token, no console.

All console paths are static (no path params) so they can be matched by the
exact-match ``hmac_exempt_paths`` set in ``app_factory``. The one read route
that needs a call id takes it as a query param for that reason.
"""

from __future__ import annotations

import hmac
import logging
import os
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from backend.common.capabilities import check_capability
from backend.common.config import get_settings
from backend.common.rate_limit import rate_limit_dependency

from .models import InitiateCallRequest
from .service import (
    get_call_status,
    get_voice_call_summary,
    initiate_call,
    list_recent_calls,
)


_LOG = logging.getLogger("samus.voice.console")

_STATIC_DIR = Path(__file__).resolve().parent / "static"
_CONSOLE_HTML = _STATIC_DIR / "console.html"

# Every console path is STATIC (no path params) so app_factory's exact-match
# ``hmac_exempt_paths`` set can carve them out of VerifyHMACMiddleware. app.py
# splices this tuple into the create_base_app() call.
CONSOLE_PATHS: tuple[str, ...] = (
    "/console",
    "/console/api/config-check",
    "/console/api/call",
    "/console/api/calls",
    "/console/api/call-status",
    "/console/api/summary",
)


class ConsoleCallRequest(BaseModel):
    """``POST /console/api/call`` body — the operator only supplies the callee.

    The ``assistant_id`` / ``phone_number_id`` come from server-side settings
    (``VAPI_ASSISTANT_ID`` / ``VAPI_PHONE_NUMBER_ID``); the browser never sees
    or chooses them.
    """

    model_config = ConfigDict(extra="forbid")

    customer_number: str = Field(min_length=1)
    customer_name: str | None = None


def _console_token() -> str:
    """The operator bearer token. Empty -> the console API is fail-closed."""
    return (os.getenv("SAMUS_VOICE_CONSOLE_TOKEN", "") or "").strip()


def require_console_token(
    x_console_token: str | None = Header(default=None),
) -> None:
    """FastAPI dependency — gate a ``/console/api`` route on the operator token.

    503 when the token is unset (the console is intentionally off until an
    operator opts in by setting one); 401 on a missing / wrong token. The
    compare is constant-time so a wrong token leaks no timing signal.
    """
    expected = _console_token()
    if not expected:
        raise HTTPException(status_code=503, detail="console_token_unset")
    provided = (x_console_token or "").strip()
    if not provided or not hmac.compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail="console_unauthorized")


def _normalize_console_phone(raw: str) -> str | None:
    """Validate + normalize an operator-typed number to E.164-ish.

    Lazy-imports the dialer's canonical normalizer so console.py does not drag
    the dialer's ``backend.prospecting`` dependency into the app import graph
    (the same reasoning app.py uses for its lazy ``dial_call_list`` import).
    """
    from .dialer import _normalize_phone

    return _normalize_phone(raw)


def register_console_routes(app: FastAPI) -> None:
    """Attach the operator console page + its JSON API to ``app``.

    Called by :func:`backend.voice.app.create_app` after the HMAC-signed
    routes. The paths registered here MUST stay in sync with
    :data:`CONSOLE_PATHS` (which app.py feeds to ``hmac_exempt_paths``).
    """

    @app.get("/console")
    async def console_page() -> Any:
        """Serve the single-page console shell. Public (no token): the page
        itself has no data — its JS prompts for the operator token and every
        data route below is token-gated."""
        if not _CONSOLE_HTML.exists():
            raise HTTPException(status_code=500, detail="console_html_missing")
        return FileResponse(_CONSOLE_HTML, media_type="text/html")

    @app.get(
        "/console/api/config-check",
        dependencies=[Depends(require_console_token)],
    )
    async def console_config_check() -> dict[str, Any]:
        """Report whether the Vapi settings the console needs are present.

        Returns booleans only — never the secret values — so the UI can warn
        the operator before a call fails.
        """
        s = get_settings()
        return {
            "vapi_api_key_set": bool((s.vapi_api_key or "").strip()),
            "vapi_assistant_id_set": bool((s.vapi_assistant_id or "").strip()),
            "vapi_phone_number_id_set": bool((s.vapi_phone_number_id or "").strip()),
        }

    @app.post(
        "/console/api/call",
        dependencies=[
            Depends(require_console_token),
            # Reuse the same scope as the HMAC-signed /voice/call route so a
            # runaway console + a runaway mesh caller share one ceiling.
            Depends(rate_limit_dependency("voice_call")),
        ],
    )
    async def console_call(request: Request) -> dict[str, Any]:
        """Place one outbound call. Body: ``{customer_number, customer_name?}``."""
        check_capability("voice", "initiate_call")
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="expected_json_object")
        try:
            req = ConsoleCallRequest.model_validate(body)
        except Exception as exc:  # noqa: BLE001 — surface a 422, not a 500
            raise HTTPException(status_code=422, detail=f"invalid_request: {exc}")

        # Server-side phone validation (requirement: never trust the browser).
        normalized = _normalize_console_phone(req.customer_number)
        if normalized is None:
            raise HTTPException(
                status_code=422,
                detail=f"invalid_phone_number: {req.customer_number!r} "
                "(need at least 10 digits; US 10-digit or E.164)",
            )

        s = get_settings()
        assistant_id = (s.vapi_assistant_id or "").strip()
        phone_number_id = (s.vapi_phone_number_id or "").strip()
        if not assistant_id or not phone_number_id:
            raise HTTPException(
                status_code=503,
                detail="vapi_assistant_or_phone_unset: set VAPI_ASSISTANT_ID "
                "and VAPI_PHONE_NUMBER_ID",
            )

        # Log the action — phone tail only (PII minimization, mirrors the
        # ``customer_number_tail`` the service-layer audit ledger records).
        _LOG.info(
            "console call initiated: customer_tail=%s name=%r",
            normalized[-4:],
            req.customer_name or "",
        )
        result = initiate_call(
            InitiateCallRequest(
                assistant_id=assistant_id,
                phone_number_id=phone_number_id,
                customer_number=normalized,
                customer_name=req.customer_name or None,
                metadata={"source": "operator_console"},
            )
        )
        return result.model_dump()

    @app.get(
        "/console/api/calls",
        dependencies=[Depends(require_console_token)],
    )
    async def console_calls(limit: int = 10) -> dict[str, Any]:
        """List recent Vapi calls for the console's history table."""
        check_capability("voice", "list_calls")
        return list_recent_calls(limit=limit).model_dump()

    @app.get(
        "/console/api/call-status",
        dependencies=[Depends(require_console_token)],
    )
    async def console_call_status(call_id: str = "") -> dict[str, Any]:
        """Fetch one call's live status — the JS polls this after a dial."""
        check_capability("voice", "fetch_call")
        if not call_id.strip():
            raise HTTPException(status_code=422, detail="call_id query param required")
        return get_call_status(call_id.strip()).model_dump()

    @app.get(
        "/console/api/summary",
        dependencies=[Depends(require_console_token)],
    )
    async def console_summary(window_hours: int = 24) -> dict[str, Any]:
        """Rolling call-activity summary for the console's stats block."""
        check_capability("voice", "list_calls")
        return get_voice_call_summary(window_hours=window_hours).model_dump()
