"""Payment-receipt template + send glue for the Stripe webhook handler.

Renders a plain-text + HTML receipt for a checkout.session.completed event
and dispatches via the common email backend selector
(``backend/common/email_backend.send_email``). The send is best-effort:
``send_payment_receipt`` swallows backend failures and returns a result dict
so the webhook handler can record the outcome on ``WebhookEventRecord``
without failing the webhook — a 5xx back to Stripe would cause retries on
an event that successfully advanced the customer's state.

Idempotency note: Stripe retries are caught upstream by the event-log
idempotency check (``_load_seen_event_ids`` in ``webhook.py``), so the
receipt-send fires at most once per Stripe event_id.
"""

from __future__ import annotations

import logging
from typing import Any

from backend.common.email_backend import EmailBackendError, send_email


_LOG = logging.getLogger("samus.finance.receipts")

# Known acronyms to keep uppercase when title-casing an offer slug.
_ACRONYMS = frozenset({"SEO", "API", "CRM", "AI", "ML", "SMS"})


def _offer_display_name(offer_code: str) -> str:
    """Turn ``"seo_audit"`` into ``"SEO Audit"``; ``""`` into ``"your purchase"``.

    No product catalog yet — title-case the slug with a small acronym pass.
    A future PR can swap this for a real catalog lookup.
    """
    if not (offer_code and offer_code.strip()):
        return "your purchase"
    words = offer_code.replace("-", "_").split("_")
    return " ".join(w.upper() if w.upper() in _ACRONYMS else w.title() for w in words if w)


def _format_amount(amount_total_usd: float | None, currency: str) -> str:
    if amount_total_usd is None:
        return "(amount unavailable)"
    return f"${amount_total_usd:,.2f} {currency.upper()}"


def render_payment_receipt(
    *,
    amount_total_usd: float | None,
    currency: str,
    hf_offer_code: str,
    event_id: str,
    received_at: str,
) -> tuple[str, str, str]:
    """Return ``(subject, text_body, html_body)`` for a payment-confirmation email.

    All inputs are operator-facing values pulled from the Stripe event by
    ``webhook._extract_session_fields``. Safe with empty / missing fields:
    offer code falls back to ``"your purchase"``, amount falls back to
    ``"(amount unavailable)"``.
    """
    offer_name = _offer_display_name(hf_offer_code)
    amount_str = _format_amount(amount_total_usd, currency)
    offer_code_paren = f" ({hf_offer_code})" if hf_offer_code else ""

    subject = f"Receipt for {offer_name} — {amount_str}"

    text_body = (
        "Hi,\n"
        "\n"
        "Thanks for your purchase. This email confirms your payment to HustleForge:\n"
        "\n"
        f"  Offer:    {offer_name}{offer_code_paren}\n"
        f"  Amount:   {amount_str}\n"
        f"  Date:     {received_at}\n"
        f"  Event ID: {event_id}\n"
        "\n"
        "Your fulfillment team has been notified and will be in touch within "
        "24 hours to begin the work.\n"
        "\n"
        "Questions? Reply to this email.\n"
        "\n"
        "— HustleForge\n"
    )

    offer_code_html = f" <code>({hf_offer_code})</code>" if hf_offer_code else ""
    html_body = (
        "<p>Hi,</p>"
        "<p>Thanks for your purchase. This email confirms your payment to HustleForge:</p>"
        '<table style="border-collapse:collapse">'
        f'<tr><td style="padding:4px 12px 4px 0"><strong>Offer:</strong></td>'
        f"<td>{offer_name}{offer_code_html}</td></tr>"
        f'<tr><td style="padding:4px 12px 4px 0"><strong>Amount:</strong></td>'
        f"<td>{amount_str}</td></tr>"
        f'<tr><td style="padding:4px 12px 4px 0"><strong>Date:</strong></td>'
        f"<td>{received_at}</td></tr>"
        f'<tr><td style="padding:4px 12px 4px 0"><strong>Event ID:</strong></td>'
        f"<td><code>{event_id}</code></td></tr>"
        "</table>"
        "<p>Your fulfillment team has been notified and will be in touch within "
        "24 hours to begin the work.</p>"
        "<p>Questions? Reply to this email.</p>"
        "<p>— HustleForge</p>"
    )

    return subject, text_body, html_body


def send_payment_receipt(
    *,
    customer_email: str,
    amount_total_usd: float | None,
    currency: str,
    hf_offer_code: str,
    event_id: str,
    received_at: str,
) -> dict[str, Any]:
    """Render + send the payment-receipt email. Never raises.

    Returns ``{"sent": bool, "message_id": str, "error": str}``. The webhook
    handler maps this onto the matching ``WebhookEventRecord`` fields
    (``receipt_sent``, ``receipt_message_id``, ``receipt_error``).

    Failure modes captured (never re-raised):
      - Missing customer_email -> ``error="no_customer_email"``
      - SendGrid HTTP / transport failure -> ``error=<adapter message>``
      - Misconfigured backend (no API key, bad selector) -> ``error=<reason>``
    """
    email = (customer_email or "").strip()
    if not email:
        return {"sent": False, "message_id": "", "error": "no_customer_email"}

    subject, text_body, html_body = render_payment_receipt(
        amount_total_usd=amount_total_usd,
        currency=currency,
        hf_offer_code=hf_offer_code,
        event_id=event_id,
        received_at=received_at,
    )

    try:
        result = send_email(
            to=email,
            subject=subject,
            body=text_body,
            html_body=html_body,
            message_kind="transactional",  # payment receipt — CAN-SPAM transactional
        )
    except (EmailBackendError, ValueError, NotImplementedError) as exc:
        _LOG.warning(
            "payment receipt send failed event=%s email=%s: %s",
            event_id,
            email,
            exc,
        )
        return {"sent": False, "message_id": "", "error": str(exc)[:200]}

    return {
        "sent": True,
        "message_id": str(result.get("message_id", "")),
        "error": "",
    }


__all__ = ["render_payment_receipt", "send_payment_receipt"]
