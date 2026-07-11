"""SendGrid Mail Send v3 backend.

Thin httpx wrapper around ``POST /v3/mail/send``. No ``sendgrid`` SDK — same
rationale as ``finance/stripe_client.py``: the SDK adds dependency weight we
don't need for a one-endpoint surface, and we already handle retries / timeout
at the workcell layer.

Returns the same shape as ``outreach/ses_adapter.py``'s ``send_email_via_ses``
so the selector in ``common/email_backend.py`` can dispatch interchangeably.

Attachments: pass a list of dicts each shaped
``{"filename": str, "content": bytes, "mime_type": str}`` (mime_type defaults
to ``application/octet-stream``). Adapter base64-encodes the bytes into
SendGrid's expected ``attachments`` envelope.

EU subusers: set ``SENDGRID_BASE_URL=https://api.eu.sendgrid.com``.
"""
from __future__ import annotations

import base64
import logging
from typing import Any

import httpx

from backend.common.config import get_settings
from backend.common.dates import iso_now


_LOG = logging.getLogger("samus.common.email_backends.sendgrid")

_SEND_PATH = "/v3/mail/send"
_HTTP_TIMEOUT = 15.0


from . import EmailBackendError


class SendGridAdapterError(EmailBackendError):
    """Raised when SendGrid returns a non-2xx response or the network fails."""


def _build_attachment_envelope(att: dict[str, Any]) -> dict[str, str]:
    """Convert a caller's attachment dict into SendGrid's v3 envelope.

    Required keys: ``filename`` (str), ``content`` (bytes).
    Optional: ``mime_type`` (str; default ``application/octet-stream``),
    ``disposition`` (str; default ``attachment``), ``content_id`` (str).
    """
    filename = att.get("filename")
    content = att.get("content")
    if not isinstance(filename, str) or not filename:
        raise ValueError("attachment requires non-empty 'filename'")
    if not isinstance(content, (bytes, bytearray)):
        raise ValueError("attachment 'content' must be bytes")
    mime = att.get("mime_type") or "application/octet-stream"
    disposition = att.get("disposition") or "attachment"

    envelope: dict[str, str] = {
        "content": base64.b64encode(bytes(content)).decode("ascii"),
        "filename": filename,
        "type": mime,
        "disposition": disposition,
    }
    content_id = att.get("content_id")
    if isinstance(content_id, str) and content_id:
        envelope["content_id"] = content_id
    return envelope


def _resolve(value: str | None, fallback: str, *, name: str, required: bool = True) -> str:
    """Pick ``value`` if non-empty, else ``fallback``. Raise ValueError if
    ``required`` and both are empty.
    """
    chosen = (value if value is not None else fallback) or ""
    chosen = chosen.strip()
    if not chosen and required:
        raise ValueError(f"send_email_via_sendgrid requires {name}")
    return chosen


def send_email_via_sendgrid(
    to: str,
    subject: str,
    body: str,
    *,
    html_body: str | None = None,
    from_addr: str | None = None,
    from_name: str | None = None,
    reply_to: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
    attachments: list[dict[str, Any]] | None = None,
    headers: dict[str, str] | None = None,
    custom_args: dict[str, str] | None = None,
    timeout: float = _HTTP_TIMEOUT,
) -> dict[str, str]:
    """Send one email via SendGrid Mail Send v3.

    Required: ``to``, ``subject``, ``body`` (plain text). Optional ``html_body``
    is included as a second content entry — SendGrid's convention is that the
    LAST content entry is the preferred rendering, so plain-text first then
    HTML matches typical mail-client behavior.

    ``reply_to`` sets the Reply-To header. An empty string or None means no
    header is added (mail clients fall back to From). The value is also
    resolved from ``settings.sendgrid_reply_to`` when the argument is None.

    ``attachments`` is an optional list of dicts shaped
    ``{"filename": str, "content": bytes, "mime_type": str}`` — bytes are
    base64-encoded into SendGrid's ``attachments`` envelope. See
    ``_build_attachment_envelope`` for the full shape.

    Defaults are pulled from settings: ``sendgrid_api_key``,
    ``sendgrid_from_email``, ``sendgrid_from_name``, ``sendgrid_base_url``.
    """
    if not (to and to.strip()):
        raise ValueError("send_email_via_sendgrid requires 'to'")
    if not (subject and subject.strip()):
        raise ValueError("send_email_via_sendgrid requires 'subject'")
    if not (body and body.strip()):
        raise ValueError("send_email_via_sendgrid requires 'body'")

    settings = get_settings()
    resolved_key = _resolve(api_key, settings.sendgrid_api_key, name="api_key (arg or SENDGRID_API_KEY)")
    resolved_from = _resolve(from_addr, settings.sendgrid_from_email, name="from_addr (arg or SENDGRID_FROM_EMAIL)")
    resolved_name = _resolve(from_name, settings.sendgrid_from_name, name="from_name", required=False)
    # Reply-To: empty string means no header (mail clients fall back to From).
    # required=False allows None / empty without raising.
    resolved_reply_to = _resolve(reply_to, settings.sendgrid_reply_to, name="reply_to", required=False)
    resolved_base = (base_url if base_url is not None else settings.sendgrid_base_url) or "https://api.sendgrid.com"
    resolved_base = resolved_base.rstrip("/")

    content: list[dict[str, str]] = [{"type": "text/plain", "value": body}]
    if html_body and html_body.strip():
        content.append({"type": "text/html", "value": html_body})

    from_obj: dict[str, str] = {"email": resolved_from}
    if resolved_name:
        from_obj["name"] = resolved_name

    payload: dict[str, Any] = {
        "personalizations": [{"to": [{"email": to}]}],
        "from": from_obj,
        "subject": subject,
        "content": content,
    }
    if resolved_reply_to:
        payload["reply_to"] = {"email": resolved_reply_to}
    if attachments:
        payload["attachments"] = [
            _build_attachment_envelope(a) for a in attachments
        ]
    # Custom message headers (e.g. List-Unsubscribe / List-Unsubscribe-Post
    # from the ComplianceGuard). SendGrid v3 ``headers`` is a top-level
    # string->string map applied to the message. Skipped when empty.
    if headers:
        payload["headers"] = {
            str(k): str(v) for k, v in headers.items() if k
        }
    # custom_args are echoed back verbatim on every Event Webhook event — the
    # Heat Field / attribution correlation key (sg_message_id -> deal). Values
    # must be strings. Skipped when empty.
    if custom_args:
        payload["custom_args"] = {
            str(k): str(v) for k, v in custom_args.items() if k
        }
    request_headers = {
        "Authorization": f"Bearer {resolved_key}",
        "Content-Type": "application/json",
    }

    url = f"{resolved_base}{_SEND_PATH}"
    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.post(url, headers=request_headers, json=payload)
    except httpx.HTTPError as exc:
        raise SendGridAdapterError(f"sendgrid_transport_error: {exc}") from exc

    if response.status_code >= 400:
        message = ""
        try:
            errors = (response.json() or {}).get("errors") or []
            if errors and isinstance(errors, list):
                message = "; ".join(
                    str(e.get("message") or "") for e in errors if isinstance(e, dict)
                ).strip("; ")
        except ValueError:
            pass
        if not message:
            message = response.text[:200]
        raise SendGridAdapterError(
            f"sendgrid_http_{response.status_code}: {message}"
        )

    # 202 Accepted is the documented success — accept any 2xx defensively.
    # X-Message-Id (per docs); some accounts emit lowercase, httpx headers are
    # case-insensitive but we read both for clarity.
    message_id = (
        response.headers.get("X-Message-Id")
        or response.headers.get("x-message-id")
        or ""
    )
    return {
        "message_id": message_id,
        "channel": "email",
        "to": to,
        "ts": iso_now(),
    }


__all__ = ["SendGridAdapterError", "send_email_via_sendgrid"]
