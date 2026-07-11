"""Voice workcell FastAPI service.

Endpoints:

  - ``POST /voice/call``           — operator-initiated outbound call
  - ``GET  /voice/call/{id}``      — fetch call status
  - ``GET  /voice/calls``          — list recent calls
  - ``POST /voice/dial_call_list`` — morning autodialer over a CSV callsheet
  - ``GET  /voice/summary``        — morning-briefing rollup
  - ``POST /vapi/webhook``         — Vapi pushes events here (sig-verified)
  - ``POST /work``                 — TaskEnvelope route for SQS / gateway parity

The webhook is signature-verified against ``VAPI_WEBHOOK_SECRET`` before any
business logic runs. The flag ``SAMUS_VOICE_VERIFY_WEBHOOK`` (default ON) can
be set to ``0`` for the test suite — same pattern the feedback workcell uses
for SNS verification.
"""
from __future__ import annotations

import logging
import os
from typing import Any

from fastapi import Depends, HTTPException, Request

from backend.common.app_factory import create_base_app
from backend.common.capabilities import check_capability
from backend.common.config import get_settings
from backend.common.models import TaskEnvelope
from backend.common.rate_limit import rate_limit_dependency

from .models import (
    CallListResult,
    CallStatusResult,
    DialCallListRequest,
    DialRunResult,
    InitiateCallRequest,
    InitiateCallResult,
    TuneResult,
    VapiWebhookEvent,
    VoiceCallSummary,
    WebhookResult,
)
from .service import (
    get_call_status,
    get_voice_call_summary,
    handle_webhook_event,
    initiate_call,
    list_recent_calls,
)
from .adaptive import adapt as _adaptive_adapt
from .console import CONSOLE_PATHS, register_console_routes
from .signature import VapiSignatureError, verify_vapi_signature, verify_vapi_webhook


_LOG = logging.getLogger("samus.voice.app")


def _signature_verification_enabled() -> bool:
    """Whether the Vapi webhook signature check runs.

    Default ON. ``SAMUS_VOICE_VERIFY_WEBHOOK=0`` disables it — but ONLY
    outside a production environment.

    Finding L3 (2026-05-20): the disable flag previously had no production
    guard, so a stray ``=0`` in a prod env (or an attacker who could touch
    the env) silently turned off Vapi signature verification on
    ``/vapi/webhook``. Now, in a production-class env (``SAMUS_ENV`` =
    production / prod) verification is always on regardless of the flag —
    mirroring the development-vs-production idiom in
    ``backend.common.app_factory`` and the Stripe webhook live-mode gate.
    The flag still works in development / test so the suite can post
    synthetic unsigned payloads.
    """
    if get_settings().is_production:
        return True
    raw = os.getenv("SAMUS_VOICE_VERIFY_WEBHOOK", "1").strip().lower()
    return raw not in ("0", "false", "no", "off", "")


def _parse(model_cls, payload: Any):
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail=f"expected_{model_cls.__name__}_object")
    try:
        return model_cls.model_validate(payload)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"invalid_{model_cls.__name__}: {exc}")


def _voice_port() -> int:
    """Port samus-voice binds inside the container — used by the tunnel."""
    try:
        return int(os.getenv("SAMUS_PORT", "8080"))
    except ValueError:
        return 8080


def _run_tunnel_startup() -> None:
    """Open the embedded ngrok tunnel + auto-PATCH the Vapi assistant.

    Best-effort: every degraded path logs a warning and returns; the
    workcell stays up regardless. Pulls config from settings at call time
    so test fixtures can monkeypatch get_settings() before app boot.
    """
    from .tunnel import patch_vapi_assistant_server_url, start_ngrok_listener
    settings = get_settings()
    tr = start_ngrok_listener(
        port=_voice_port(),
        authtoken=settings.ngrok_authtoken,
        reserved_domain=settings.ngrok_reserved_domain,
    )
    if not tr.url:
        # start_ngrok_listener already logged the reason; nothing more to do.
        return
    # Tunnel up — PATCH every configured assistant so Vapi knows where to
    # deliver end-of-call reports. Append the canonical webhook path; the
    # operator doesn't have to know to include it in any config. Both the
    # outbound sales assistant and the inbound receptionist assistant share
    # the one tunnel + the one /vapi/webhook surface; empty ids are skipped
    # inside patch_vapi_assistant_server_url.
    webhook_url = tr.url.rstrip("/") + "/vapi/webhook"
    for assistant_id in (
        settings.vapi_assistant_id,
        settings.vapi_inbound_assistant_id,
    ):
        if not assistant_id:
            continue
        patch_vapi_assistant_server_url(
            assistant_id=assistant_id,
            server_url=webhook_url,
            server_url_secret=settings.vapi_webhook_secret,
            vapi_api_key=settings.vapi_api_key,
        )


def create_app():
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _lifespan(app):  # pragma: no cover — exercised via TestClient
        if not _signature_verification_enabled():
            _LOG.warning(
                "SECURITY: Vapi webhook signature verification is DISABLED "
                "(SAMUS_VOICE_VERIFY_WEBHOOK=0). Any caller can POST to "
                "/vapi/webhook without authentication. Set to 1 before production."
            )
        # Fail-loud dispatch-config check. A missing routing URL doesn't stop
        # the workcell (dispatches are best-effort by design) but it silently
        # drops EVERY call outcome — the 7/02 crm_url_unset outage lost a full
        # day of CRM writes with zero log evidence. ERROR at boot is the alarm.
        _urls = get_settings().gateway_urls or {}
        for _dep in ("crm", "memory"):
            if not (_urls.get(_dep) or "").strip():
                _LOG.error(
                    "DISPATCH CONFIG: gateway_urls[%r] is unset — every "
                    "end-of-call %s dispatch will fail silently "
                    "(crm_url_unset). Set %s_URL or SAMUS_GATEWAY_URLS on "
                    "the voice container.", _dep, _dep, _dep.upper(),
                )
        _run_tunnel_startup()
        yield
        # Shutdown: nothing to do today. ngrok listener is a daemon and
        # ngrok-side cleans up the tunnel after the process exits; we don't
        # bother explicitly closing it.

    # ``/vapi/webhook`` is carved out of VerifyHMACMiddleware: Vapi signs
    # its POSTs with ``X-Vapi-Signature`` (verified inside the route handler
    # via verify_vapi_signature -> VapiSignatureError -> 403). The operator
    # console paths (``/console`` + ``/console/api/*``) are also carved out —
    # a browser cannot hold the shared HMAC key, so the console enforces its
    # own ``SAMUS_VOICE_CONSOLE_TOKEN`` bearer-token gate instead (see
    # backend.voice.console). Operator-side mesh routes (/voice/call,
    # /voice/calls, /work, ...) still require HMAC.
    app = create_base_app(
        service_name="voice",
        lifespan=_lifespan,
        hmac_exempt_paths=("/vapi/webhook", *CONSOLE_PATHS),
    )

    # --- outbound (operator -> Vapi) -----------------------------------

    # M7 — the outbound-call routes place real phone calls (Vapi side effect,
    # metered cost). A per-caller rate limit caps a runaway loop / a
    # compromised caller before it can dial out without bound. Env-tunable via
    # SAMUS_RATE_LIMIT_VOICE_CALL_PER_MINUTE / ..._DIAL_CALL_LIST_PER_MINUTE.
    @app.post(
        "/voice/call",
        response_model=InitiateCallResult,
        dependencies=[Depends(rate_limit_dependency("voice_call"))],
    )
    async def call_post(request: Request) -> InitiateCallResult:
        check_capability("voice", "initiate_call")
        body = await request.json()
        req = _parse(InitiateCallRequest, body)
        return initiate_call(req)

    @app.get("/voice/call/{call_id}", response_model=CallStatusResult)
    async def call_get(call_id: str) -> CallStatusResult:
        check_capability("voice", "fetch_call")
        if not call_id:
            raise HTTPException(status_code=422, detail="call_id required")
        return get_call_status(call_id)

    @app.get("/voice/calls", response_model=CallListResult)
    async def calls_list(limit: int = 10) -> CallListResult:
        check_capability("voice", "list_calls")
        return list_recent_calls(limit=limit)

    @app.post(
        "/voice/dial_call_list",
        response_model=DialRunResult,
        dependencies=[Depends(rate_limit_dependency("voice_dial_call_list"))],
    )
    async def dial_call_list_post(request: Request) -> DialRunResult:
        check_capability("voice", "initiate_call")
        body = await request.json()
        req = _parse(DialCallListRequest, body)
        # Lazy import so the dialer module's prospecting dependency isn't
        # dragged into the app graph when no one calls this endpoint.
        from pathlib import Path as _Path
        from .dialer import dial_call_list, default_csv_path_for_today
        csv_path = _Path(req.csv_path) if req.csv_path else default_csv_path_for_today()
        if not csv_path.exists():
            raise HTTPException(
                status_code=404, detail=f"call_list_csv_not_found: {csv_path}",
            )
        try:
            return dial_call_list(csv_path, req.config)
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.get("/voice/summary", response_model=VoiceCallSummary)
    async def voice_summary(window_hours: int = 24) -> VoiceCallSummary:
        check_capability("voice", "list_calls")
        return get_voice_call_summary(window_hours=window_hours)

    @app.post("/voice/maintenance")
    async def voice_maintenance() -> dict:
        """Compact JSONL ledgers and retry queue, dropping records older than 30 days."""
        check_capability("voice", "list_calls")
        import os as _os
        from backend.common import persistence as _p
        results: dict[str, int] = {}
        for env_var, default, label in (
            ("SAMUS_VOICE_EVENTS_PATH", "/opt/samus/data/voice/voice_events.jsonl", "events"),
            ("SAMUS_VOICE_AUDIT_PATH",  "/opt/samus/data/voice/voice_audit.jsonl",  "audit"),
        ):
            try:
                ledger = _p.JsonlLedger(_os.getenv(env_var, default))
                results[label] = ledger.rotate_by_age(max_age_hours=720)  # 30 days
            except Exception as exc:  # noqa: BLE001
                _LOG.warning("maintenance: %s rotation failed: %s", label, exc)
                results[label] = -1
        try:
            from .retry import get_retry_entries as _gre
            # clear_entry is per-prospect; rotation is handled by max_age_hours
            # in get_retry_entries. The file itself is bounded by the 36h window
            # the dialer uses, so compact to 48h to keep a one-day buffer.
            from .retry import _queue_path, _queue_lock
            import json as _json, datetime as _dt
            cutoff = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=48)
            path = _queue_path()
            if path.exists():
                with _queue_lock:
                    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
                    kept = []
                    dropped = 0
                    for line in lines:
                        try:
                            rec = _json.loads(line.strip())
                            ts_raw = rec.get("ts") or ""
                            ts = _dt.datetime.strptime(ts_raw, "%Y-%m-%dT%H:%M:%SZ").replace(
                                tzinfo=_dt.timezone.utc) if ts_raw else None
                            if ts and ts < cutoff:
                                dropped += 1
                                continue
                        except Exception:  # noqa: BLE001
                            pass
                        kept.append(line)
                    with path.open("w", encoding="utf-8") as fh:
                        fh.writelines(kept)
                    results["retry_queue"] = dropped
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("maintenance: retry queue rotation failed: %s", exc)
            results["retry_queue"] = -1
        return {"compacted": results}

    @app.post("/voice/tune", response_model=TuneResult)
    async def voice_tune(request: Request) -> TuneResult:
        check_capability("voice", "tune_assistant")
        body = await request.json()
        dry_run = bool(body.get("dry_run", True))
        window_calls = int(body.get("window_calls", 20))
        min_calls = int(body.get("min_calls", 5))
        settings = get_settings()
        from .tuner import tune_assistant
        return tune_assistant(
            assistant_id=settings.vapi_assistant_id or "",
            window_calls=window_calls,
            min_calls=min_calls,
            dry_run=dry_run,
        )

    # --- inbound (Vapi -> us) ------------------------------------------

    @app.post(
        "/vapi/webhook",
        response_model=WebhookResult,
        dependencies=[Depends(rate_limit_dependency("vapi_webhook"))],
    )
    async def vapi_webhook(request: Request) -> WebhookResult:
        check_capability("voice", "handle_webhook")
        raw = await request.body()

        if _signature_verification_enabled():
            settings = get_settings()
            secret = (settings.vapi_webhook_secret or "").strip()
            if not secret:
                _LOG.error("vapi webhook rejected: VAPI_WEBHOOK_SECRET unset")
                raise HTTPException(
                    status_code=503,
                    detail="vapi_webhook_secret_unset",
                )
            sig = request.headers.get("x-vapi-signature")
            secret_header = request.headers.get("x-vapi-secret")
            try:
                verify_vapi_webhook(
                    raw, signature=sig, secret_header=secret_header, secret=secret,
                )
            except VapiSignatureError as exc:
                _LOG.warning("vapi webhook signature rejected: %s", exc)
                raise HTTPException(
                    status_code=403,
                    detail=f"vapi_signature_invalid: {exc}",
                ) from exc

        try:
            body = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"invalid JSON: {exc}") from exc
        if not isinstance(body, dict):
            raise HTTPException(status_code=422, detail="body must be a JSON object")
        try:
            event = VapiWebhookEvent.model_validate(body)
        except Exception as exc:
            raise HTTPException(status_code=422, detail=f"invalid_VapiWebhookEvent: {exc}")

        return await handle_webhook_event(event)

    # --- operator tooling -------------------------------------------------

    @app.get("/voice/retry-queue")
    async def retry_queue_get(max_age_hours: int = 48) -> dict:
        """Return current retry queue entries (no-answer/voicemail candidates)."""
        check_capability("voice", "list_calls")
        from .retry import get_retry_entries
        entries = get_retry_entries(max_age_hours=max_age_hours)
        return {"depth": len(entries), "entries": entries}

    @app.post("/voice/suppression")
    async def suppression_add(request: Request) -> dict:
        """Manually add a phone number to the DNC suppression table."""
        check_capability("voice", "initiate_call")
        body = await request.json()
        phone = str(body.get("phone") or "").strip()
        if not phone:
            raise HTTPException(status_code=422, detail="phone required")
        from .service import _write_suppression
        _write_suppression(phone, prospect_id=str(body.get("prospect_id") or ""), call_id="manual")
        return {"suppressed": phone}

    @app.delete("/voice/suppression/{phone}")
    async def suppression_remove(phone: str) -> dict:
        """Remove a phone number from the DNC suppression table."""
        check_capability("voice", "initiate_call")
        import re
        safe = re.sub(r"[^\d+]", "", phone)
        if not safe:
            raise HTTPException(status_code=422, detail="invalid phone")
        try:
            import boto3
            from backend.common.config import get_settings as _gs
            settings = _gs()
            table = boto3.resource("dynamodb", region_name="us-west-1").Table(
                settings.ddb_suppression_table or "samus_suppression"
            )
            table.delete_item(Key={"phone": safe})
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("suppression delete failed for %s: %s", safe, exc)
            raise HTTPException(status_code=503, detail=f"suppression_delete_failed: {exc}") from exc
        return {"removed": safe}

    # --- TaskEnvelope parity (gateway / future SQS) --------------------

    @app.post("/work")
    async def work(request: Request) -> dict[str, Any]:
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="expected_json_object")
        try:
            envelope = TaskEnvelope.model_validate(body)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"invalid_envelope: {exc}")
        action = (envelope.metadata or {}).get("action") or "initiate_call"
        payload = envelope.payload if isinstance(envelope.payload, dict) else {}
        if action == "initiate_call":
            check_capability("voice", "initiate_call")
            return initiate_call(InitiateCallRequest.model_validate(payload)).model_dump()
        if action == "fetch_call":
            check_capability("voice", "fetch_call")
            call_id = str(payload.get("call_id") or "")
            if not call_id:
                raise HTTPException(status_code=422, detail="call_id required")
            return get_call_status(call_id).model_dump()
        if action == "list_calls":
            check_capability("voice", "list_calls")
            limit = int(payload.get("limit") or 10)
            return list_recent_calls(limit=limit).model_dump()
        if action == "adapt_call":
            # TIER-3 / WIRING NOTE: adapt_call is functional as a pure decision
            # layer but the LIVE call site (Vapi mid-call transcript stream →
            # adapt → closer next-state) does not exist yet. This /work dispatch
            # allows offline testing + future gateway routing without requiring
            # live transcript streaming infrastructure.
            check_capability("voice", "adapt_call")
            transcript_text = payload.get("transcript_text")
            if not isinstance(transcript_text, str) or not transcript_text.strip():
                raise HTTPException(
                    status_code=422,
                    detail="adapt_call requires non-empty string: transcript_text",
                )
            current_state = payload.get("current_state")
            if not isinstance(current_state, str) or not current_state.strip():
                raise HTTPException(
                    status_code=422,
                    detail="adapt_call requires non-empty string: current_state",
                )
            intel = payload.get("intel")
            if intel is not None and not isinstance(intel, dict):
                raise HTTPException(
                    status_code=422,
                    detail="adapt_call: intel must be a dict or omitted",
                )
            return _adaptive_adapt(transcript_text, current_state, intel)
        raise HTTPException(status_code=400, detail=f"unknown_action: {action}")

    # --- transcript feedback pipeline ---------------------------------

    @app.get("/voice/pending_transcripts")
    async def pending_transcripts_get() -> dict:
        """List TalkerACR transcripts awaiting operator classification.

        These are calls whose phone number was not found in any prospect CSV.
        Review each entry and POST /voice/classify_transcript to either
        approve it as a business call or mark it as personal.
        """
        check_capability("voice", "list_calls")
        from dataclasses import asdict
        from .privacy_gate import list_pending
        entries = list_pending()
        return {"count": len(entries), "entries": [asdict(e) for e in entries]}

    @app.post("/voice/classify_transcript")
    async def classify_transcript_post(request: Request) -> dict:
        """Classify a pending transcript as business or personal.

        Body:
          filename   str   — exact filename from pending queue
          decision   str   — "approved" or "personal"
          prospect_id str? — link to a known prospect (optional, approved only)
          note       str?  — operator note

        Approved calls are analyzed on the next /voice/ingest_transcripts run.
        Personal calls are archived and permanently excluded from enrichment.
        """
        check_capability("voice", "tune_assistant")
        body = await request.json()
        filename = str(body.get("filename") or "").strip()
        decision = str(body.get("decision") or "").strip().lower()
        if not filename:
            raise HTTPException(status_code=422, detail="filename required")
        if decision not in ("approved", "personal"):
            raise HTTPException(status_code=422, detail="decision must be 'approved' or 'personal'")

        from .privacy_gate import mark_approved, mark_personal
        note = str(body.get("note") or "").strip() or None
        if decision == "approved":
            prospect_id = str(body.get("prospect_id") or "").strip() or None
            company_name = str(body.get("company_name") or "").strip() or None
            mark_approved(filename, prospect_id=prospect_id, company_name=company_name, operator_note=note)
            return {"classified": filename, "decision": "approved", "prospect_id": prospect_id}
        else:
            mark_personal(filename, operator_note=note)
            return {"classified": filename, "decision": "personal"}

    @app.post("/voice/ingest_transcripts")
    async def ingest_transcripts_post(request: Request) -> dict:
        """Pull TalkerACR recordings from Z Flip5, transcribe locally,
        analyze with the local LLM, aggregate patterns, and update
        callsheet objection handlers + WTLF signals. Fully offline.

        Body (all optional):
          dry_run         bool — skip callsheet write (default false)
          skip_sync       bool — skip MTP file pull; use staging as-is
          skip_transcribe bool — skip Whisper STT; .txt sidecars must exist
        """
        check_capability("voice", "tune_assistant")
        body = await request.json()
        dry_run = bool(body.get("dry_run", False))
        skip_sync = bool(body.get("skip_sync", False))
        skip_transcribe = bool(body.get("skip_transcribe", False))
        from .ingest_pipeline import run_ingest_pipeline
        return run_ingest_pipeline(
            dry_run=dry_run,
            skip_sync=skip_sync,
            skip_transcribe=skip_transcribe,
        )

    @app.get("/voice/patterns")
    async def voice_patterns_get() -> dict:
        """Return the current aggregated call pattern report."""
        check_capability("voice", "list_calls")
        from dataclasses import asdict
        from .pattern_aggregator import load_pattern_report
        report = load_pattern_report()
        if report is None:
            return {"error": "no_pattern_report_available", "hint": "run POST /voice/ingest_transcripts first"}
        return asdict(report)

    @app.get("/voice/session_status")
    async def voice_session_status_get() -> dict:
        """Return intraday session monitor state and last adjustment."""
        check_capability("voice", "list_calls")
        from .call_session_monitor import (
            _build_intraday_report,
            _is_enabled,
            load_session_adjustment,
        )
        from dataclasses import asdict
        report = _build_intraday_report()
        adj = load_session_adjustment()
        return {
            "enabled": _is_enabled(),
            "intraday": asdict(report),
            "last_adjustment": asdict(adj) if adj else None,
        }

    @app.post("/voice/session_check")
    async def voice_session_check_post() -> dict:
        """Force a session monitor check regardless of enable flag / cooldown."""
        check_capability("voice", "tune_assistant")
        from .call_session_monitor import check_session
        from dataclasses import asdict
        adj = check_session(force=True)
        if adj is None:
            return {"triggered": False, "reason": "no trigger conditions met or insufficient calls"}
        return {"triggered": True, "adjustment": asdict(adj)}

    @app.get("/voice/strategy_brief")
    async def voice_strategy_brief_get() -> dict:
        """Return the latest day-over-day strategy brief."""
        check_capability("voice", "list_calls")
        from dataclasses import asdict
        from .call_strategy_reasoner import load_strategy_brief
        brief = load_strategy_brief()
        if brief is None:
            return {"error": "no_strategy_brief_available", "hint": "run the call feedback pipeline first"}
        return asdict(brief)

    @app.get("/voice/cost_snapshot")
    async def voice_cost_snapshot_get(force: bool = False) -> dict:
        """Return a Vapi + Twilio cost snapshot for CODB tracking."""
        check_capability("voice", "list_calls")
        from dataclasses import asdict
        from .voice_cost_tracker import voice_codb_snapshot
        snap = voice_codb_snapshot(force=force)
        return asdict(snap)

    @app.get("/voice/callsheet_intel")
    async def voice_callsheet_intel_get() -> dict:
        """Return the current dynamic callsheet intel (objection handlers, signals, etc.)."""
        check_capability("voice", "list_calls")
        from .callsheet_updater import load_dynamic_callsheet_intel
        intel = load_dynamic_callsheet_intel()
        if intel is None:
            return {"error": "no_callsheet_intel_available", "hint": "run POST /voice/ingest_transcripts first"}
        return intel

    # --- operator browser console -------------------------------------
    # GET /console (page) + /console/api/* (token-gated JSON). Registered
    # last so the HMAC-signed routes above keep their precedence.
    register_console_routes(app)

    return app


app = create_app()
