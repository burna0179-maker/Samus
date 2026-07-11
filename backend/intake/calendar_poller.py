"""Two-way sync: poll samushustleforge@'s calendar, ingest operator edits.

The one-way direction (email -> calendar) lives in
:mod:`backend.intake.calendar_ingest` and fires from the Gmail drain loop.

This module handles the OTHER direction: the operator adds/edits events on
samushustleforge@'s Google Calendar (via calendar.google.com or the mobile
app), and Samus needs to know about them so:

* every operator-entered "meeting at 3pm with Kerry" becomes a CRM artifact
  the client-thread + business-event ledger can reference;
* an event that has finished (end time < now) emits a
  ``calendar.event_completed`` business event so the journey view shows
  what actually happened, not just what was scheduled.

DEDUP + IDEMPOTENCY

The polled events are tagged locally with a ledger of processed event IDs
(``~/data/telemetry/calendar_events_seen.jsonl``). A first-pass event
becomes an artifact + ``calendar.event_scheduled`` event. If the same
event's end time later passes, a subsequent pass emits
``calendar.event_completed`` and updates the ledger row so we never
double-emit either signal.

We DO NOT re-ingest events that Samus itself created (they carry
``extendedProperties.private.source == "samus.intake"`` — the ingest
side-tag). Those are already durably in CRM from the projection path.

CLIENT LINKING

If the event's summary or attendees mention a known client (via
:mod:`backend.crm.client_directory`), the artifact + business event
carry the client_id so a per-client planner view naturally includes
meetings.

FAIL-SOFT

Every stage catches, logs, and continues. A Calendar API 5xx / scope
drift / stale token yields ``PollPassResult(enabled=False,
connect_error=...)`` — the outer task loop keeps ticking.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from backend.common.config import get_settings

_LOG = logging.getLogger("samus.intake.calendar_poller")

# Ledger path — same convention as gmail_poller.seen_message_ids.
_LEDGER_ENV = "SAMUS_CALENDAR_EVENTS_SEEN_PATH"
_LEDGER_DEFAULT = "/opt/samus/data/telemetry/calendar_events_seen.jsonl"

# Poll window: past N hours + next N days. Small enough to keep API cost
# minimal, large enough that a 5-min interval never misses an event.
_PAST_HOURS = 24
_FUTURE_DAYS = 30


# --- ledger --------------------------------------------------------------


def _ledger_path() -> Path:
    return Path(os.environ.get(_LEDGER_ENV) or _LEDGER_DEFAULT)


def _load_ledger() -> dict[str, dict[str, Any]]:
    """Return ``{event_id -> row}``. Empty dict on any read failure."""
    p = _ledger_path()
    if not p.exists():
        return {}
    out: dict[str, dict[str, Any]] = {}
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            ev_id = str(row.get("event_id") or "").strip()
            if ev_id:
                out[ev_id] = row  # last-write-wins under duplicate lines
    except OSError as exc:
        _LOG.warning("calendar ledger read failed: %s", exc)
    return out


def _append_ledger_row(row: dict[str, Any]) -> None:
    """Append one JSON line. Never raises."""
    p = _ledger_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError as exc:
        _LOG.warning("calendar ledger append failed: %s", exc)


# --- time helpers --------------------------------------------------------


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _parse_calendar_dt(node: dict[str, Any]) -> datetime | None:
    """Turn one Calendar API date/dateTime dict into an aware UTC datetime.

    Google's events resource uses ``{"dateTime": "..."}`` for timed
    events and ``{"date": "YYYY-MM-DD"}`` for all-day. Both are supported.
    """
    if not isinstance(node, dict):
        return None
    dt_str = node.get("dateTime")
    if isinstance(dt_str, str) and dt_str:
        try:
            parsed = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except ValueError:
            return None
    date_str = node.get("date")
    if isinstance(date_str, str) and date_str:
        try:
            return datetime.strptime(date_str, "%Y-%m-%d").replace(
                tzinfo=timezone.utc,
            )
        except ValueError:
            return None
    return None


def _is_samus_created(event: dict[str, Any]) -> bool:
    """True if the event carries our own source-tag from calendar_ingest."""
    ext = (event.get("extendedProperties") or {}).get("private") or {}
    return str(ext.get("source") or "") == "samus.intake"


def _client_id_for_event(event: dict[str, Any]) -> str:
    """Best-effort: match the event to a known client via summary/attendee.

    Returns ``""`` when no client hit — the artifact still writes but as
    a generic operator-planner entry.
    """
    try:
        from backend.crm.client_directory import all_known_clients, lookup_client
    except Exception:  # noqa: BLE001
        return ""
    known = all_known_clients()
    if not known:
        return ""

    haystack_parts: list[str] = []
    for key in ("summary", "description", "location"):
        v = event.get(key)
        if isinstance(v, str) and v:
            haystack_parts.append(v.lower())
    haystack = " ".join(haystack_parts)

    for kc in known:
        # slug like "sample_school" -> "conquerors christian school"
        needle = kc.client_id.replace("_", " ")
        if needle and needle in haystack:
            return kc.client_id
        # exact email in the text
        if kc.email and kc.email in haystack:
            return kc.client_id

    # Fall back to attendee email match.
    attendees = event.get("attendees") or []
    if isinstance(attendees, list):
        for a in attendees:
            if not isinstance(a, dict):
                continue
            email = str(a.get("email") or "").strip().lower()
            if not email:
                continue
            kc = lookup_client(email)
            if kc is not None:
                return kc.client_id
    return ""


# --- ingest one event ----------------------------------------------------


def _create_artifact_for_event(
    event: dict[str, Any],
    client_id: str,
) -> str:
    """Create a CRM ``calendar_event`` artifact. Returns artifact_id or ''."""
    try:
        from backend.crm.models import CreateArtifactRequest
        from backend.crm.service import create_artifact
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("crm import failed: %s", exc)
        return ""

    summary = str(event.get("summary") or "(no title)").strip()[:200]
    start_dt = _parse_calendar_dt(event.get("start") or {})
    end_dt = _parse_calendar_dt(event.get("end") or {})
    location = str(event.get("location") or "")
    description = str(event.get("description") or "")

    owner_kind = "client" if client_id else "planner"
    owner_id = client_id or "samus.calendar"

    inline = {
        "event_id": event.get("id"),
        "summary": summary,
        "start_iso": start_dt.isoformat().replace("+00:00", "Z") if start_dt else "",
        "end_iso": end_dt.isoformat().replace("+00:00", "Z") if end_dt else "",
        "location": location[:500],
        "description": description[:2000],
        "html_link": event.get("htmlLink"),
        "hangout_link": event.get("hangoutLink"),
        "attendees": [
            {"email": a.get("email"), "response": a.get("responseStatus")}
            for a in (event.get("attendees") or [])
            if isinstance(a, dict) and a.get("email")
        ][:20],
        "extended_properties": (event.get("extendedProperties") or {}).get("private") or {},
    }
    try:
        req = CreateArtifactRequest(
            kind="calendar_event",
            owner_entity_kind=owner_kind,
            owner_entity_id=owner_id,
            title=summary or "(no title)",
            inline_data=inline,
            source="intake.calendar_poller",
            created_by="intake.calendar_poller",
        )
        res = create_artifact(req)
        return res.artifact_id or ""
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("calendar_event artifact write failed: %s", exc)
        return ""


def _emit_scheduled(
    event: dict[str, Any],
    *,
    client_id: str,
    artifact_id: str,
    origin: str,
) -> None:
    try:
        from backend.common.business_events import (
            CALENDAR_EVENT_SCHEDULED,
            emit_business_event,
        )

        start_dt = _parse_calendar_dt(event.get("start") or {})
        emit_business_event(
            CALENDAR_EVENT_SCHEDULED,
            workcell="intake",
            metadata={
                "event_id": event.get("id"),
                "summary": event.get("summary"),
                "start_iso": start_dt.isoformat().replace("+00:00", "Z") if start_dt else "",
                "location": event.get("location"),
                "client_id": client_id,
                "artifact_id": artifact_id,
                "origin": origin,  # "operator" | "samus"
            },
        )
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("emit calendar.event_scheduled failed: %s", exc)


def _emit_completed(event: dict[str, Any], *, client_id: str, artifact_id: str) -> None:
    try:
        from backend.common.business_events import (
            CALENDAR_EVENT_COMPLETED,
            emit_business_event,
        )

        end_dt = _parse_calendar_dt(event.get("end") or {})
        emit_business_event(
            CALENDAR_EVENT_COMPLETED,
            workcell="intake",
            metadata={
                "event_id": event.get("id"),
                "summary": event.get("summary"),
                "end_iso": end_dt.isoformat().replace("+00:00", "Z") if end_dt else "",
                "client_id": client_id,
                "artifact_id": artifact_id,
            },
        )
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("emit calendar.event_completed failed: %s", exc)


# --- one poll pass -------------------------------------------------------


@dataclass
class PollPassResult:
    enabled: bool
    fetched: int = 0
    ingested: int = 0
    scheduled_emitted: int = 0
    completed_emitted: int = 0
    already_seen: int = 0
    skipped_samus_owned: int = 0
    connect_error: str = ""
    events: list[dict[str, Any]] = field(default_factory=list)


def poll_calendar_once(
    *,
    client_factory=None,
    now: datetime | None = None,
) -> PollPassResult:
    """One poll pass over samushustleforge@'s calendar. Never raises.

    ``client_factory`` is for tests — a zero-arg callable returning a
    context manager exposing ``check_scope_or_raise`` and
    ``list_events``. Production callers leave it ``None`` and this
    function builds a :class:`CalendarApiClient` from settings.
    """
    settings = get_settings()
    if not (
        settings.gmail_inbox_email
        and settings.gmail_oauth_client_id
        and settings.gmail_oauth_client_secret
    ):
        _LOG.info("calendar poll disabled: gmail oauth secrets unset")
        return PollPassResult(enabled=False)

    ts_now = now or _now_utc()

    if client_factory is None:
        from backend.intake.calendar_api_client import CalendarApiClient

        def client_factory():  # type: ignore[no-redef]
            return CalendarApiClient(
                client_id=settings.gmail_oauth_client_id,
                client_secret=settings.gmail_oauth_client_secret,
                token_path=Path(settings.gmail_oauth_token_path),
            )

    out = PollPassResult(enabled=True)
    ledger = _load_ledger()

    try:
        with client_factory() as client:
            try:
                client.check_scope_or_raise()
            except Exception as exc:  # noqa: BLE001
                out.connect_error = f"scope_missing: {exc}"
                _LOG.info("calendar poll scope-drift: %s", exc)
                return out

            time_min = (
                (ts_now - timedelta(hours=_PAST_HOURS))
                .isoformat()
                .replace(
                    "+00:00",
                    "Z",
                )
            )
            time_max = (
                (ts_now + timedelta(days=_FUTURE_DAYS))
                .isoformat()
                .replace(
                    "+00:00",
                    "Z",
                )
            )
            events = client.list_events_range(
                calendar_id="primary",
                time_min=time_min,
                time_max=time_max,
                max_results=100,
            )
            out.fetched = len(events)

            for event in events:
                event_id = str(event.get("id") or "").strip()
                if not event_id:
                    continue

                # 1) Samus-created events: already durable, don't re-ingest.
                #    Still handle completion emission below.
                samus_owned = _is_samus_created(event)

                prior = ledger.get(event_id)
                end_dt = _parse_calendar_dt(event.get("end") or {})
                completed_now = bool(end_dt and end_dt < ts_now)

                if prior:
                    # We've seen this event on a previous pass. Only re-emit
                    # on completion transition (was scheduled, now finished).
                    if completed_now and not prior.get("completed_emitted"):
                        _emit_completed(
                            event,
                            client_id=prior.get("client_id") or "",
                            artifact_id=prior.get("artifact_id") or "",
                        )
                        prior["completed_emitted"] = True
                        prior["completed_at_ts"] = ts_now.isoformat().replace(
                            "+00:00",
                            "Z",
                        )
                        _append_ledger_row(prior)
                        out.completed_emitted += 1
                    else:
                        out.already_seen += 1
                    continue

                # 2) NEW event this pass.
                if samus_owned:
                    # Samus already ingested this via the email path. Just
                    # record it in the ledger so completion emission fires
                    # later.
                    ledger_row = {
                        "event_id": event_id,
                        "origin": "samus",
                        "client_id": "",
                        "artifact_id": (
                            (event.get("extendedProperties") or {})
                            .get("private", {})
                            .get("artifact_id", "")
                        ),
                        "seen_at_ts": ts_now.isoformat().replace("+00:00", "Z"),
                        "completed_emitted": False,
                    }
                    _append_ledger_row(ledger_row)
                    out.skipped_samus_owned += 1
                    continue

                # 3) Operator-created event: ingest.
                client_id = _client_id_for_event(event)
                artifact_id = _create_artifact_for_event(event, client_id)
                _emit_scheduled(
                    event,
                    client_id=client_id,
                    artifact_id=artifact_id,
                    origin="operator",
                )
                out.scheduled_emitted += 1
                out.ingested += 1
                out.events.append(event)

                ledger_row = {
                    "event_id": event_id,
                    "origin": "operator",
                    "client_id": client_id,
                    "artifact_id": artifact_id,
                    "seen_at_ts": ts_now.isoformat().replace("+00:00", "Z"),
                    "completed_emitted": False,
                }
                # If the event is ALREADY over by the time we first see it,
                # also emit completion so we never miss it.
                if completed_now:
                    _emit_completed(
                        event,
                        client_id=client_id,
                        artifact_id=artifact_id,
                    )
                    ledger_row["completed_emitted"] = True
                    ledger_row["completed_at_ts"] = ts_now.isoformat().replace(
                        "+00:00",
                        "Z",
                    )
                    out.completed_emitted += 1
                _append_ledger_row(ledger_row)
    except Exception as exc:  # noqa: BLE001 — never crash the task
        out.connect_error = f"unexpected: {exc}"
        _LOG.warning("calendar poller unexpected error: %s", exc)

    return out


__all__ = [
    "PollPassResult",
    "poll_calendar_once",
]
