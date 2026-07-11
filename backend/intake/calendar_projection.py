"""Universal calendar projection — any workcell can put a scheduled item
onto samushustleforge@'s primary calendar as a first-class planner entry.

Complements the two DIRECTIONAL calendar seams already in the intake stack:

* :mod:`backend.intake.calendar_ingest` — email inbox -> calendar (booking
  confirmations, .ics attachments, LLM-extracted meetings)
* :mod:`backend.intake.calendar_poller` — calendar -> Samus (operator-added
  events flow back as CRM artifacts + business events)

This module fills the third seam: **subsystems can push arbitrary planner
items** — product-deliverable deadlines, client-requested actions, campaign
milestones, hiring-campaign interview slots, weekly-report reminders — all
onto ONE shared calendar the operator uses as the company plan of record.

DESIGN PRINCIPLES

* One entry point (:func:`project_event`) with a typed ``projection_kind``
  so downstream analytics can slice by planner category without every
  caller reinventing tags.
* Every projected event carries ``extendedProperties.private`` metadata:

    source              : "samus.projection"   (distinguishes from
                                                calendar_ingest's
                                                "samus.intake")
    projection_kind     : deliverable | client_request | campaign_milestone |
                          engagement | meeting_request | hiring_milestone
    source_id           : opaque idempotency key (dedupe on re-runs)
    client_id           : optional — enables per-client planner views
    campaign_id         : optional — enables per-campaign planner views
    created_by_workcell : which workcell projected this

* Fail-soft: OAuth scope drift, Calendar API 5xx, or a bad time value logs
  a warning and returns ``{"created": False, "error": ...}``. Callers can
  ignore the return value; a failed projection never breaks the caller's
  primary work (creating a task, advancing a campaign, sending an email).
* Dedup by ``source_id``: a second call with the same ``source_id`` finds
  the existing event via the two-way poller's search and no-ops.

FUTURE HOOK POINTS (planned, not yet wired)

* Campaign orchestrator ``create_campaign`` — walk the template for
  deadline-shaped inputs and project each one.
* Hiring-campaign template (``staffing_hiring_campaign``, pending for
  <sample-client>) — every interview_slot node projects a calendar
  event on its scheduled time; the ``posting_close_at`` /
  ``shortlist_due_at`` / ``offer_window_end_at`` template inputs each
  land as ``[HIRING]`` deadlines.
* Client-correspondence intent ``requested_meeting`` — auto-project a
  TBD placeholder event so operator sees the request on the calendar
  in addition to the operator queue. (Wired here.)
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

from backend.common.config import get_settings

_LOG = logging.getLogger("samus.intake.calendar_projection")

ProjectionKind = Literal[
    "deliverable",  # product/service deliverable due to a client
    "client_request",  # something a client asked us to do by a date
    "campaign_milestone",  # campaign template's node deadline
    "engagement",  # recurring engagement rhythm (weekly report, ...)
    "meeting_request",  # client asked for a meeting (TBD placeholder)
    "hiring_milestone",  # staffing campaign — posting close, shortlist, offer window
]


# --- prefix vocabulary ----------------------------------------------------
# Kept in one place so all projected events surface a consistent title tag
# in Google Calendar (parallel to the [CATEGORY/INTENT] Gmail forward
# prefixes the operator already recognizes).

_PREFIXES: dict[str, str] = {
    "deliverable": "[DELIVERABLE]",
    "client_request": "[REQUEST]",
    "campaign_milestone": "[CAMPAIGN]",
    "engagement": "[ENGAGEMENT]",
    "meeting_request": "[MEETING-REQUEST]",
    "hiring_milestone": "[HIRING]",
}


def _format_title(kind: ProjectionKind, title: str, client_id: str = "") -> str:
    prefix = _PREFIXES.get(kind, "[EVENT]")
    client_tag = ""
    if client_id:
        client_tag = f" {client_id.replace('_', ' ').title()}:"
    return f"{prefix}{client_tag} {title.strip()}"[:200]


def _ensure_iso_utc(value: str) -> str:
    """Normalize an ISO 8601 string to a form Google Calendar accepts.

    Accepts ``2026-07-15T15:00:00Z``, ``2026-07-15T15:00:00+00:00``, and
    naive ``2026-07-15T15:00:00`` (treated as UTC). Returns the string
    Google's Events resource expects on ``start.dateTime`` /
    ``end.dateTime``.
    """
    if not value:
        return ""
    v = value.strip()
    try:
        dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _default_end(start_iso_utc: str, kind: ProjectionKind) -> str:
    """Default end time when the caller doesn't supply one.

    Deliverables + client requests + deadlines default to a 15-minute
    marker (calendar shows a slim block, not a full day, unless
    all_day=True is passed). Meeting requests default to 30 min.
    """
    try:
        start = datetime.fromisoformat(start_iso_utc.replace("Z", "+00:00"))
    except ValueError:
        return ""
    delta = timedelta(minutes=30 if kind == "meeting_request" else 15)
    return (start + delta).isoformat().replace("+00:00", "Z")


def _build_event(
    *,
    title: str,
    start_iso: str,
    end_iso: str,
    all_day: bool,
    description: str,
    location: str,
    projection_kind: ProjectionKind,
    client_id: str,
    campaign_id: str,
    source_id: str,
    source_workcell: str,
) -> dict[str, Any]:
    """Build the Google Calendar Events resource body."""
    body: dict[str, Any] = {
        "summary": _format_title(projection_kind, title, client_id),
        "description": (description or "")[:8000],
        "extendedProperties": {
            "private": {
                "source": "samus.projection",
                "projection_kind": projection_kind,
                "source_id": (source_id or "")[:1000],
                "client_id": client_id or "",
                "campaign_id": campaign_id or "",
                "created_by_workcell": source_workcell or "",
            }
        },
    }
    if location:
        body["location"] = location[:300]
    if all_day:
        # All-day events use the {"date": "YYYY-MM-DD"} shape.
        try:
            d = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
        except ValueError:
            return {}
        body["start"] = {"date": d.date().isoformat()}
        body["end"] = {"date": (d + timedelta(days=1)).date().isoformat()}
    else:
        body["start"] = {"dateTime": start_iso}
        body["end"] = {"dateTime": end_iso}
    return body


def _find_existing(client, source_id: str) -> str:
    """Return an existing projected event id with this source_id, or ''.

    Google Calendar's free-text ``q=`` only indexes summary / description /
    location / attendees — it does NOT search ``extendedProperties``. The
    right primitive is ``privateExtendedProperty=key=value``, which
    filters server-side by the exact key/value pair.
    """
    if not source_id:
        return ""
    try:
        events = client.list_events(
            calendar_id="primary",
            private_extended_property=[
                f"source_id={source_id}",
                "source=samus.projection",
            ],
            max_results=10,
        )
    except Exception:  # noqa: BLE001
        return ""
    for e in events:
        priv = (e.get("extendedProperties") or {}).get("private") or {}
        if priv.get("source") == "samus.projection" and priv.get("source_id") == source_id:
            return str(e.get("id") or "")
    return ""


def project_event(
    *,
    title: str,
    start_iso: str,
    projection_kind: ProjectionKind,
    end_iso: str = "",
    all_day: bool = False,
    description: str = "",
    location: str = "",
    client_id: str = "",
    campaign_id: str = "",
    source_id: str = "",
    source_workcell: str = "",
) -> dict[str, Any]:
    """Add one scheduled item to samushustleforge@'s primary calendar.

    Returns ``{"created": bool, "event_id": str, "error": str,
    "already_existed": bool}``. Never raises.

    * ``title``  — human-readable label (a projection prefix is added).
    * ``start_iso`` — ISO 8601 (Z or offset; naive treated as UTC).
    * ``end_iso`` — optional; sensible per-kind default when absent.
    * ``all_day=True`` — a deadline-style all-day marker instead of a
      15-min block.
    * ``source_id`` — opaque idempotency key. A second call with the
      same source_id no-ops (returns ``already_existed=True``).
    """
    outcome: dict[str, Any] = {
        "created": False,
        "event_id": "",
        "already_existed": False,
        "error": "",
    }
    if not title or not start_iso:
        outcome["error"] = "missing_required: title + start_iso"
        return outcome
    if projection_kind not in _PREFIXES:
        outcome["error"] = f"unknown_projection_kind: {projection_kind}"
        return outcome

    start_utc = _ensure_iso_utc(start_iso)
    if not start_utc:
        outcome["error"] = f"invalid_start_iso: {start_iso!r}"
        return outcome
    end_utc = (
        _ensure_iso_utc(end_iso)
        if end_iso
        else _default_end(
            start_utc,
            projection_kind,
        )
    )
    if not end_utc:
        outcome["error"] = "invalid_end_iso"
        return outcome

    body = _build_event(
        title=title,
        start_iso=start_utc,
        end_iso=end_utc,
        all_day=all_day,
        description=description,
        location=location,
        projection_kind=projection_kind,
        client_id=client_id,
        campaign_id=campaign_id,
        source_id=source_id,
        source_workcell=source_workcell,
    )
    if not body:
        outcome["error"] = "build_failed"
        return outcome

    settings = get_settings()
    if not (
        settings.gmail_inbox_email
        and settings.gmail_oauth_client_id
        and settings.gmail_oauth_client_secret
    ):
        outcome["error"] = "calendar_disabled_config_missing"
        return outcome

    try:
        from backend.intake.calendar_api_client import (
            CalendarApiClient,
            CalendarApiError,
        )
    except Exception as exc:  # noqa: BLE001
        outcome["error"] = f"client_import_failed: {exc}"
        return outcome

    try:
        with CalendarApiClient(
            client_id=settings.gmail_oauth_client_id,
            client_secret=settings.gmail_oauth_client_secret,
            token_path=Path(settings.gmail_oauth_token_path),
        ) as client:
            try:
                client.check_scope_or_raise()
            except CalendarApiError as exc:
                outcome["error"] = f"scope: {exc}"
                return outcome

            # Idempotency check.
            existing = _find_existing(client, source_id)
            if existing:
                outcome["already_existed"] = True
                outcome["event_id"] = existing
                return outcome

            created = client.insert_event(body)
            outcome["event_id"] = str(created.get("id") or "")
            outcome["created"] = True
    except Exception as exc:  # noqa: BLE001
        _LOG.warning(
            "projection %s -> %s failed: %s",
            projection_kind,
            title[:60],
            exc,
        )
        outcome["error"] = f"insert_failed: {exc}"
        return outcome

    # Also emit business event so the ledger has the scheduling entry
    # regardless of whether the two-way poller has picked the event up yet.
    try:
        from backend.common.business_events import (
            CALENDAR_EVENT_SCHEDULED,
            emit_business_event,
        )

        emit_business_event(
            CALENDAR_EVENT_SCHEDULED,
            workcell=source_workcell or "intake",
            campaign_id=campaign_id or None,
            metadata={
                "event_id": outcome["event_id"],
                "projection_kind": projection_kind,
                "client_id": client_id,
                "title": title,
                "start_iso": start_utc,
                "source_id": source_id,
                "origin": "samus",
            },
        )
    except Exception as exc:  # noqa: BLE001
        _LOG.debug("business event emit skipped: %s", exc)

    return outcome


__all__ = ["ProjectionKind", "project_event"]
