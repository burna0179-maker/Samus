"""Gmail API poller — converts inbound customer email into CRM artifacts + tasks.

The Samus outbound From (``ahartman@hustleforge.tech``) is forwarded to a
Gmail mailbox (``samushustleforge@gmail.com`` per the config comment); this
poller is what turns each *unread* message in that mailbox into:

  1. A CRM ``Artifact`` of kind ``inbound_email`` storing the raw parsed
     message (headers, text body, attachment names).
  2. A CRM ``OperatorTask`` of kind ``reply_email`` referencing that
     artifact, with the per-customer billing summary inlined in the
     description so the operator opens the task already knowing whether
     the sender is paid / subscribed / unknown.
  3. (Best-effort) attaches both to the most-recent open opportunity for
     the sender's email via :func:`find_opportunity_for_email`.

Idempotency: each processed Message-ID is appended to a JSONL ledger and
the Gmail message has its UNREAD label removed. Either alone is sufficient;
both together survive a ledger-file deletion (Gmail flag remembers) and
a Gmail state reset (ledger remembers).

Failure model: this is a *script-driven drain* — invoked by
``scripts/Poll-Inbox.ps1`` on a Task Scheduler cadence. We process
messages one at a time, never raise out of the per-message handler, and
bail the outer loop only on connection-level errors. A single bad
message is logged + skipped, the rest of the batch still flows through.

Transport: Gmail REST API over OAuth 2.0 / TCP 443. The earlier IMAP
implementation was abandoned after host-dev egress on TCP 993 turned out
to be blocked by an upstream firewall; the API is also the long-term-
correct choice (label-aware, rate-limited by Google, no app password).
See :mod:`backend.intake.gmail_api_client` for the transport surface and
:mod:`backend.intake.gmail_oauth` for the one-time consent flow.
"""

from __future__ import annotations

import email
import email.policy
import json
import logging
import os
from dataclasses import dataclass, field
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from backend.common import persistence
from backend.common.config import get_settings
from backend.common.dates import iso_now

from .gmail_api_client import GmailApiClient, GmailApiError


_LOG = logging.getLogger("samus.intake.gmail_poller")


# ---------------------------------------------------------------------------
# Parsed-email shape (in-memory; not persisted as-is)
# ---------------------------------------------------------------------------


@dataclass
class ParsedInboundEmail:
    """One inbound email after IMAP fetch + RFC822 parse.

    All fields are normalized to strings safe for JSON serialization.
    ``body_text`` is the best-available plain-text body; multipart/alternative
    messages prefer text/plain over text/html (HTML is downgraded to a note
    on the artifact rather than stripped on the fly).
    """

    message_id: str  # RFC822 Message-ID header (idempotency key)
    from_addr: str  # sender email (lower-cased, stripped of name)
    from_display: str  # raw "Name <a@b>" header for the artifact
    to_addrs: list[str]  # all recipients (To + Cc), lower-cased
    subject: str
    date_header: str  # raw Date header
    body_text: str  # plain-text body (capped — see _MAX_BODY_BYTES)
    body_format: str  # "text/plain" | "text/html" | "empty"
    attachment_names: list[str] = field(default_factory=list)


_MAX_BODY_BYTES = 64 * 1024  # 64 KB body cap — anything longer is truncated
# with a marker; full message lives in IMAP.


# ---------------------------------------------------------------------------
# RFC822 parsing
# ---------------------------------------------------------------------------


def _strip_addr(raw: str) -> str:
    """Extract bare email from a "Name <a@b>" header. Lower-cased."""
    if not raw:
        return ""
    addr = email.utils.parseaddr(raw)[1]
    return (addr or "").strip().lower()


def _split_addrs(raw: str) -> list[str]:
    """Parse a comma-separated address list into bare lower-cased emails."""
    if not raw:
        return []
    return [addr.lower() for _name, addr in email.utils.getaddresses([raw]) if addr]


def _select_body(msg: EmailMessage) -> tuple[str, str]:
    """Pick the best body part. Returns (text, format)."""
    # email.policy.default gives us .get_body(); prefer plain over html.
    body_part = msg.get_body(preferencelist=("plain", "html"))
    if body_part is None:
        return "", "empty"
    payload = body_part.get_content()
    if not isinstance(payload, str):
        try:
            payload = payload.decode("utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            payload = str(payload)
    fmt = body_part.get_content_type() or "text/plain"
    return payload, fmt


def parse_rfc822(raw_bytes: bytes) -> ParsedInboundEmail:
    """Parse one raw IMAP RFC822 blob into a ParsedInboundEmail."""
    msg = email.message_from_bytes(raw_bytes, policy=email.policy.default)
    if not isinstance(msg, EmailMessage):
        # email.policy.default returns EmailMessage; defensive cast for stubs.
        msg = email.message_from_bytes(raw_bytes, policy=email.policy.default)

    message_id = (msg.get("Message-ID") or "").strip()
    from_header = msg.get("From") or ""
    from_addr = _strip_addr(from_header)
    to_addrs = _split_addrs(msg.get("To") or "") + _split_addrs(msg.get("Cc") or "")
    subject = (msg.get("Subject") or "").strip()
    date_header = (msg.get("Date") or "").strip()

    body, fmt = _select_body(msg)
    # Cap body size — pathological campaigns can attach a 1 MB plain-text
    # signature. Truncate with a clear marker so the operator knows to
    # consult the live IMAP message if they need the rest.
    body_bytes = body.encode("utf-8", errors="replace")
    if len(body_bytes) > _MAX_BODY_BYTES:
        body = body_bytes[:_MAX_BODY_BYTES].decode("utf-8", errors="replace") + (
            f"\n\n--- truncated by samus poller at {_MAX_BODY_BYTES} bytes ---"
        )

    attachment_names: list[str] = []
    for part in msg.iter_attachments():
        name = part.get_filename() or ""
        if name:
            attachment_names.append(name)

    return ParsedInboundEmail(
        message_id=message_id,
        from_addr=from_addr,
        from_display=from_header.strip(),
        to_addrs=to_addrs,
        subject=subject,
        date_header=date_header,
        body_text=body,
        body_format=fmt,
        attachment_names=attachment_names,
    )


# ---------------------------------------------------------------------------
# Idempotency ledger (JSONL)
# ---------------------------------------------------------------------------


def _ledger_path() -> Path:
    return Path(get_settings().gmail_inbox_ledger_path)


def _ledger() -> persistence.JsonlLedger:
    return persistence.JsonlLedger(str(_ledger_path()))


def load_seen_message_ids() -> set[str]:
    """Scan the ledger and return the set of already-processed Message-IDs."""
    path = _ledger_path()
    if not path.exists():
        return set()
    seen: set[str] = set()
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            mid = rec.get("message_id")
            if mid:
                seen.add(mid)
    return seen


def _append_ledger(rec: dict[str, Any]) -> None:
    """Append one drain-result row. Best-effort — never raises."""
    try:
        _ledger().append(rec)
    except OSError as exc:
        _LOG.warning("inbound_email ledger append failed: %s", exc)


# ---------------------------------------------------------------------------
# Per-message processor (pure-ish — CRM + finance writes are the side effects)
# ---------------------------------------------------------------------------


@dataclass
class InboundEmailHandled:
    """One processed message — what artifact+task were produced + how."""

    message_id: str
    from_addr: str
    subject: str
    artifact_id: str = ""
    operator_task_id: str = ""
    opportunity_id: str = ""
    billing_state: str = ""
    billing_summary_line: str = ""
    persisted: bool = False
    error: str = ""
    # Populated after processing so the drain loop's forwarder can route on
    # the same signals the artifact/task were built from without redoing the
    # classification + intent LLM call.
    classification: dict[str, Any] | None = None
    intent: dict[str, Any] | None = None
    intent_task_prefix: str = ""  # e.g. "[CS/URGENT]" or "[CLIENT/COUNTER]"


def _safe_find_opportunity(email_addr: str) -> str:
    """Wrap find_opportunity_for_email in a try; lookup failures are non-fatal."""
    try:
        from backend.crm.service import find_opportunity_for_email

        return find_opportunity_for_email(email_addr) or ""
    except Exception as exc:  # noqa: BLE001 — DDB / Neo4j hiccup is non-fatal
        _LOG.warning("find_opportunity_for_email failed: %s", exc)
        return ""


def _safe_billing_summary(email_addr: str) -> tuple[str, str, str]:
    """Wrap get_customer_billing_summary; returns (state, one_line, error)."""
    try:
        from backend.finance.service import get_customer_billing_summary

        summary = get_customer_billing_summary(email_addr)
        return summary.state, summary.one_line_summary(), summary.lookup_error
    except Exception as exc:  # noqa: BLE001 — Stripe / network is non-fatal
        _LOG.warning("get_customer_billing_summary failed: %s", exc)
        return "lookup_failed", f"billing lookup raised: {exc}", str(exc)


def _build_task_description(
    parsed: ParsedInboundEmail,
    *,
    billing_line: str,
    opportunity_id: str,
    artifact_id: str,
    classification: dict | None = None,
) -> str:
    """Render the OperatorTask description body."""
    lines = [
        f"From:    {parsed.from_display or parsed.from_addr}",
        f"Subject: {parsed.subject or '(no subject)'}",
        f"Date:    {parsed.date_header or '(no date)'}",
        f"Billing: {billing_line}",
    ]
    if classification:
        cat = classification.get("category", "other")
        conf = classification.get("confidence", 0)
        extras = []
        if classification.get("vendor_registry_id"):
            extras.append(f"vendor={classification['vendor_registry_id']}")
        if classification.get("bill_amount_usd") is not None:
            extras.append(f"${classification['bill_amount_usd']:.2f}")
        if classification.get("bill_signal_kind"):
            extras.append(classification["bill_signal_kind"])
        extra_str = f" ({', '.join(extras)})" if extras else ""
        lines.append(f"Category: {cat} (conf={conf:.0%}){extra_str}")
    if opportunity_id:
        lines.append(f"Opportunity: {opportunity_id}")
    else:
        lines.append("Opportunity: (no open opportunity found for this email)")
    if artifact_id:
        lines.append(f"Artifact:    {artifact_id}")
    if parsed.attachment_names:
        lines.append(
            f"Attachments: {', '.join(parsed.attachment_names[:8])}"
            + (" (+more)" if len(parsed.attachment_names) > 8 else ""),
        )
    lines.append("")
    lines.append("--- Message body ---")
    lines.append(parsed.body_text or "(no body)")
    return "\n".join(lines)


def handle_parsed_email(parsed: ParsedInboundEmail) -> InboundEmailHandled:
    """Produce CRM artifact + task for one parsed inbound email.

    Routing branch at the top: YouTube channel-upload notifications go
    through :mod:`backend.intake.youtube_ingest` instead of the customer-
    reply CRM flow (different content, different consumer, different
    storage target — Hivemind KB nodes vs. CRM artifacts + operator
    tasks). YouTube ingest still returns an :class:`InboundEmailHandled`
    shaped result so the poller's ledger writer doesn't need to branch.

    Direct in-process calls into ``backend.crm.service`` — matches the
    script-driven dispatch pattern used by ``morning_send.py``. CRM and
    finance failures degrade gracefully: the artifact is still attempted,
    the task is still attempted, and the ledger row records whichever
    parts succeeded so the next pass doesn't double-process.
    """
    # ------ YouTube notification branch ---------------------------------
    # Cheap two-gate classifier (sender host + body URL); runs on every
    # email but skips out for non-YouTube mail without any external call.
    try:
        from backend.intake import youtube_ingest

        if youtube_ingest.is_youtube_notification(parsed):
            yt = youtube_ingest.handle_youtube_email(parsed)
            # Translate to the InboundEmailHandled shape so drain_once's
            # bookkeeping/ledger writer doesn't need to know about a
            # second result type. We don't create a CRM artifact/task --
            # the KB node IS the artifact for this path.
            return InboundEmailHandled(
                message_id=parsed.message_id,
                from_addr=parsed.from_addr,
                subject=parsed.subject,
                opportunity_id="",
                billing_state=f"youtube:{yt.distill_status}",
                billing_summary_line=(
                    f"YouTube insight {yt.video_id} -> "
                    f"target={yt.proposed_target_agent or 'none'} "
                    f"(transcript={yt.transcript_status})"
                ),
                artifact_id=yt.video_id,  # repurpose for KB node id
                operator_task_id="",  # no operator task on this path
                persisted=yt.persisted,
                error=yt.error,
            )
    except Exception as exc:  # noqa: BLE001 — YouTube path must never break the poller
        _LOG.warning(
            "youtube_ingest branch raised for %s: %s; falling through to CRM flow",
            getattr(parsed, "message_id", ""),
            exc,
        )

    # ------ Regular customer-reply CRM flow ------------------------------
    from backend.crm.models import (
        CreateArtifactRequest,
        CreateOperatorTaskRequest,
    )
    from backend.crm.service import create_artifact, create_operator_task

    result = InboundEmailHandled(
        message_id=parsed.message_id,
        from_addr=parsed.from_addr,
        subject=parsed.subject,
    )

    # 1. Try to find an existing opportunity for this sender.
    opportunity_id = _safe_find_opportunity(parsed.from_addr)
    result.opportunity_id = opportunity_id

    # 2. Billing summary (best-effort).
    billing_state, billing_line, _billing_error = _safe_billing_summary(parsed.from_addr)
    result.billing_state = billing_state
    result.billing_summary_line = billing_line

    # 2b. Classify the email (client_correspondence, bill, account, ...).
    classification_dict: dict = {}
    classification = None
    try:
        from backend.intake.email_classifier import classify

        classification = classify(parsed)
        classification_dict = classification.to_dict()
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("email_classifier failed for %s: %s", parsed.message_id, exc)
    result.classification = classification_dict or None

    is_client_correspondence = bool(
        classification and classification.category == "client_correspondence"
    )
    is_outbound = bool(is_client_correspondence and classification.direction == "outbound")

    # 3. Create the artifact (raw parsed email lives inline_data).
    # Known-client mail is owned by the client entity (not the opportunity or
    # bare contact) so operator surfaces can pivot from the client -> every
    # message on the thread (both directions).
    if is_client_correspondence:
        owner_entity_kind = "client"
        owner_entity_id = classification.client_id  # e.g. "sample_school"
        artifact_kind = "client_correspondence"
    else:
        owner_entity_kind = "opportunity" if opportunity_id else "contact"
        owner_entity_id = opportunity_id or parsed.from_addr
        artifact_kind = "inbound_email"

    inline = {
        "message_id": parsed.message_id,
        "from_addr": parsed.from_addr,
        "from_display": parsed.from_display,
        "to_addrs": parsed.to_addrs,
        "subject": parsed.subject,
        "date_header": parsed.date_header,
        "body_text": parsed.body_text,
        "body_format": parsed.body_format,
        "attachment_names": parsed.attachment_names,
        "billing_state": billing_state,
        "billing_summary_line": billing_line,
    }
    if classification_dict:
        inline["classification"] = classification_dict
        # Persist direction + original headers at the top-level so an
        # operator surface reading artifacts can join both sides of the
        # thread without cracking open the classification dict.
        if is_client_correspondence:
            inline["direction"] = classification.direction
            if classification.original_to:
                inline["original_to"] = classification.original_to
            if classification.original_subject:
                inline["original_subject"] = classification.original_subject
            if classification.original_date:
                inline["original_date"] = classification.original_date

    # LLM-backed intent reasoning — client correspondence only. Local-first
    # (LM Studio), fail-soft: on empty / malformed output we skip attaching
    # the intent dict and the artifact still writes cleanly. Reasoning runs
    # over the ORIGINAL body when this is a forwarded outbound (the
    # operator's own reply text) rather than the forward wrapper.
    intent_result = None
    if is_client_correspondence:
        try:
            from backend.intake.correspondence_intent import reason_intent

            # For outbound forwards, use the body BELOW the preamble as the
            # reasoning target so the intent classifier sees the operator's
            # actual message, not the wrapper.
            reason_body = parsed.body_text
            reason_subject = parsed.subject
            if is_outbound:
                reason_subject = classification.original_subject or parsed.subject
                # Everything after the preamble's header block — the operator's
                # sent message text. Split on the first blank line following
                # the preamble; the body_text was already truncated to 3000
                # chars inside reason_intent so we don't need to slice here.
                marker_hit = "Forwarded message" in parsed.body_text
                if marker_hit:
                    # Take the tail after the LAST header-block blank line
                    # by splitting on the first double-newline that follows
                    # the "Forwarded message" marker.
                    idx = parsed.body_text.find("Forwarded message")
                    tail = parsed.body_text[idx:]
                    # Skip header lines until we hit a blank
                    split_at = tail.find("\n\n")
                    if split_at >= 0:
                        reason_body = tail[split_at + 2 :].strip()
            intent_result = reason_intent(
                direction=classification.direction,
                client_id=classification.client_id,
                subject=reason_subject,
                body_text=reason_body,
            )
            if intent_result and not intent_result.error and not intent_result.is_empty():
                inline["intent"] = intent_result.to_dict()
                result.intent = intent_result.to_dict()
        except Exception as exc:  # noqa: BLE001 — intent must never break capture
            _LOG.warning(
                "intent reasoning failed for %s: %s",
                parsed.message_id,
                exc,
            )
            intent_result = None
    artifact_req = CreateArtifactRequest(
        kind=artifact_kind,
        owner_entity_kind=owner_entity_kind,
        owner_entity_id=owner_entity_id,
        title=(parsed.subject or "(no subject)")[:200],
        inline_data=inline,
        mime_type="message/rfc822",
        bytes=len(parsed.body_text.encode("utf-8", errors="replace")),
        source="intake.gmail_poller",
        created_by="intake.gmail_poller",
    )
    try:
        artifact_res = create_artifact(artifact_req)
        result.artifact_id = artifact_res.artifact_id
        if artifact_res.status != "created":
            result.error = f"artifact_{artifact_res.status}: {artifact_res.error or ''}"
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("create_artifact failed for %s: %s", parsed.message_id, exc)
        result.error = f"artifact_raised: {exc}"

    # 4. Create the operator task (the human-facing surface).
    # OUTBOUND client correspondence (operator forwarded their own sent
    # reply) does NOT get an operator task — the operator already sent it,
    # there is nothing pending. The artifact IS the record. Persistence is
    # marked from the artifact write above.
    if is_outbound:
        if result.artifact_id and not result.error:
            result.persisted = True
    else:
        if is_client_correspondence:
            client_tag = classification.client_id.replace("_", " ").title()
            # Route intent -> (task_kind, urgency, due_at, title_prefix).
            # Puts service_issue_reported / escalation_needed on a dedicated
            # customer_service track with due_at=now; sales-cycle intents
            # go to normal client_correspondence with softer due offsets.
            from backend.crm.client_intent_routing import route_client_intent

            intent_tag_value = (
                intent_result.intent if intent_result and not intent_result.error else None
            )
            action = route_client_intent(intent_tag_value)
            task_title = (
                f"{action.title_prefix} {client_tag}: {(parsed.subject or '(no subject)')[:80]}"
            )
            task_kind = action.task_kind
            task_due_at = action.due_at
            # Expose the routed prefix to the drain loop's forwarder so
            # both faces of the operator queue (in-app + Gmail forward)
            # carry the same category label.
            result.intent_task_prefix = action.title_prefix
        else:
            cat_prefix = (
                classification_dict.get("category", "").upper() if classification_dict else ""
            )
            task_title_prefix = f"[{cat_prefix}] " if cat_prefix else ""
            task_title = f"{task_title_prefix}Reply: {(parsed.subject or '(no subject)')[:80]}"
            task_kind = "reply_email"
            task_due_at = ""  # non-client mail keeps the default (no due)

        task_desc = _build_task_description(
            parsed,
            billing_line=billing_line,
            opportunity_id=opportunity_id,
            artifact_id=result.artifact_id,
            classification=classification_dict or None,
        )
        task_req = CreateOperatorTaskRequest(
            kind=task_kind,
            title=task_title,
            description=task_desc,
            due_at=task_due_at,
            related_entity_kind=owner_entity_kind,
            related_entity_id=owner_entity_id,
            source="intake.gmail_poller",
            source_ref=parsed.message_id,
        )
        try:
            task_res = create_operator_task(task_req)
            result.operator_task_id = task_res.operator_task_id
            if task_res.status != "created":
                prior = result.error
                result.error = f"{prior}; task_{task_res.status}: {task_res.error or ''}".strip(
                    "; "
                )
            else:
                result.persisted = True
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("create_operator_task failed for %s: %s", parsed.message_id, exc)
            prior = result.error
            result.error = f"{prior}; task_raised: {exc}".strip("; ")

    # ------ Client correspondence -> business-event ledger ------------------
    # Signed/existing-client mail emits a CLIENT_CORRESPONDENCE event so the
    # unified business-event ledger has one chronological view per client
    # (analog to the per-prospect journey). Best-effort — a telemetry hiccup
    # must never break the poller.
    if is_client_correspondence:
        try:
            from backend.common.business_events import (
                CLIENT_CORRESPONDENCE,
                emit_business_event,
            )

            event_metadata: dict[str, Any] = {
                "client_id": classification.client_id,
                "client_role": classification.client_role,
                "direction": classification.direction,
                "from_addr": parsed.from_addr,
                "to_addr": (
                    classification.original_to
                    if classification.direction == "outbound"
                    else (parsed.to_addrs[0] if parsed.to_addrs else "")
                ),
                "subject": (classification.original_subject or parsed.subject),
                "message_id": parsed.message_id,
                "artifact_id": result.artifact_id,
                "operator_task_id": result.operator_task_id,
            }
            if intent_result and not intent_result.error and not intent_result.is_empty():
                if intent_result.intent:
                    event_metadata["intent"] = intent_result.intent
                if intent_result.sentiment:
                    event_metadata["sentiment"] = intent_result.sentiment
                if intent_result.requested_action:
                    event_metadata["requested_action"] = intent_result.requested_action
                if intent_result.summary_sentence:
                    event_metadata["summary"] = intent_result.summary_sentence
            emit_business_event(
                CLIENT_CORRESPONDENCE,
                workcell="intake",
                campaign_id=classification.campaign_id or None,
                metadata=event_metadata,
            )
        except Exception as exc:  # noqa: BLE001 — telemetry must not break the poller
            _LOG.warning(
                "client_correspondence event emit failed for %s: %s",
                parsed.message_id,
                exc,
            )

    # ------ Auto-project meeting requests onto the planner calendar --------
    # When a client asks for a meeting (intent=requested_meeting), drop a
    # placeholder onto samushustleforge@'s calendar so the operator sees
    # the request everywhere it belongs: operator queue AND planner. The
    # placeholder is scheduled 48h out at 10 AM UTC as a TBD marker —
    # operator moves it to the real slot once the meeting is confirmed.
    # Idempotent via source_id = original message_id.
    if (
        is_client_correspondence
        and classification.direction == "inbound"
        and intent_result
        and not intent_result.error
        and intent_result.intent == "requested_meeting"
    ):
        try:
            from datetime import datetime, timedelta, timezone

            from backend.intake.calendar_projection import project_event

            tbd_start = (
                (
                    datetime.now(timezone.utc).replace(hour=10, minute=0, second=0, microsecond=0)
                    + timedelta(days=2)
                )
                .isoformat()
                .replace("+00:00", "Z")
            )
            desc_parts = [
                f"Client: {classification.client_id}",
                f"From:   {parsed.from_addr}",
                "",
                intent_result.summary_sentence or "",
                "",
                (parsed.body_text or "")[:1500],
            ]
            project_event(
                title=f"TBD: {parsed.subject or 'meeting request'}",
                start_iso=tbd_start,
                projection_kind="meeting_request",
                description="\n".join(p for p in desc_parts if p is not None),
                client_id=classification.client_id,
                campaign_id=classification.campaign_id,
                source_id=parsed.message_id,
                source_workcell="intake",
            )
        except Exception as exc:  # noqa: BLE001 — must never break the drain
            _LOG.warning(
                "meeting-request projection failed for %s: %s",
                parsed.message_id,
                exc,
            )

    # ------ Reply-handling pod chain (flag-gated; observe-only when off) -----
    # IntentClassifier -> state signal + compliance-checked follow-up draft.
    # Purely additive: the artifact + operator task above are unchanged; this
    # only ADDS a classified draft artifact and (for opt-outs) auto
    # suppress+halt. Best-effort — never breaks the poller.
    #
    # SKIPPED for known-client correspondence: the pod is prospect-nurture
    # code (INTERESTED / OPT_OUT / QUALIFIED intents driving cash-engine
    # signals). Signed clients don't belong in that flow — an operator (or
    # a future customer-service handler) handles them explicitly.
    if not is_client_correspondence:
        try:
            from backend.common.config import get_settings as _gs

            if getattr(_gs(), "reply_handling_enabled", False):
                _handle_reply_intent(parsed, result.opportunity_id)
        except Exception as exc:  # noqa: BLE001 — pod chain must never break the poller
            _LOG.warning("reply-handling pod chain failed for %s: %s", parsed.message_id, exc)

    return result


def _handle_reply_intent(parsed: ParsedInboundEmail, opportunity_id: str) -> None:
    """Classify an inbound reply, drive state via the existing cash-engine
    signal fan-in, honor opt-outs, and attach a compliance-checked draft.

    Reuses ``feedback.handlers.fire_cash_engine_signal`` (the canonical
    reply->state seam) rather than mutating CallState/Opportunity directly, and
    the suppression list (which the ComplianceGuard reads pre-send). Never sends
    anything; the follow-up is a DRAFT artifact for the operator.
    """
    from backend.intake.follow_up_drafter import draft_follow_up
    from backend.intake.reply_classifier import (
        INTENT_INTERESTED,
        INTENT_MEETING_BOOKED,
        INTENT_OPT_OUT,
        classify_reply,
    )

    intent = classify_reply(parsed.subject, parsed.body_text)
    addr = (parsed.from_addr or "").strip()

    # Resolve prospect_id for the state signal (opportunity_id passed in).
    prospect_id = ""
    try:
        from backend.common import recipient_index

        rec = recipient_index.lookup_recipient(addr) or {}
        prospect_id = str(rec.get("prospect_id", "") or "")
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("reply recipient lookup failed for %s: %s", addr, exc)

    # State transition via the existing signal fan-in (positive -> reengage,
    # opt-out -> halt). reengage is DORMANT-gated downstream so a parked deal
    # is a safe no-op.
    try:
        from backend.feedback import handlers

        if intent.intent == INTENT_OPT_OUT:
            handlers.fire_cash_engine_signal(
                event="unsubscribe",
                opportunity_id=opportunity_id,
                prospect_id=prospect_id,
            )
        elif intent.intent in (INTENT_INTERESTED, INTENT_MEETING_BOOKED):
            handlers.fire_cash_engine_signal(
                event="reply",
                opportunity_id=opportunity_id,
                prospect_id=prospect_id,
            )
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("reply state signal failed (%s): %s", intent.intent, exc)

    # Honor opt-out on the suppression list — the ComplianceGuard blocks future
    # sends to this address (Component 1 integration). Best-effort.
    if intent.intent == INTENT_OPT_OUT and addr:
        try:
            from backend.common import aws
            from backend.common.config import get_settings
            from backend.common.dates import iso_now

            s = get_settings()
            aws.table(s.ddb_suppression_table, s.aws_region).put_item(
                Item={"email": addr.lower(), "reason": "reply_opt_out", "ts": iso_now()},
            )
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("reply opt-out suppression write failed for %s: %s", addr, exc)

    # Compliance-checked follow-up DRAFT -> CRM artifact for operator review.
    try:
        draft = draft_follow_up(
            intent.intent,
            original_subject=parsed.subject,
            from_addr=addr,
        )
        from backend.crm.models import CreateArtifactRequest
        from backend.crm.service import create_artifact

        owner_kind = "opportunity" if opportunity_id else "contact"
        # ArtifactKind is a closed Literal; "content_draft" is the closest fit
        # for a drafted reply. The follow-up nature is carried in inline_data.
        payload = {
            "artifact_subtype": "follow_up_draft",
            "intent": intent.to_dict(),
            "draft": draft.to_dict(),
        }
        create_artifact(
            CreateArtifactRequest(
                kind="content_draft",
                owner_entity_kind=owner_kind,
                owner_entity_id=opportunity_id or addr,
                title=f"[{intent.intent}] draft reply: {(parsed.subject or '(no subject)')[:120]}",
                inline_data=payload,
                mime_type="application/json",
                bytes=len(str(payload).encode("utf-8", errors="replace")),
                source="intake.reply_classifier",
                created_by="intake.reply_classifier",
            )
        )
        _LOG.info(
            "reply-handling: %s (conf=%.2f) draft%s for %s",
            intent.intent,
            intent.confidence,
            " send-recommended" if draft.send_recommended else "",
            parsed.message_id,
        )
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("reply follow-up draft/artifact failed for %s: %s", parsed.message_id, exc)


# ---------------------------------------------------------------------------
# Drain pass (entry point)
# ---------------------------------------------------------------------------


@dataclass
class DrainPassResult:
    """Outcome of one drain_once call."""

    enabled: bool  # False -> config missing, no-op
    fetched: int = 0
    processed: int = 0  # produced both artifact + task ok
    duplicates: int = 0  # message_id already in ledger
    failed: int = 0  # at least one of artifact/task failed
    connect_error: str = ""  # auth/network failure (whole pass failed)
    handled: list[InboundEmailHandled] = field(default_factory=list)


def drain_once(
    *,
    api_factory=None,
    now: str | None = None,
) -> DrainPassResult:
    """One drain pass: list unread -> fetch raw -> process -> mark read.

    ``api_factory`` is for tests — a zero-arg callable returning a context
    manager that exposes ``list_unread_message_ids``, ``fetch_raw``,
    ``mark_read``. Production callers leave it ``None`` and let the
    function build a :class:`GmailApiClient` from settings.

    Returns a typed :class:`DrainPassResult` summary. Never raises.
    """
    settings = get_settings()
    if not (
        settings.gmail_inbox_email
        and settings.gmail_oauth_client_id
        and settings.gmail_oauth_client_secret
    ):
        _LOG.info(
            "gmail poller disabled: gmail_inbox_email / oauth_client_id / "
            "oauth_client_secret unset",
        )
        return DrainPassResult(enabled=False)

    ts = now or iso_now()

    if api_factory is None:

        def api_factory() -> GmailApiClient:  # type: ignore[no-redef]
            return GmailApiClient(
                client_id=settings.gmail_oauth_client_id,
                client_secret=settings.gmail_oauth_client_secret,
                token_path=Path(settings.gmail_oauth_token_path),
            )

    out = DrainPassResult(enabled=True)
    seen_message_ids = load_seen_message_ids()
    max_per_pass = max(1, int(settings.gmail_inbox_max_per_pass or 25))

    try:
        with api_factory() as client:
            message_ids = client.list_unread_message_ids(max_results=max_per_pass)
            out.fetched = len(message_ids)
            for gmail_id in message_ids:
                try:
                    raw = client.fetch_raw(gmail_id)
                    parsed = parse_rfc822(raw)
                except Exception as exc:  # noqa: BLE001 — bad single msg
                    _LOG.warning(
                        "gmail fetch/parse failed gmail_id=%s: %s",
                        gmail_id,
                        exc,
                    )
                    out.failed += 1
                    _append_ledger(
                        {
                            "ts": ts,
                            "gmail_id": gmail_id,
                            "message_id": "",
                            "status": "fetch_failed",
                            "error": str(exc)[:240],
                        }
                    )
                    continue

                if parsed.message_id and parsed.message_id in seen_message_ids:
                    out.duplicates += 1
                    # Still mark read on Gmail so the label matches the ledger.
                    try:
                        client.mark_read(gmail_id)
                    except Exception as exc:  # noqa: BLE001
                        _LOG.warning("mark_read on duplicate failed: %s", exc)
                    continue

                handled = handle_parsed_email(parsed)
                out.handled.append(handled)
                if handled.persisted and not handled.error:
                    out.processed += 1
                else:
                    out.failed += 1

                ledger_row: dict[str, Any] = {
                    "ts": ts,
                    "gmail_id": gmail_id,
                    "message_id": parsed.message_id,
                    "from_addr_tail": parsed.from_addr[-12:],
                    "subject_head": parsed.subject[:120],
                    "artifact_id": handled.artifact_id,
                    "operator_task_id": handled.operator_task_id,
                    "opportunity_id": handled.opportunity_id,
                    "billing_state": handled.billing_state,
                    "persisted": handled.persisted,
                    "error": handled.error[:240] if handled.error else "",
                }
                try:
                    from backend.intake.email_classifier import classify as _classify

                    _cls = _classify(parsed)
                    ledger_row["category"] = _cls.category
                    if _cls.vendor_registry_id:
                        ledger_row["vendor"] = _cls.vendor_registry_id
                    if _cls.bill_amount_usd is not None:
                        ledger_row["amount_usd"] = _cls.bill_amount_usd
                    if _cls.bill_signal_kind:
                        ledger_row["bill_signal_kind"] = _cls.bill_signal_kind
                except Exception:  # noqa: BLE001
                    pass
                # ---- Calendar-event projection (per operator directive) ----
                # Emails classified as calendar get their event details
                # extracted (from .ics attachment when present, LLM
                # fallback on the body) and projected onto samus's own
                # Google Calendar (primary calendar of the OAuth-bound
                # inbox). Operator reviews scheduling there instead of
                # having to open each Gmail forward.
                calendar_outcome: dict[str, Any] | None = None
                if (
                    handled.persisted
                    and not handled.error
                    and (handled.classification or {}).get("category") == "calendar"
                ):
                    try:
                        from backend.intake.calendar_api_client import (
                            CalendarApiClient,
                            CalendarApiError,
                        )
                        from backend.intake.calendar_ingest import project_event

                        with CalendarApiClient(
                            client_id=settings.gmail_oauth_client_id,
                            client_secret=settings.gmail_oauth_client_secret,
                            token_path=Path(settings.gmail_oauth_token_path),
                        ) as cal_client:
                            try:
                                cal_client.check_scope_or_raise()
                            except CalendarApiError as exc:
                                calendar_outcome = {
                                    "created": False,
                                    "error": str(exc),
                                }
                            else:
                                calendar_outcome = project_event(
                                    cal_client,
                                    parsed,
                                    raw,
                                    artifact_id=handled.artifact_id,
                                )
                    except Exception as exc:  # noqa: BLE001 — never break drain
                        _LOG.warning(
                            "calendar project raised for %s: %s",
                            gmail_id,
                            exc,
                        )
                        calendar_outcome = {
                            "created": False,
                            "error": f"raised: {exc}",
                        }

                if calendar_outcome:
                    ledger_row["calendar"] = calendar_outcome

                # ---- Categorized forward + trash (per operator directive) ----
                # After the durable CRM record is written, forward the message
                # to the operator's personal inbox with a [CATEGORY/INTENT]
                # subject prefix and TRASH the original — CRM is the source of
                # truth, the Gmail copy is redundant. Fires ONLY on success
                # (persisted + no error) so a failed capture is left in the
                # inbox for retry on the next pass.
                forward_outcome: dict[str, Any] | None = None
                if handled.persisted and not handled.error:
                    try:
                        from backend.intake.email_forwarder import (
                            forward_and_cleanup,
                            is_configured as _fwd_configured,
                        )

                        if _fwd_configured():
                            fwd = forward_and_cleanup(
                                gmail_client=client,
                                original_gmail_id=gmail_id,
                                parsed=parsed,
                                classification=handled.classification,
                                intent=handled.intent,
                                intent_action_prefix=(handled.intent_task_prefix or None),
                            )
                            forward_outcome = {
                                "forwarded": fwd.forwarded,
                                "trashed": fwd.trashed,
                                "prefix": fwd.category_prefix,
                                "urgent": fwd.is_urgent,
                                "error": fwd.error,
                            }
                    except Exception as exc:  # noqa: BLE001 — never break drain
                        _LOG.warning(
                            "forward step raised for %s: %s",
                            gmail_id,
                            exc,
                        )

                if forward_outcome:
                    ledger_row["forward"] = forward_outcome

                _append_ledger(ledger_row)
                if parsed.message_id:
                    seen_message_ids.add(parsed.message_id)

                # Mark read even on partial failure: the artifact / task were
                # attempted, the ledger captured the outcome, and re-fetching
                # the same message will just hit the ledger as a duplicate.
                # Leaving it UNREAD would poison-pill every drain pass.
                # SKIP if we already trashed — a trashed message can't be
                # UNREAD-labeled (the API 404s on trashed refs).
                already_gone = bool(forward_outcome and forward_outcome.get("trashed"))
                if not already_gone:
                    try:
                        client.mark_read(gmail_id)
                    except Exception as exc:  # noqa: BLE001
                        _LOG.warning("mark_read failed gmail_id=%s: %s", gmail_id, exc)
    except GmailApiError as exc:
        out.connect_error = f"gmail_api_error: {exc}"
        _LOG.warning("gmail poller API error: %s", exc)
    except OSError as exc:
        out.connect_error = f"network_error: {exc}"
        _LOG.warning("gmail poller network error: %s", exc)
    except Exception as exc:  # noqa: BLE001 — never crash the script
        out.connect_error = f"unexpected_error: {exc}"
        _LOG.warning("gmail poller unexpected error: %s", exc)

    return out


# ---------------------------------------------------------------------------
# Script entry point (Poll-Inbox.ps1 -> python -m backend.intake.gmail_poller)
# ---------------------------------------------------------------------------


def main() -> int:
    """Drain once + print one summary line. Exit 0 on success or skip.

    Exit codes:
        0 - drain completed (any count of processed/duplicates/failed)
        0 - poller disabled (config not set is not an error)
        1 - connection failure (whole pass died before fetching anything)
    """
    logging.basicConfig(
        level=os.getenv("SAMUS_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    result = drain_once()
    if not result.enabled:
        print(
            "gmail poller: disabled "
            "(gmail_inbox_email / oauth_client_id / oauth_client_secret unset)",
        )
        return 0
    if result.connect_error:
        print(f"gmail poller: FAILED connect_error={result.connect_error}")
        return 1
    print(
        f"gmail poller: fetched={result.fetched} processed={result.processed} "
        f"duplicates={result.duplicates} failed={result.failed}",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
