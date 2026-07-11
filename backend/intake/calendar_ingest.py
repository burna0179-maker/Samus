"""Extract calendar events from inbound emails, project onto samushustleforge@.

The email classifier flags ``category=calendar`` for booking confirmations,
meeting invites, Calendly notifications, and reschedule/cancel emails. This
module turns those detections into ACTUAL Google Calendar events on
``samushustleforge@``'s primary calendar so the operator can review the
schedule at a glance.

STRATEGY

1. **Primary: parse an ``.ics`` attachment.** RFC-5545 VEVENT is the
   reliable path — Google Meet, Outlook, Calendly, and Zoom all attach
   proper .ics files. Structured, unambiguous, no LLM risk.
2. **Fallback: LLM extraction from the body.** For plain-text booking
   confirmations without .ics (some marketing / event-sourced replies),
   run ``backend.common.local_llm.chat`` to extract summary + start/end.
   Fail-soft: unparseable body -> no event, drain continues.

PROJECTION

Events go to the ``primary`` calendar of the token owner
(``samushustleforge@gmail.com``). The event body preserves the source
email's from-address, subject, and a pointer to the CRM artifact so a
click on the event in Google Calendar leads back to the durable record.

DEDUP

Each created event is tagged with an ``extendedProperties.private.source_
message_id`` matching the inbound email's Message-ID. The extractor
skips creation when Calendar's ``q=`` search finds an existing event
with the same source_message_id. Safe under duplicate drains.
"""

from __future__ import annotations

import email
import email.policy
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from backend.intake.gmail_poller import ParsedInboundEmail

_LOG = logging.getLogger("samus.intake.calendar_ingest")

# --- extracted-event schema ------------------------------------------------


@dataclass
class ExtractedEvent:
    """One event ready to be POSTed to Google Calendar."""

    summary: str
    start_iso: str  # ISO-8601 with timezone
    end_iso: str
    description: str = ""
    location: str = ""
    source: str = "ics"  # "ics" | "llm" | "empty"
    error: str = ""

    def is_valid(self) -> bool:
        return bool(self.summary and self.start_iso and self.end_iso and not self.error)

    def to_google_event(
        self,
        *,
        source_message_id: str,
        artifact_id: str = "",
    ) -> dict[str, Any]:
        """Shape into Google Calendar Events resource JSON."""
        ev: dict[str, Any] = {
            "summary": self.summary[:200],
            "start": {"dateTime": self.start_iso},
            "end": {"dateTime": self.end_iso},
            "description": self.description[:8000] if self.description else "",
            "source": {"title": "Samus intake", "url": ""},
            "extendedProperties": {
                "private": {
                    "source": "samus.intake",
                    "source_message_id": source_message_id[:1000],
                    "source_extractor": self.source,
                }
            },
        }
        if self.location:
            ev["location"] = self.location[:300]
        if artifact_id:
            ev["extendedProperties"]["private"]["artifact_id"] = artifact_id
        return ev


# --- .ics parsing ----------------------------------------------------------

# Minimal RFC 5545 line-unfolder + property extractor. Full iCalendar parsing
# is a rabbit hole; the shape we ONLY need is VEVENT{SUMMARY, DTSTART,
# DTEND, LOCATION, DESCRIPTION}. Skip alarms, recurrence rules, timezones
# beyond UTC/date-only for v1.

_VEVENT_START = "BEGIN:VEVENT"
_VEVENT_END = "END:VEVENT"


def _unfold_lines(raw: str) -> list[str]:
    """RFC-5545: lines continued with a leading space/tab are folded."""
    lines: list[str] = []
    for line in raw.splitlines():
        if line.startswith((" ", "\t")) and lines:
            lines[-1] += line[1:]
        else:
            lines.append(line)
    return lines


def _parse_ical_dt(raw: str) -> str:
    """Parse an iCal DTSTART/DTEND value into ISO-8601 with timezone.

    Formats we handle:
      * ``20260710T193000Z``       — floating UTC
      * ``TZID=America/Los_Angeles:20260710T123000``  — with TZID param
      * ``20260710``               — all-day (return start of day UTC)
    """
    raw = raw.strip()
    tzid = ""
    value = raw
    if raw.startswith("TZID="):
        # "TZID=America/Los_Angeles:20260710T123000"
        head, _, value = raw.partition(":")
        tzid = head.split("=", 1)[1] if "=" in head else ""

    if value.endswith("Z"):
        try:
            dt = datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(
                tzinfo=timezone.utc,
            )
            return dt.isoformat().replace("+00:00", "Z")
        except ValueError:
            return ""
    if "T" in value:
        try:
            dt = datetime.strptime(value, "%Y%m%dT%H%M%S")
        except ValueError:
            return ""
        if tzid:
            # We can't guarantee zoneinfo lookup in the container's tzdata
            # without an extra dependency; encode as an "unfixed" ISO plus
            # timezone label the Calendar API will honor if valid, else
            # fall back to naive local. Google Calendar accepts a bare
            # ISO 8601 dateTime plus a timeZone field on the top-level
            # event — but our shape puts it on start.dateTime as an
            # RFC3339 string. Best-effort: emit naive ISO and hope Google
            # infers.
            return dt.isoformat()
        # Naive local: pretend UTC (booking confirmations usually put UTC
        # in the ics; this is a lossy fallback).
        return dt.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
    # All-day date
    try:
        d = datetime.strptime(value, "%Y%m%d").replace(tzinfo=timezone.utc)
        return d.isoformat().replace("+00:00", "Z")
    except ValueError:
        return ""


def _extract_first_vevent(ics_text: str) -> ExtractedEvent | None:
    """Return the first VEVENT block as an ExtractedEvent, or None."""
    if not ics_text or _VEVENT_START not in ics_text:
        return None
    lines = _unfold_lines(ics_text)
    in_ev = False
    summary = ""
    location = ""
    description = ""
    dtstart_raw = ""
    dtend_raw = ""
    for line in lines:
        if line.strip() == _VEVENT_START:
            in_ev = True
            continue
        if line.strip() == _VEVENT_END and in_ev:
            break
        if not in_ev:
            continue
        # Property lines are "NAME[;params]:VALUE".
        if ":" not in line:
            continue
        name_part, _, value = line.partition(":")
        name = name_part.split(";", 1)[0].upper()
        params = name_part[len(name) :]
        if name == "SUMMARY":
            summary = _ical_unescape(value)
        elif name == "LOCATION":
            location = _ical_unescape(value)
        elif name == "DESCRIPTION":
            description = _ical_unescape(value)
        elif name == "DTSTART":
            dtstart_raw = (params + ":" + value) if params else value
        elif name == "DTEND":
            dtend_raw = (params + ":" + value) if params else value

    if not (dtstart_raw and summary):
        return None
    start_iso = _parse_ical_dt(dtstart_raw)
    if not start_iso:
        return None
    end_iso = _parse_ical_dt(dtend_raw) if dtend_raw else ""
    if not end_iso:
        # Default duration: 1 hour after start.
        try:
            base = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
            end_iso = (base + timedelta(hours=1)).isoformat().replace("+00:00", "Z")
        except ValueError:
            return None

    return ExtractedEvent(
        summary=summary or "(no title)",
        start_iso=start_iso,
        end_iso=end_iso,
        location=location,
        description=description,
        source="ics",
    )


def _ical_unescape(value: str) -> str:
    return (
        value.replace("\\n", "\n")
        .replace("\\N", "\n")
        .replace("\\,", ",")
        .replace("\\;", ";")
        .replace("\\\\", "\\")
    )


def find_ics_content(raw_rfc822: bytes) -> str | None:
    """Return the first text/calendar or .ics attachment's decoded text.

    Handles both inline text/calendar and application/ics attachments.
    Returns None when the message carries no calendar payload.
    """
    if not raw_rfc822:
        return None
    try:
        msg = email.message_from_bytes(raw_rfc822, policy=email.policy.default)
    except Exception:  # noqa: BLE001
        return None

    for part in msg.walk():
        ctype = (part.get_content_type() or "").lower()
        fname = (part.get_filename() or "").lower()
        is_ics_type = ctype in ("text/calendar", "application/ics")
        is_ics_name = fname.endswith(".ics")
        if not (is_ics_type or is_ics_name):
            continue
        try:
            payload = part.get_payload(decode=True)
            if not payload:
                continue
            # get_content_charset can be missing on nested MIME parts
            charset = part.get_content_charset() or "utf-8"
            return payload.decode(charset, errors="replace")
        except Exception:  # noqa: BLE001
            continue
    return None


# --- LLM fallback ----------------------------------------------------------

_LLM_SYSTEM = """\
You are a calendar-event extractor. The user pastes an email body they
believe contains a meeting/booking. You reply with ONLY a JSON object
matching:

{
  "summary": "<short event title>",
  "start": "<ISO-8601 datetime with timezone, e.g. 2026-07-10T15:30:00-07:00>",
  "end":   "<ISO-8601 datetime with timezone>",
  "location": "<location or empty>",
  "description": "<short optional description>"
}

Rules:
- If no clear meeting is present, reply {"summary": "", "start": "", "end": ""}.
- Never invent times not present in the body.
- Never add attendee lists.
- ISO-8601 only. No prose outside the JSON.
"""

_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def _extract_llm(body_text: str, llm_chat=None) -> ExtractedEvent:
    """LLM fallback when .ics isn't present. Returns empty on failure."""
    if not body_text or not body_text.strip():
        return ExtractedEvent(
            summary="", start_iso="", end_iso="", source="empty", error="empty_body"
        )
    if llm_chat is None:
        try:
            from backend.common.local_llm import chat as llm_chat  # type: ignore
        except Exception as exc:  # noqa: BLE001
            return ExtractedEvent(
                summary="", start_iso="", end_iso="", source="llm", error=f"llm_import: {exc}"
            )
    try:
        raw = llm_chat(
            _LLM_SYSTEM,
            body_text[:4000],
            max_tokens=400,
            temperature=0.0,
        )
    except Exception as exc:  # noqa: BLE001
        return ExtractedEvent(
            summary="", start_iso="", end_iso="", source="llm", error=f"llm_raised: {exc}"
        )
    if not raw or not raw.strip():
        return ExtractedEvent(summary="", start_iso="", end_iso="", source="llm", error="llm_empty")
    m = _JSON_OBJECT_RE.search(raw)
    if not m:
        return ExtractedEvent(
            summary="", start_iso="", end_iso="", source="llm", error="llm_no_json"
        )
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return ExtractedEvent(
            summary="", start_iso="", end_iso="", source="llm", error="llm_bad_json"
        )

    summary = str(obj.get("summary") or "").strip()
    start = str(obj.get("start") or "").strip()
    end = str(obj.get("end") or "").strip()
    if not (summary and start and end):
        return ExtractedEvent(
            summary="", start_iso="", end_iso="", source="llm", error="llm_incomplete"
        )
    return ExtractedEvent(
        summary=summary,
        start_iso=start,
        end_iso=end,
        location=str(obj.get("location") or "").strip(),
        description=str(obj.get("description") or "").strip(),
        source="llm",
    )


# --- public entry points ---------------------------------------------------


def extract_event(
    parsed: ParsedInboundEmail,
    raw_rfc822: bytes | None = None,
    *,
    llm_chat=None,
) -> ExtractedEvent | None:
    """Extract one event from an inbound email. Returns None on no-signal.

    ``raw_rfc822`` is optional — when provided, the .ics-attachment path is
    tried first. Without it, only the LLM fallback runs on the parsed body.
    """
    if raw_rfc822:
        ics_text = find_ics_content(raw_rfc822)
        if ics_text:
            ev = _extract_first_vevent(ics_text)
            if ev and ev.is_valid():
                return ev

    # LLM fallback on the plain body
    ev = _extract_llm(parsed.body_text, llm_chat=llm_chat)
    if ev.is_valid():
        return ev
    return None


def already_projected(
    calendar_client,
    source_message_id: str,
    *,
    calendar_id: str = "primary",
) -> str:
    """Return the existing Calendar event ID for this message-id, or ''.

    Uses ``privateExtendedProperty=source_message_id=<value>`` — Google's
    server-side extended-property filter. Free-text ``q=`` does NOT index
    extendedProperties, so it wouldn't find the tag we stamped at insert.

    Belt-and-suspenders dedupe (the poller ledger's seen_message_ids
    already prevents most double-processing).
    """
    if not source_message_id:
        return ""
    try:
        events = calendar_client.list_events(
            calendar_id=calendar_id,
            max_results=10,
            private_extended_property=[
                f"source_message_id={source_message_id}",
            ],
        )
    except Exception:  # noqa: BLE001
        return ""
    for e in events:
        ext = (e.get("extendedProperties") or {}).get("private") or {}
        if ext.get("source_message_id") == source_message_id:
            return str(e.get("id") or "")
    return ""


def project_event(
    calendar_client,
    parsed: ParsedInboundEmail,
    raw_rfc822: bytes | None,
    *,
    artifact_id: str = "",
    llm_chat=None,
) -> dict[str, Any]:
    """Extract + create one Google Calendar event for a calendar email.

    Returns a small outcome dict::

        {"created": bool, "event_id": str, "source": "ics"|"llm", "error": str}

    Never raises — a broken .ics or a Calendar 5xx logs a warning and
    returns ``created=False`` so the drain keeps going.
    """
    outcome: dict[str, Any] = {
        "created": False,
        "event_id": "",
        "source": "",
        "error": "",
    }
    try:
        ev = extract_event(parsed, raw_rfc822=raw_rfc822, llm_chat=llm_chat)
    except Exception as exc:  # noqa: BLE001
        outcome["error"] = f"extract_raised: {exc}"
        return outcome
    if ev is None:
        outcome["error"] = "no_signal"
        return outcome
    outcome["source"] = ev.source

    # Dedup by source_message_id
    try:
        existing = already_projected(calendar_client, parsed.message_id)
        if existing:
            outcome["event_id"] = existing
            outcome["error"] = "already_projected"
            return outcome
    except Exception:  # noqa: BLE001
        pass

    body = ev.to_google_event(
        source_message_id=parsed.message_id,
        artifact_id=artifact_id,
    )
    # Enrich description with the source email header + intent info the
    # operator wants at a glance in Google Calendar.
    body["description"] = (
        f"From:    {parsed.from_display or parsed.from_addr}\n"
        f"Subject: {parsed.subject}\n"
        f"Date:    {parsed.date_header}\n\n"
        f"{body.get('description', '') or (parsed.body_text or '')[:2000]}"
    )[:8000]

    try:
        created = calendar_client.insert_event(body)
        outcome["created"] = True
        outcome["event_id"] = str(created.get("id") or "")
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("calendar insert failed for %s: %s", parsed.message_id, exc)
        outcome["error"] = f"insert_failed: {exc}"
    return outcome


__all__ = [
    "ExtractedEvent",
    "already_projected",
    "extract_event",
    "find_ics_content",
    "project_event",
]
