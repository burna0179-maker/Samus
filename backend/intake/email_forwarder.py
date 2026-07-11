"""Post-drain categorized forwarder — the "clean the inbox" seam.

After :func:`backend.intake.gmail_poller.handle_parsed_email` has written a
durable CRM record for one processed message (artifact + operator task +
optional business event), this module:

1. Composes a NEW email FROM ``samushustleforge@gmail.com`` TO the
   operator's personal address (``SAMUS_FORWARD_TO_EMAIL`` — the operator
   set this to the operator address).
2. The forwarded subject carries a categorization prefix so the operator's
   personal inbox naturally organizes:

     [CLIENT/COUNTER]      Kerry Brown re: proposal
     [CS/URGENT]           Kerry re: outage on landing page
     [BILL/ANTHROPIC]      Your invoice for $19.99
     [SOCIAL/LINKEDIN]     Zach W. viewed your profile
     [URGENT/UNCLASSIFIED] From an unknown sender

3. TRASHes the original in ``samushustleforge@gmail.com`` — the CRM
   record is the durable copy; the Gmail message is redundant now.

TRAINING SIGNAL

Anything Samus couldn't confidently classify (``category == "other"``, or
absent classification, or intent confidence < the threshold) lands under
``[URGENT/UNCLASSIFIED]``. Over time the operator eyeballs those, and the
tags they add manually (or feedback the operator returns via a future
seam) will let the classifier's heuristics + intent vocabulary grow to
absorb the previously-urgent categories.

FAIL-SOFT

Every stage is best-effort — a send failure logs a warning and skips the
trash; the CRM record already exists as the source of truth. The
forwarder MUST never break the drain: any exception in this module is
caught upstream in the poller loop.
"""
from __future__ import annotations

import base64
import logging
import os
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Any

from backend.intake.gmail_poller import ParsedInboundEmail

_LOG = logging.getLogger("samus.intake.email_forwarder")

# --- config helpers --------------------------------------------------------

_ENV_ENABLED = "SAMUS_FORWARD_ENABLED"
_ENV_TARGET = "SAMUS_FORWARD_TO_EMAIL"
_ENV_TRASH = "SAMUS_FORWARD_TRASH_ORIGINAL"
_ENV_MIN_CONFIDENCE = "SAMUS_FORWARD_CLASSIFY_MIN_CONFIDENCE"


def _flag_on(name: str, default_on: bool = True) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default_on
    return raw not in ("0", "false", "no", "off")


def _target_email() -> str:
    return (os.environ.get(_ENV_TARGET) or "").strip()


def _from_email() -> str:
    # The polled inbox is the natural sender identity — replies land in
    # the same mailbox the drain reads next, and the operator's personal
    # inbox has a stable From: to filter on if they want a Gmail rule.
    return (os.environ.get("SAMUS_GMAIL_INBOX_EMAIL") or "").strip()


def _min_confidence() -> float:
    raw = (os.environ.get(_ENV_MIN_CONFIDENCE) or "").strip()
    if not raw:
        return 0.4
    try:
        return max(0.0, min(1.0, float(raw)))
    except ValueError:
        return 0.4


def is_configured() -> bool:
    return bool(_target_email() and _from_email() and _flag_on(_ENV_ENABLED))


# --- categorization -> subject prefix -------------------------------------

@dataclass(frozen=True)
class ForwardCategory:
    """Resolved bucket for one processed message."""

    prefix: str          # e.g. "[CLIENT/COUNTER]" or "[URGENT/UNCLASSIFIED]"
    is_urgent: bool      # true for the training queue


_CATEGORY_LABELS: dict[str, str] = {
    "bill":                   "BILL",
    "account":                "ACCOUNT",
    "calendar":               "CALENDAR",
    "developer":              "DEVELOPER",
    "social":                 "SOCIAL",
    "business":               "BUSINESS",
    "marketing":              "MARKETING",
    "client_correspondence":  "CLIENT",
    "other":                  "UNCLASSIFIED",
}


def choose_category(
    classification: dict[str, Any] | None,
    intent: dict[str, Any] | None,
    intent_action_prefix: str | None = None,
) -> ForwardCategory:
    """Pick the forwarding bucket based on classifier + intent output.

    Precedence:
      1. If the intent router already produced a ``[CS/...]`` or
         ``[CLIENT/...]`` prefix, reuse it (source of truth for client
         mail — carries the routed intent + urgency).
      2. Category=``other`` or missing classification or confidence
         below the min = ``[URGENT/UNCLASSIFIED]`` (training queue).
      3. Otherwise a per-category label; for ``bill``, append the vendor
         short code so operator's rules can filter (e.g. ``[BILL/ANTHROPIC]``).
    """
    if intent_action_prefix:
        return ForwardCategory(
            prefix=intent_action_prefix,
            is_urgent=intent_action_prefix.startswith("[CS/"),
        )

    cls = classification or {}
    category = str(cls.get("category") or "").strip().lower()
    confidence = 0.0
    try:
        confidence = float(cls.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0

    if not category or category == "other" or confidence < _min_confidence():
        return ForwardCategory(prefix="[URGENT/UNCLASSIFIED]", is_urgent=True)

    label = _CATEGORY_LABELS.get(category, category.upper())
    detail = ""
    if category == "bill":
        vendor = str(cls.get("vendor_registry_id") or "").strip().upper()
        if vendor:
            detail = f"/{vendor}"
        elif cls.get("bill_signal_kind") == "payment_declined":
            detail = "/DECLINED"
    return ForwardCategory(prefix=f"[{label}{detail}]", is_urgent=False)


# --- MIME building ---------------------------------------------------------

_URGENCY_FROM_PREFIX: dict[str, str] = {
    "[CS/ESCALATION]":   "URGENT",
    "[CS/SERVICE]":      "URGENT",
    "[CLIENT/AGREED]":   "HIGH",
    "[CLIENT/MEETING]":  "HIGH",
    "[CLIENT/COUNTER]":  "NORMAL",
    "[CLIENT/OBJECTION]": "NORMAL",
    "[CLIENT/HESITATION]": "NORMAL",
    "[CLIENT/INFO]":     "NORMAL",
    "[CLIENT/QUESTION]": "NORMAL",
    "[CLIENT/ACK]":      "LOW",
    "[CLIENT/CLOSED]":   "LOW",
}


def _urgency_for(prefix: str) -> str:
    return _URGENCY_FROM_PREFIX.get(prefix, "NORMAL")


def _clean_body_for_forward(body_text: str) -> str:
    """Strip HTML from the forwarded body so it reads cleanly in plain-text.

    Titan / Gmail-HTML / rich-client bodies would otherwise arrive at
    ``the operator`` as a wall of ``<div>`` tags and inline styles. The
    stripped form is the same content the classifier + intent reasoner
    saw — no information lost from the operator's perspective.
    """
    from backend.intake.forwarded_email import _looks_like_html, strip_html
    if not body_text:
        return "(no body)"
    if _looks_like_html(body_text):
        return strip_html(body_text)
    return body_text


def _client_expectation_block(
    *,
    classification: dict[str, Any],
    intent: dict[str, Any],
    category: ForwardCategory,
) -> list[str]:
    """Build the top-of-body 'What the client expects' block.

    Only rendered for inbound client_correspondence — outbound archives
    don't need this (the operator already knows what they sent), and
    non-client mail doesn't have a client role to summarize.
    """
    client_id = str(classification.get("client_id") or "")
    role = str(classification.get("client_role") or "").replace("_", " ").title()
    display_name = ""
    # display_name usually comes from the client_directory record; not
    # always plumbed through the classification dict, so fall back to
    # the humanized client_id.
    if client_id:
        display_name = client_id.replace("_", " ").title()

    intent_tag = str(intent.get("intent") or "").strip()
    sentiment = str(intent.get("sentiment") or "").strip()
    requested = str(intent.get("requested_action") or "").strip()
    summary = str(intent.get("summary_sentence") or "").strip()

    urgency = _urgency_for(category.prefix)

    lines: list[str] = []
    hr = "=" * 70
    lines.append(hr)
    lines.append(f"CLIENT   : {display_name}" + (f" ({role})" if role else ""))
    if intent_tag:
        lines.append(f"INTENT   : {intent_tag}")
    lines.append(f"URGENCY  : {urgency}")
    if sentiment:
        lines.append(f"SENTIMENT: {sentiment}")
    lines.append("")
    if requested:
        lines.append("WHAT THE CLIENT EXPECTS:")
        lines.append(f"  {requested}")
        lines.append("")
    if summary:
        lines.append("SAMUS SUMMARY:")
        lines.append(f"  {summary}")
        lines.append("")
    lines.append(hr)
    lines.append("")
    return lines


def build_forward_mime(
    *,
    from_email: str,
    to_email: str,
    parsed: ParsedInboundEmail,
    category: ForwardCategory,
    intent_summary: str = "",
    classification: dict[str, Any] | None = None,
    intent: dict[str, Any] | None = None,
) -> bytes:
    """Compose a forward MIME whose SUBJECT is prefix + original subject.

    For inbound ``client_correspondence`` mail, prepends a prominent
    "CLIENT EXPECTATIONS" block that surfaces role, intent, urgency,
    what the client wants (requested_action), and Samus's summary —
    so the operator sees what the client wants at a glance in
    ``the operator`` without opening the message. Other categories keep
    the shorter classic header.
    """
    msg = EmailMessage()
    msg["From"] = from_email
    msg["To"] = to_email
    original_subject = (parsed.subject or "(no subject)").strip()
    if len(original_subject) > 180:
        original_subject = original_subject[:180] + "..."
    msg["Subject"] = f"{category.prefix} {original_subject}"

    cls = classification or {}
    is_inbound_client = (
        cls.get("category") == "client_correspondence"
        and cls.get("direction", "inbound") == "inbound"
    )

    lines: list[str] = []

    if is_inbound_client and intent:
        # Rich client-expectation block.
        lines.extend(
            _client_expectation_block(
                classification=cls, intent=intent, category=category,
            )
        )
    else:
        if intent_summary:
            lines.append(f"Samus intent: {intent_summary}")
        lines.append(f"Category: {category.prefix}")
        lines.append("")

    lines.append("---------- Forwarded message ---------")
    lines.append(f"From: {parsed.from_display or parsed.from_addr}")
    if parsed.date_header:
        lines.append(f"Date: {parsed.date_header}")
    if parsed.subject:
        lines.append(f"Subject: {parsed.subject}")
    if parsed.to_addrs:
        lines.append(f"To: {', '.join(parsed.to_addrs)}")
    lines.append("")
    lines.append(_clean_body_for_forward(parsed.body_text))

    msg.set_content("\n".join(lines))
    return msg.as_bytes()


# --- orchestration ---------------------------------------------------------

@dataclass
class ForwardResult:
    forwarded: bool = False
    trashed: bool = False
    forward_msg_id: str = ""
    category_prefix: str = ""
    is_urgent: bool = False
    error: str = ""


def forward_and_cleanup(
    *,
    gmail_client,
    original_gmail_id: str,
    parsed: ParsedInboundEmail,
    classification: dict[str, Any] | None,
    intent: dict[str, Any] | None = None,
    intent_action_prefix: str | None = None,
) -> ForwardResult:
    """Compose + send the categorized forward, then trash the original.

    ``gmail_client`` is the same :class:`GmailApiClient` the poller opened
    for this drain pass — same auth, same context manager scope.

    Returns a :class:`ForwardResult` describing what happened. Never
    raises: any failure is captured in ``error`` and the drain continues.

    OUTBOUND CLIENT MAIL SHORT-CIRCUIT

    When the classification is ``client_correspondence`` with
    ``direction == "outbound"``, the email is an operator-forwarded
    archive of a message the operator ALREADY has in their sent folder.
    Re-forwarding it to the operator would just create inbox noise, so
    the forwarder skips send + trash for these — the CRM artifact is
    the archival record; the Gmail copy can stay in the polled inbox
    (or the operator can clean up manually if desired).
    """
    result = ForwardResult()
    if not is_configured():
        result.error = "not_configured"
        return result

    # SHORT-CIRCUIT: outbound client mail (Alex's own archives) skips the
    # the operator forward. Alex already has these in his sent folder.
    cls = classification or {}
    if (
        cls.get("category") == "client_correspondence"
        and cls.get("direction") == "outbound"
    ):
        result.error = "skipped_outbound_client_archive"
        return result

    from_email = _from_email()
    to_email = _target_email()

    try:
        category = choose_category(
            classification=classification,
            intent=intent,
            intent_action_prefix=intent_action_prefix,
        )
        result.category_prefix = category.prefix
        result.is_urgent = category.is_urgent

        intent_summary = ""
        if intent and isinstance(intent, dict):
            summary = str(intent.get("summary_sentence") or "").strip()
            tag = str(intent.get("intent") or "").strip()
            if summary and tag:
                intent_summary = f"{tag} -- {summary}"
            elif summary:
                intent_summary = summary
            elif tag:
                intent_summary = tag

        mime_bytes = build_forward_mime(
            from_email=from_email,
            to_email=to_email,
            parsed=parsed,
            category=category,
            intent_summary=intent_summary,
            classification=classification,
            intent=intent,
        )
    except Exception as exc:  # noqa: BLE001 — never break the drain
        _LOG.warning("forward: compose failed for %s: %s", original_gmail_id, exc)
        result.error = f"compose_failed: {exc}"
        return result

    try:
        sent_id = gmail_client.send_raw(mime_bytes)
        result.forward_msg_id = sent_id
        result.forwarded = True
    except Exception as exc:  # noqa: BLE001
        _LOG.warning(
            "forward: send failed for %s (prefix=%s): %s",
            original_gmail_id, category.prefix, exc,
        )
        result.error = f"send_failed: {exc}"
        return result

    if _flag_on(_ENV_TRASH, default_on=True):
        try:
            gmail_client.trash(original_gmail_id)
            result.trashed = True
        except Exception as exc:  # noqa: BLE001
            _LOG.warning(
                "forward: trash failed for %s (send OK): %s",
                original_gmail_id, exc,
            )
            # Not fatal — CRM record + forward already succeeded.

    _LOG.info(
        "forward: %s -> %s prefix=%s trashed=%s",
        original_gmail_id, sent_id, category.prefix, result.trashed,
    )
    return result


__all__ = [
    "ForwardCategory",
    "ForwardResult",
    "build_forward_mime",
    "choose_category",
    "forward_and_cleanup",
    "is_configured",
]
