"""AWS SES email backend (lifted into ``common/email_backends/``).

This is the ``common``-layer SES backend the selector in
``common/email_backend.py`` dispatches to. It depends only on ``common`` (no
workcell import), so it resolves the layering violation that previously forced
``send_email`` to raise ``NotImplementedError`` for the ``"ses"`` backend.

The working outreach send path may still call ``backend.outreach.ses_adapter``
directly; this module is the canonical SES implementation behind the selector
and exposes the same ``send_email_via_<provider>`` contract + receipt dict as
``sendgrid``.

Fail-closed: ``boto3``/``botocore`` are imported lazily so that selecting a
different backend (or importing this module on a box without the AWS SDK) never
hard-fails at import time; a missing SDK surfaces as ``SesAdapterError`` only
when an SES send is actually attempted. A missing ``from`` address raises
``ValueError`` (bad selection / unconfigured sender) rather than silently
no-op'ing.
"""
from __future__ import annotations

import logging
from typing import Any

from backend.common.config import get_settings
from backend.common.dates import iso_now

_LOG = logging.getLogger("samus.common.email_backends.ses")


from . import EmailBackendError


class SesAdapterError(EmailBackendError):
    """Raised when the SES API call fails (wraps botocore ClientError) or the
    AWS SDK is unavailable when an SES send is attempted."""


def send_email_via_ses(
    to: str,
    subject: str,
    body: str,
    *,
    html_body: str | None = None,
    from_addr: str | None = None,
    from_name: str | None = None,
    reply_to: str | None = None,
    aws_region: str | None = None,
    # Accepted for selector-interface parity; SES SDK send below doesn't take
    # an api_key or attachments here. Attachments would need a raw MIME send
    # (send_raw_email) — out of scope for this lift; passing them raises.
    api_key: str | None = None,
    attachments: list[dict[str, Any]] | None = None,
    # Custom message headers (e.g. List-Unsubscribe) — accepted for selector
    # parity but IGNORED: the SES simple ``send_email`` API cannot set arbitrary
    # message headers (only ``send_raw_email`` with raw MIME can). SendGrid is
    # the production backend where these headers land; this is logged, not
    # raised, so a header-bearing send still succeeds via SES sans headers.
    headers: dict[str, str] | None = None,
    # custom_args (SendGrid event-correlation key) — accepted for selector
    # parity but inapplicable to SES; ignored.
    custom_args: dict[str, str] | None = None,
) -> dict[str, str]:
    """Send an email via AWS SES. Returns the standard receipt dict.

    Required: ``to``, ``subject``, ``body`` (plain text). Optional ``html_body``
    is sent as the HTML part. ``from_addr`` falls back to
    ``settings.ses_from_email``; an empty resolved sender raises ``ValueError``
    (fail-closed on an unconfigured sender, never a silent no-op).
    """
    if not (to and to.strip()):
        raise ValueError("send_email_via_ses requires 'to'")
    if not (subject and subject.strip()):
        raise ValueError("send_email_via_ses requires 'subject'")
    if not (body and body.strip()):
        raise ValueError("send_email_via_ses requires 'body'")
    if attachments:
        raise ValueError(
            "send_email_via_ses does not support attachments (would require a "
            "raw-MIME send); use the sendgrid backend for attachments"
        )
    if headers:
        # Simple SES send cannot carry custom headers; surface it so a
        # List-Unsubscribe expectation on the SES path is visible, not silent.
        _LOG.debug(
            "ses backend ignores custom message headers (simple API): %s",
            list(headers),
        )

    settings = get_settings()
    source = from_addr if from_addr is not None else getattr(settings, "ses_from_email", "")
    source = (source or "").strip()
    if not source:
        raise ValueError(
            "send_email_via_ses requires from_addr (arg or settings.ses_from_email)"
        )
    if from_name and from_name.strip():
        # RFC 5322 display-name form: "Name <addr>".
        source = f"{from_name.strip()} <{source}>"

    region = aws_region or getattr(settings, "aws_region", None)

    try:
        import boto3
        from botocore.exceptions import ClientError
    except Exception as exc:  # noqa: BLE001 — SDK missing surfaces as adapter error
        raise SesAdapterError(f"aws_sdk_unavailable: {exc}") from exc

    client: Any = boto3.client("ses", region_name=region)

    message_body: dict[str, Any] = {"Text": {"Data": body}}
    if html_body and html_body.strip():
        message_body["Html"] = {"Data": html_body}
    message: dict[str, Any] = {
        "Subject": {"Data": subject},
        "Body": message_body,
    }
    kwargs: dict[str, Any] = {
        "Source": source,
        "Destination": {"ToAddresses": [to]},
        "Message": message,
    }
    if reply_to and reply_to.strip():
        kwargs["ReplyToAddresses"] = [reply_to.strip()]

    try:
        resp = client.send_email(**kwargs)
    except ClientError as exc:
        _LOG.warning("SES send_email failed: %s", exc)
        raise SesAdapterError(str(exc)) from exc

    return {
        "message_id": str(resp.get("MessageId", "")),
        "channel": "email",
        "to": to,
        "ts": iso_now(),
    }


__all__ = ["SesAdapterError", "send_email_via_ses"]
