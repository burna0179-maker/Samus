"""Email backend adapter — selects a provider by ``settings.email_backend``.

Architecture (Architecture_Samus.md): ``common/email_backend.py`` is the
selector; ``common/email_backends/{sendgrid,ses}.py`` are peer
implementations. All implementations expose
``send_email_via_<provider>(to, subject, body, *, html_body=None, ...)`` and
return ``{"message_id", "channel", "to", "ts"}``.

Both backends now live under ``common/email_backends/`` (``sendgrid``, ``ses``)
so the selector routes either without a workcell import. The outreach send path
may still call ``backend.outreach.ses_adapter`` directly; ``email_backends.ses``
is the canonical common-layer SES implementation.
"""

from __future__ import annotations

import logging
from typing import Any

from backend.common import compliance_guard
from backend.common.config import get_settings

from .email_backends import EmailBackendError
from .email_backends.sendgrid import (
    send_email_via_sendgrid,
)
from .email_backends.ses import (
    send_email_via_ses,
)


_LOG = logging.getLogger("samus.common.email_backend")


# Public error base — callers can ``except EmailBackendError`` to catch any
# provider-specific failure regardless of the selected backend, and may also
# ``raise EmailBackendError(...)`` themselves. Both SendGridAdapterError and
# SesAdapterError subclass it, so a single ``except`` clause covers every
# backend. (Re-exported from .email_backends; bound here for import stability.)
EmailBackendError = EmailBackendError


def _run_compliance_guard(
    *,
    to: str,
    subject: str,
    body: str,
    html_body: str | None,
    from_addr: str | None,
    reply_to: str | None,
    chosen_backend: str,
    message_kind: str,
) -> dict[str, str]:
    """Run the CAN-SPAM/FTC ComplianceGuard before a send.

    Returns the List-Unsubscribe headers to inject (possibly empty). Raises
    ``EmailBackendError`` when mode is ``enforce`` and the message fails a hard
    rule (or the guard itself errors — fail-closed). In ``audit`` mode it only
    logs and never blocks (fail-open on a guard error). When mode is ``off``
    this is never called.
    """
    mode = compliance_guard.get_mode()
    if mode == "off":
        return {}

    # The effective From is resolved inside the provider adapter from settings
    # when ``from_addr`` is None; resolve the same value here so the guard's
    # From-domain check sees the real sender.
    eff_from = from_addr
    if not eff_from:
        settings = get_settings()
        eff_from = (
            settings.sendgrid_from_email
            if chosen_backend == "sendgrid"
            else getattr(settings, "ses_from_email", "")
        )

    try:
        verdict = compliance_guard.evaluate(
            compliance_guard.ComplianceMessage(
                to=to,
                subject=subject,
                body=body,
                html_body=html_body,
                from_addr=eff_from,
                reply_to=reply_to,
                kind=compliance_guard.coerce_kind(message_kind),
            )
        )
    except Exception as exc:  # noqa: BLE001 — guard bug must not silently pass mail in enforce
        if mode == "enforce":
            raise EmailBackendError(f"compliance_guard_error: {exc}") from exc
        _LOG.warning("compliance guard errored (audit; sending anyway): %s", exc)
        return {}

    if not verdict.ok:
        if mode == "enforce":
            _LOG.info(
                "compliance BLOCK to=%s kind=%s reasons=%s",
                to,
                verdict.kind,
                list(verdict.reasons),
            )
            raise EmailBackendError("compliance_block: " + ", ".join(verdict.reasons))
        _LOG.warning(
            "compliance would-block (audit) to=%s kind=%s reasons=%s warnings=%s",
            to,
            verdict.kind,
            list(verdict.reasons),
            list(verdict.warnings),
        )
    elif verdict.warnings:
        _LOG.info("compliance warnings to=%s: %s", to, list(verdict.warnings))

    return dict(verdict.headers)


def send_email(
    to: str,
    subject: str,
    body: str,
    *,
    html_body: str | None = None,
    backend: str | None = None,
    from_addr: str | None = None,
    from_name: str | None = None,
    reply_to: str | None = None,
    api_key: str | None = None,
    attachments: list[dict[str, Any]] | None = None,
    headers: dict[str, str] | None = None,
    custom_args: dict[str, str] | None = None,
    message_kind: str = "commercial",
) -> dict[str, str]:
    """Dispatch to the configured email backend.

    ``backend`` overrides ``settings.email_backend`` when set. Returns the
    provider's send-receipt dict on success; raises ``EmailBackendError`` on
    provider failure or ``NotImplementedError`` / ``ValueError`` on a bad
    backend selection.

    ``reply_to`` sets the Reply-To header on the outbound message (passed
    through to the underlying provider).

    ``attachments`` is an optional list of dicts shaped
    ``{"filename": str, "content": bytes, "mime_type": str}`` — passed through
    to the underlying provider for inline attachment.

    ``headers`` is an optional dict of extra message headers (e.g.
    ``List-Unsubscribe``) merged with any the ComplianceGuard injects; the
    guard's headers win on a key clash. Honored by the SendGrid backend; the
    SES simple-send API cannot set custom headers and ignores them.

    ``message_kind`` is ``"commercial"`` (default) or ``"transactional"``.
    Commercial mail is held to the full CAN-SPAM structural ruleset by the
    ComplianceGuard (unsubscribe + postal address); transactional / relationship
    mail (receipts, onboarding acks, fulfillment deliverables) is exempt from
    those two but still suppression-checked. The default is the strict kind so
    a forgotten tag fails safe. See ``backend.common.compliance_guard``.

    ``custom_args`` is an optional string->string map echoed back on every
    SendGrid event-webhook event (the Heat Field / attribution correlation key,
    e.g. ``{"prospect_id": ..., "template_id": ...}``). Honored by the SendGrid
    backend; ignored by SES.
    """
    chosen = (backend if backend is not None else get_settings().email_backend) or "sendgrid"
    chosen = chosen.strip().lower()

    # ComplianceGuard (CAN-SPAM/FTC) — dormant unless SAMUS_COMPLIANCE_GUARD_MODE
    # is audit/enforce. Returns List-Unsubscribe headers to inject; raises in
    # enforce mode on a hard violation. Guard headers win over caller headers.
    guard_headers = _run_compliance_guard(
        to=to,
        subject=subject,
        body=body,
        html_body=html_body,
        from_addr=from_addr,
        reply_to=reply_to,
        chosen_backend=chosen,
        message_kind=message_kind,
    )
    merged_headers = {**(headers or {}), **guard_headers} or None

    if chosen == "sendgrid":
        return send_email_via_sendgrid(
            to=to,
            subject=subject,
            body=body,
            html_body=html_body,
            from_addr=from_addr,
            from_name=from_name,
            reply_to=reply_to,
            api_key=api_key,
            attachments=attachments,
            headers=merged_headers,
            custom_args=custom_args,
        )

    if chosen == "ses":
        return send_email_via_ses(
            to=to,
            subject=subject,
            body=body,
            html_body=html_body,
            from_addr=from_addr,
            from_name=from_name,
            reply_to=reply_to,
            api_key=api_key,
            attachments=attachments,
            headers=merged_headers,
            custom_args=custom_args,
        )

    raise ValueError(f"unknown email_backend: {chosen!r}")


__all__ = ["EmailBackendError", "send_email"]
