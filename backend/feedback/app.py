"""HTTP surface for the feedback workcell (doc §9).

Single ingestion endpoint that accepts either:
  - a raw SNS HTTPS subscription / notification body, or
  - a TaskEnvelope wrapper whose ``payload`` is an SNS notification.

Capability ``ingest`` is registered into the in-process capabilities registry on
module import so the gateway / direct calls both pass through ``check_capability``.

This endpoint is deployed on Cloud Run with ``--ingress=all`` (public). Every
inbound SNS body is **signature-verified** against AWS's X.509 SigningCert
before any business logic runs — see :mod:`backend.feedback.sns_signature`.
The verifier is a dependency on the route; a forged or tampered payload
returns ``403`` and never reaches the suppression handlers.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any
from urllib.parse import urlparse

import httpx
from fastapi import HTTPException, Request
from pydantic import ValidationError

from backend.common import persistence
from backend.common.app_factory import create_base_app
from backend.common.capabilities import SERVICE_CAPABILITIES, check_capability
from backend.common.config import get_settings
from backend.common.idempotency import GLOBAL_IDEMPOTENCY_STORE
from backend.heat import service as heat_service
from backend.heat.sendgrid_signature import (
    SendGridSignatureError,
    verify_sendgrid_signature,
)

from . import handlers
from .models import FeedbackResult, SnsNotification
from .service import process_sns_notification
from .sns_signature import (
    SnsSignatureError,
    _SIGNING_CERT_HOST_RE,
    verify_sns_message,
)

_LOG = logging.getLogger("samus.feedback.app")

# Register the feedback capability surface at import time.
SERVICE_CAPABILITIES.setdefault("feedback", set()).update({"ingest", "engagement"})

# ``/api/ses/feedback`` is carved out of VerifyHMACMiddleware: AWS SNS
# signs every notification with its own X.509 ``SigningCert`` scheme,
# verified inside the route handler via verify_sns_message -> 403 on
# rejection. The feedback workcell only exposes this single ingress
# endpoint today, but the carve-out is path-scoped (not workcell-scoped)
# so future internal routes added to the feedback workcell will still
# require HMAC by default.
app = create_base_app(
    service_name="feedback",
    hmac_exempt_paths=("/api/ses/feedback", "/api/sendgrid/events"),
)


def _coerce_sns_body(body: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    """Accept either a TaskEnvelope wrapper or a direct SNS body.

    Returns ``(raw_sns_dict, task_id)``. The raw dict is what the signature
    verifier consumes; the pydantic ``SnsNotification`` is constructed later
    against the same dict so verify-and-parse see identical bytes.
    """
    task_id: str | None = None
    if isinstance(body, dict) and "task_id" in body and "payload" in body:
        task_id = str(body.get("task_id")) or None
        payload = body.get("payload") or {}
        if not isinstance(payload, dict):
            raise HTTPException(status_code=422, detail="payload must be an object")
        return payload, task_id
    return body, task_id


def _replay_key(message_id: str) -> str:
    return f"sns:msgid:{message_id}"


_REPLAY_LEDGER_PATH_DEFAULT = "/opt/samus/data/feedback/sns_replay.jsonl"


def _replay_ledger() -> persistence.Ledger:
    """Open the SNS-replay claim ledger via the pluggable backend (FIN-05).

    The in-process ``GLOBAL_IDEMPOTENCY_STORE`` is an OrderedDict that a
    container restart empties — so a captured SNS-signed notification could be
    replayed post-restart to suppress arbitrary emails (DNC poisoning). The
    persistent ledger's atomic ``claim()`` survives restart and (under the
    ``firestore`` backend) cross-instance scale-out, exactly as the Stripe
    webhook's ``claim_event_id`` does for Stripe event ids.

    ``jsonl`` (default — local / VM) uses an ``O_CREAT|O_EXCL`` claim file;
    ``firestore`` (Cloud Run, ``SAMUS_LEDGER_BACKEND=firestore``) uses a
    conditional ``create()`` per MessageId. The on-disk path is operator-
    overridable via ``SAMUS_FEEDBACK_REPLAY_PATH`` so tests get a per-case
    tmp file.
    """
    path = os.getenv("SAMUS_FEEDBACK_REPLAY_PATH", _REPLAY_LEDGER_PATH_DEFAULT)
    return persistence.open_ledger(
        jsonl_path=path,
        collection="sns_feedback_replay",
    )


def _claim_replay(message_id: str) -> bool:
    """Atomically claim ``message_id`` across restarts. True iff first sighting.

    Combines the fast in-process LRU (cheap reject of a hot duplicate within
    one process) with the persistent ledger ``claim()`` (the authoritative
    cross-restart / cross-instance gate). Both must agree this is the first
    sighting for the message to be processed:

    * if the in-process store has already seen the key -> duplicate (False);
    * otherwise consult the persistent ledger -> True only if THIS caller wins
      the persistent claim.

    The in-process ``first_seen`` is advanced only when the persistent claim is
    won, so a transient persistent-backend ``False`` (already-claimed) is not
    masked by the in-process placeholder on a subsequent retry. When the
    persistent backend is the jsonl/dev one, ``claim`` fails OPEN on a disk
    OSError (returns True) — the in-process store still catches sequential
    in-process retries, preserving prior dev behaviour.
    """
    key = _replay_key(message_id)
    # Fast path: a duplicate already seen in THIS process is rejected without
    # touching disk. (first_seen sets a placeholder + returns True on first.)
    if not GLOBAL_IDEMPOTENCY_STORE.first_seen(key):
        return False
    # Authoritative path: survives restart / scales across instances.
    return _replay_ledger().claim(key)


def _signature_verification_enabled() -> bool:
    """Default ON. Set ``SAMUS_FEEDBACK_VERIFY_SNS=0`` to disable (tests only).

    Production / Cloud Run must leave this enabled. The flag exists so the
    pre-existing test suite — which constructs unsigned payloads — keeps
    working without touching its fixtures. A dedicated signature test file
    exercises the verifier path explicitly.

    Production guard (finding L2, CWE-294): with both SNS signature and replay
    defense gated on this flag, turning it off in a production environment
    would leave ``/api/ses/feedback`` open to forged + replayed SES events.
    Mirroring the Stripe webhook's ``is_production`` live-mode gate
    (``backend.finance.webhook``), this fails **closed** — when the runtime
    env is production-class and the flag is disabled, a ``RuntimeError`` is
    raised so the workcell refuses the request rather than silently accepting
    unverified input.
    """
    raw = os.getenv("SAMUS_FEEDBACK_VERIFY_SNS", "1").strip().lower()
    enabled = raw not in ("0", "false", "no", "off", "")
    if not enabled:
        try:
            is_production = bool(getattr(get_settings(), "is_production", False))
        except Exception:  # noqa: BLE001 — settings unavailable -> treat as non-prod
            is_production = False
        if is_production:
            raise RuntimeError(
                "SAMUS_FEEDBACK_VERIFY_SNS cannot be disabled in a production "
                "environment — SNS signature + replay verification is mandatory."
            )
    return enabled


def _subscribe_url_is_aws_sns(subscribe_url: str) -> bool:
    """True iff ``subscribe_url`` is an HTTPS URL on the AWS SNS host allowlist.

    The signature on a ``SubscriptionConfirmation`` only proves the message was
    produced by *some* SNS topic — an attacker can create their own topic and
    point its ``SubscribeURL`` anywhere (finding M4, CWE-918 SSRF). The host
    must therefore be re-validated independently against the same
    ``sns.<region>.amazonaws.com`` allowlist already enforced for
    ``SigningCertURL`` (see ``sns_signature._validate_signing_cert_url``).
    """
    parsed = urlparse(subscribe_url)
    if parsed.scheme.lower() != "https":
        return False
    host = parsed.hostname or ""
    return bool(_SIGNING_CERT_HOST_RE.match(host))


def _confirm_subscription(subscribe_url: str) -> None:
    """GET the SubscribeURL to complete an SNS SubscriptionConfirmation handshake.

    Only invoked **after** signature verification has passed. The signature
    proves the body came from an SNS topic, but **not** that the
    ``SubscribeURL`` host is an AWS endpoint — so the host is independently
    validated against the AWS SNS allowlist (finding M4) before any request is
    issued. A non-AWS / non-HTTPS URL is logged and dropped without a fetch.

    Failures here are logged but not raised — the message has already been
    audited as confirmed and AWS will retry the confirmation if needed.
    """
    if not subscribe_url:
        return
    if not _subscribe_url_is_aws_sns(subscribe_url):
        _LOG.warning(
            "SNS SubscribeURL rejected — host not in AWS SNS HTTPS allowlist: url=%s",
            subscribe_url,
        )
        return
    try:
        resp = httpx.get(subscribe_url, timeout=10.0, follow_redirects=False)
        if resp.status_code != 200:
            _LOG.warning(
                "SNS SubscribeURL GET returned status=%s url=%s",
                resp.status_code,
                subscribe_url,
            )
        else:
            _LOG.info("SNS subscription confirmed via SubscribeURL")
    except Exception as exc:
        _LOG.warning("SNS SubscribeURL GET failed: %s url=%s", exc, subscribe_url)


@app.post("/api/ses/feedback", response_model=FeedbackResult)
async def ingest_ses_feedback(request: Request) -> FeedbackResult:
    """Ingest an SES-via-SNS notification. Capability: ``ingest``."""
    check_capability("feedback", "ingest")

    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"invalid JSON: {exc}") from exc

    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="body must be a JSON object")

    raw_sns, task_id = _coerce_sns_body(body)
    if not isinstance(raw_sns, dict):
        raise HTTPException(status_code=422, detail="SNS payload must be a JSON object")

    # ---- Strict signature verification (runs BEFORE any business logic) ----
    verify_on = _signature_verification_enabled()
    if verify_on:
        try:
            verify_sns_message(raw_sns)
        except SnsSignatureError as exc:
            _LOG.warning(
                "SNS signature verification rejected payload: %s message_id=%s",
                exc,
                raw_sns.get("MessageId"),
            )
            raise HTTPException(status_code=403, detail=f"SNS signature invalid: {exc}") from exc

    try:
        sns = SnsNotification.model_validate(raw_sns)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors()) from exc

    # Replay gate: a verified message proves AWS sent it once, but an attacker
    # with one captured signed body could POST it forever. Atomically claim the
    # MessageId through the PERSISTENT ledger (FIN-05) so the dedup survives a
    # container restart — the in-process OrderedDict alone empties on restart,
    # letting a captured signed body replay to suppress arbitrary emails (DNC
    # poisoning). Second-and-later POSTs short-circuit with a Replay result and
    # never run handlers or confirm a subscription. 200 is intentional — non-2xx
    # makes SNS retry indefinitely. The gate only runs when signature
    # verification is enabled: without it, MessageId is attacker-controlled and
    # replay defense is meaningless. Internal TaskEnvelope flows (verify off)
    # rely on the existing task_id-keyed idempotency in the service layer.
    if verify_on and sns.MessageId and not _claim_replay(sns.MessageId):
        _LOG.info("SNS replay rejected message_id=%s type=%s", sns.MessageId, sns.Type)
        return FeedbackResult(
            notification_type="Replay",
            recipient_count=0,
            suppressed=[],
            event_id=sns.MessageId,
        )

    # SubscriptionConfirmation handshake: now-verified URL is safe to GET.
    if sns.Type == "SubscriptionConfirmation" and sns.SubscribeURL:
        _confirm_subscription(sns.SubscribeURL)

    return process_sns_notification(sns, task_id=task_id)


@app.post("/api/feedback/engagement")
async def fire_engagement(request: Request) -> dict[str, Any]:
    """Prospect-engagement signal -> cash engine. Capability: ``engagement``.

    The re-engagement webhook the cash engine waits on while a deal is
    dormant: a positive event (``reply`` / ``open`` / ``visit`` / ``manual``)
    drafts a fresh voicemail + operator task; a negative one (``bounce`` /
    ``complaint`` / ``unsubscribe``) halts the deal. Identify the deal by
    ``opportunity_id`` or ``prospect_id``.

    HMAC-gated: this path is NOT in the SNS carve-out, so VerifyHMACMiddleware
    requires a signed internal caller (operator console / peer workcell). The
    handler is best-effort and always returns a structured verdict.
    """
    check_capability("feedback", "engagement")

    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"invalid JSON: {exc}") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="body must be a JSON object")

    event = str(body.get("event") or "").strip()
    if not event:
        raise HTTPException(status_code=422, detail="event is required")

    return handlers.fire_cash_engine_signal(
        event=event,
        opportunity_id=str(body.get("opportunity_id") or ""),
        prospect_id=str(body.get("prospect_id") or ""),
    )


def _sendgrid_verify_enabled() -> bool:
    """Default ON. ``SAMUS_SENDGRID_VERIFY_EVENTS=0`` disables (tests only).

    With no verification key configured the route fails **closed** in a
    production env and accepts unverified input in dev — the same prod/dev
    posture as the SES SNS verifier above.
    """
    raw = os.getenv("SAMUS_SENDGRID_VERIFY_EVENTS", "1").strip().lower()
    return raw not in ("0", "false", "no", "off", "")


@app.post("/api/sendgrid/events")
async def ingest_sendgrid_events(request: Request) -> dict[str, Any]:
    """Ingest a SendGrid Event Webhook batch. Capability: ``ingest``.

    Public webhook (carved out of VerifyHMACMiddleware). The ECDSA signature is
    verified against ``settings.sendgrid_webhook_verification_key`` over
    ``timestamp + raw_body`` BEFORE any parsing or business logic. The body is a
    JSON array of event objects; each is folded into the Heat Field counters and
    negative events fan out to the cash-engine halt loop + suppression list.
    """
    check_capability("feedback", "ingest")

    raw_body = await request.body()
    settings = get_settings()
    verify_on = _sendgrid_verify_enabled()
    key = str(getattr(settings, "sendgrid_webhook_verification_key", "") or "")

    if verify_on:
        if key:
            sig = request.headers.get("X-Twilio-Email-Event-Webhook-Signature", "")
            ts = request.headers.get("X-Twilio-Email-Event-Webhook-Timestamp", "")
            try:
                verify_sendgrid_signature(
                    public_key_b64=key,
                    payload=raw_body,
                    signature_b64=sig,
                    timestamp=ts,
                )
            except SendGridSignatureError as exc:
                _LOG.warning("SendGrid event signature rejected: %s", exc)
                raise HTTPException(
                    status_code=403,
                    detail=f"sendgrid signature invalid: {exc}",
                ) from exc
        else:
            try:
                is_production = bool(getattr(settings, "is_production", False))
            except Exception:  # noqa: BLE001 — settings unavailable -> non-prod
                is_production = False
            if is_production:
                raise HTTPException(
                    status_code=403,
                    detail="sendgrid webhook verification key not configured",
                )
            _LOG.warning("SendGrid event webhook accepted UNVERIFIED (no key, non-prod)")

    try:
        events = json.loads(raw_body or b"[]")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"invalid JSON: {exc}") from exc
    if not isinstance(events, list):
        raise HTTPException(status_code=422, detail="body must be a JSON array of events")

    return heat_service.ingest_sendgrid_events(events)
