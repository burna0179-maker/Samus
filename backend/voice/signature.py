"""Vapi webhook signature verification (HMAC-SHA256).

Vapi signs webhook deliveries by computing HMAC-SHA256 over the raw request
body using a per-webhook shared secret configured in the Vapi dashboard. The
signature is delivered in the ``x-vapi-signature`` header as a lowercase hex
digest. This module is the strict verifier — it accepts the raw bytes and
returns nothing on success, raising :class:`VapiSignatureError` on any
failure. Callers wire it as the first step of the webhook handler so a
forged payload never reaches the dispatch logic.

The verifier uses :func:`hmac.compare_digest` so the wall-clock time to fail
is independent of how many leading bytes happened to match — that's the
standard defense against signature-timing oracle attacks.
"""

from __future__ import annotations

import hmac
from hashlib import sha256


class VapiSignatureError(Exception):
    """Raised when a Vapi webhook fails signature verification."""


def compute_signature(secret: str, body: bytes) -> str:
    """Return the canonical lowercase hex HMAC-SHA256 digest of ``body``."""
    if not isinstance(body, (bytes, bytearray)):
        raise TypeError("body must be bytes")
    if not secret:
        raise ValueError("compute_signature requires a non-empty secret")
    return hmac.new(secret.encode("utf-8"), bytes(body), sha256).hexdigest()


def verify_vapi_signature(
    body: bytes,
    provided_signature: str | None,
    secret: str,
) -> None:
    """Strict-verify a Vapi webhook signature.

    ``provided_signature`` may include the ``sha256=`` prefix some
    integrations emit; both forms are accepted.

    Raises :class:`VapiSignatureError` on any of:
      - secret unset / empty
      - signature header missing or empty
      - signature contains non-hex characters
      - signature does not match the recomputed HMAC

    No return value on success — exceptions are the contract.
    """
    if not secret:
        raise VapiSignatureError("VAPI_WEBHOOK_SECRET is not configured")
    if not provided_signature:
        raise VapiSignatureError("missing x-vapi-signature header")

    candidate = provided_signature.strip()
    if candidate.lower().startswith("sha256="):
        candidate = candidate.split("=", 1)[1]
    candidate = candidate.lower()

    # Reject non-hex early — compare_digest itself wouldn't, and a malformed
    # header is always an integration error worth surfacing distinctly.
    try:
        int(candidate, 16)
    except ValueError as exc:
        raise VapiSignatureError(
            f"x-vapi-signature is not a valid hex digest: {provided_signature!r}"
        ) from exc

    expected = compute_signature(secret, body)
    if not hmac.compare_digest(expected, candidate):
        raise VapiSignatureError("signature did not verify against VAPI_WEBHOOK_SECRET")


def verify_vapi_secret_header(provided_secret: str | None, secret: str) -> None:
    """Verify Vapi's ``x-vapi-secret`` header.

    This is Vapi's actual webhook-auth mechanism: when ``assistant.server.secret``
    is set, Vapi sends the raw shared secret verbatim in the ``x-vapi-secret``
    header on every server message (it does NOT HMAC-sign the body). Compared in
    constant time. Raises :class:`VapiSignatureError` on missing/mismatch.
    """
    if not secret:
        raise VapiSignatureError("VAPI_WEBHOOK_SECRET is not configured")
    if not provided_secret:
        raise VapiSignatureError("missing x-vapi-secret header")
    if not hmac.compare_digest(provided_secret.strip(), secret):
        raise VapiSignatureError("x-vapi-secret did not match VAPI_WEBHOOK_SECRET")


def verify_vapi_webhook(
    body: bytes,
    *,
    signature: str | None,
    secret_header: str | None,
    secret: str,
) -> None:
    """Verify an inbound Vapi webhook by whichever auth mechanism is present.

    Vapi's standard mechanism delivers the raw secret in ``x-vapi-secret``
    (``server.secret``). Some integrations instead HMAC-SHA256 the body and send
    ``x-vapi-signature``. Accept EITHER — prefer the raw-secret header since that
    is what current Vapi sends — and reject only when neither is present/valid.
    No return value on success; exceptions are the contract.
    """
    if not secret:
        raise VapiSignatureError("VAPI_WEBHOOK_SECRET is not configured")
    if secret_header:
        verify_vapi_secret_header(secret_header, secret)
        return
    if signature:
        verify_vapi_signature(body, signature, secret)
        return
    raise VapiSignatureError("missing x-vapi-secret and x-vapi-signature headers")


__all__ = [
    "VapiSignatureError",
    "compute_signature",
    "verify_vapi_signature",
    "verify_vapi_secret_header",
    "verify_vapi_webhook",
]
