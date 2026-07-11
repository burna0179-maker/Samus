"""Shared-secret verification for inbound DocuSeal webhooks.

Self-hosted DocuSeal does not HMAC-sign webhook deliveries, so we register a
custom header carrying a shared secret on the DocuSeal webhook config and verify
it here in constant time — the same fail-closed posture as the Vapi/Stripe
webhooks (``backend.voice.signature`` / ``backend.finance.webhook``). An on-box
attacker who cannot read ``DOCUSEAL_WEBHOOK_SECRET`` cannot forge a completion.

Header name is ``X-Docuseal-Secret`` by convention (set it in DocuSeal's webhook
"custom headers", e.g. ``X-Docuseal-Secret: <secret>``).
"""

from __future__ import annotations

import hmac

WEBHOOK_SECRET_HEADER = "x-docuseal-secret"


class DocuSealSignatureError(Exception):
    """Raised when a DocuSeal webhook fails shared-secret verification."""


def verify_docuseal_webhook(
    secret_header: str | None,
    secret: str,
    *,
    query_secret: str | None = None,
) -> None:
    """Verify the webhook's shared secret. Raises on any failure mode.

    Accepts the secret from **either** the ``X-Docuseal-Secret`` header or a
    ``?secret=`` query parameter (DocuSeal community edition does not support
    custom headers on webhook deliveries).  Fails closed: an unset server-side
    secret is a hard error (never default-accept).
    """
    if not secret:
        raise DocuSealSignatureError("webhook_secret_unset")
    presented = (secret_header or "").strip() or (query_secret or "").strip()
    if not presented:
        raise DocuSealSignatureError("missing_webhook_secret")
    if not hmac.compare_digest(presented.encode("utf-8"), secret.strip().encode("utf-8")):
        raise DocuSealSignatureError("webhook_secret_mismatch")
