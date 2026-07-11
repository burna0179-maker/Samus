"""SendGrid Event Webhook signature verification (ECDSA P-256).

SendGrid signs each event-webhook POST with ECDSA: the signature (base64, ASN.1
DER) in ``X-Twilio-Email-Event-Webhook-Signature`` covers
``timestamp + raw_request_body`` where ``timestamp`` is the value of
``X-Twilio-Email-Event-Webhook-Timestamp``. The public verification key
(base64 DER SubjectPublicKeyInfo) is shown in the SendGrid Mail Settings →
Signed Event Webhook UI and configured here via
``settings.sendgrid_webhook_verification_key``.

Mirrors the posture of the SES SNS verifier (``feedback.sns_signature``): a
forged/tampered body is rejected before any business logic runs. ``cryptography``
is imported lazily so a box without it (or without a configured key) degrades
gracefully per the caller's prod/dev gate rather than hard-failing at import.
"""

from __future__ import annotations

import base64
import logging

_LOG = logging.getLogger("samus.heat.sendgrid_signature")


class SendGridSignatureError(Exception):
    """Raised when a SendGrid event-webhook signature fails verification."""


def verify_sendgrid_signature(
    *,
    public_key_b64: str,
    payload: bytes,
    signature_b64: str,
    timestamp: str,
) -> None:
    """Verify a SendGrid event-webhook ECDSA signature.

    Raises :class:`SendGridSignatureError` on any failure (bad key, bad
    signature, missing crypto lib). The signed message is
    ``timestamp.encode() + payload`` (raw request bytes, pre-JSON-parse).
    """
    if not public_key_b64:
        raise SendGridSignatureError("no verification key configured")
    if not signature_b64 or not timestamp:
        raise SendGridSignatureError("missing signature or timestamp header")

    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.exceptions import InvalidSignature
    except Exception as exc:  # noqa: BLE001 — crypto lib unavailable
        raise SendGridSignatureError(f"cryptography unavailable: {exc}") from exc

    try:
        key_der = base64.b64decode(public_key_b64)
        public_key = serialization.load_der_public_key(key_der)
    except Exception as exc:  # noqa: BLE001
        raise SendGridSignatureError(f"invalid verification key: {exc}") from exc

    try:
        signature = base64.b64decode(signature_b64)
    except Exception as exc:  # noqa: BLE001
        raise SendGridSignatureError(f"invalid signature encoding: {exc}") from exc

    signed_payload = timestamp.encode("utf-8") + payload
    try:
        public_key.verify(signature, signed_payload, ec.ECDSA(hashes.SHA256()))
    except InvalidSignature as exc:
        raise SendGridSignatureError("signature does not match payload") from exc
    except Exception as exc:  # noqa: BLE001 — wrong key type, etc.
        raise SendGridSignatureError(f"verification error: {exc}") from exc


__all__ = ["SendGridSignatureError", "verify_sendgrid_signature"]
