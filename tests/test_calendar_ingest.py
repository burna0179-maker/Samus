"""Tests for backend.intake.calendar_ingest."""

from __future__ import annotations

from unittest.mock import MagicMock

from backend.intake.calendar_ingest import (
    ExtractedEvent,
    _extract_first_vevent,
    _parse_ical_dt,
    already_projected,
    extract_event,
    find_ics_content,
    project_event,
)
from backend.intake.gmail_poller import ParsedInboundEmail


def _mk_parsed(**over) -> ParsedInboundEmail:
    base = {
        "message_id": "<meet-2026-07-15@calendly>",
        "from_addr": "noreply@calendly.com",
        "from_display": "Calendly <noreply@calendly.com>",
        "to_addrs": ["samushustleforge@gmail.com"],
        "subject": "New booking: Discovery call",
        "date_header": "Fri, 11 Jul 2026 09:00:00 -0700",
        "body_text": "Alex, you have a new booking.",
        "body_format": "text",
        "attachment_names": [],
    }
    base.update(over)
    return ParsedInboundEmail(**base)


# --- _parse_ical_dt --------------------------------------------------------


def test_parse_utc_z_format():
    assert _parse_ical_dt("20260715T193000Z") == "2026-07-15T19:30:00Z"


def test_parse_all_day_date():
    assert _parse_ical_dt("20260715") == "2026-07-15T00:00:00Z"


def test_parse_tzid_format_returns_iso_string():
    # We emit naive ISO for TZID variants; Google infers timezone from
    # its own top-level field or from the datetime. Just verify shape.
    got = _parse_ical_dt("TZID=America/Los_Angeles:20260715T123000")
    assert got.startswith("2026-07-15T12:30:00")


def test_parse_malformed_returns_empty():
    assert _parse_ical_dt("garbage") == ""


# --- _extract_first_vevent -------------------------------------------------

_ICS_WITH_MEET = """\
BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Calendly//Calendly Meeting Notice//EN
METHOD:REQUEST
BEGIN:VEVENT
UID:calendly-abc-2026
DTSTAMP:20260710T160000Z
DTSTART:20260715T193000Z
DTEND:20260715T200000Z
SUMMARY:Discovery Call: Alex + Sam Vendor
LOCATION:Google Meet (link in description)
DESCRIPTION:15-min discovery call.\\nTopic: Q3 goals.
END:VEVENT
END:VCALENDAR
"""


def test_extract_vevent_happy_path():
    ev = _extract_first_vevent(_ICS_WITH_MEET)
    assert ev is not None
    assert ev.summary == "Discovery Call: Alex + Sam Vendor"
    assert ev.start_iso == "2026-07-15T19:30:00Z"
    assert ev.end_iso == "2026-07-15T20:00:00Z"
    assert "Google Meet" in ev.location
    assert "discovery call" in ev.description.lower()
    assert ev.source == "ics"
    assert ev.is_valid() is True


def test_extract_vevent_defaults_end_to_start_plus_one_hour():
    ics = """\
BEGIN:VCALENDAR
BEGIN:VEVENT
UID:x
DTSTART:20260715T093000Z
SUMMARY:Standup
END:VEVENT
END:VCALENDAR
"""
    ev = _extract_first_vevent(ics)
    assert ev is not None
    assert ev.start_iso == "2026-07-15T09:30:00Z"
    assert ev.end_iso == "2026-07-15T10:30:00Z"


def test_extract_vevent_returns_none_without_dtstart():
    ics = """\
BEGIN:VCALENDAR
BEGIN:VEVENT
UID:x
SUMMARY:Missing start
END:VEVENT
END:VCALENDAR
"""
    assert _extract_first_vevent(ics) is None


def test_extract_vevent_returns_none_without_summary():
    ics = """\
BEGIN:VCALENDAR
BEGIN:VEVENT
UID:x
DTSTART:20260715T093000Z
END:VEVENT
END:VCALENDAR
"""
    assert _extract_first_vevent(ics) is None


def test_extract_vevent_returns_none_when_no_vevent():
    assert _extract_first_vevent("") is None
    assert _extract_first_vevent("BEGIN:VCALENDAR\nEND:VCALENDAR\n") is None


def test_extract_vevent_unescapes_summary():
    ics = """\
BEGIN:VCALENDAR
BEGIN:VEVENT
UID:x
DTSTART:20260715T093000Z
SUMMARY:Team lunch\\, 12pm
END:VEVENT
END:VCALENDAR
"""
    ev = _extract_first_vevent(ics)
    assert ev is not None
    assert ev.summary == "Team lunch, 12pm"


# --- find_ics_content (raw RFC822) -----------------------------------------


def _mk_rfc822_with_ics(ics_body: str = _ICS_WITH_MEET) -> bytes:
    return (
        "From: noreply@calendly.com\r\n"
        "To: samushustleforge@gmail.com\r\n"
        "Subject: New booking\r\n"
        'Content-Type: multipart/mixed; boundary="BOUNDARY"\r\n'
        "MIME-Version: 1.0\r\n"
        "\r\n"
        "--BOUNDARY\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n"
        "\r\n"
        "You have a new booking.\r\n"
        "--BOUNDARY\r\n"
        'Content-Type: text/calendar; charset=utf-8; name="invite.ics"\r\n'
        "Content-Transfer-Encoding: 7bit\r\n"
        "\r\n"
        f"{ics_body}\r\n"
        "--BOUNDARY--\r\n"
    ).encode("utf-8")


def test_find_ics_from_multipart_body():
    raw = _mk_rfc822_with_ics()
    ics = find_ics_content(raw)
    assert ics is not None
    assert "BEGIN:VEVENT" in ics


def test_find_ics_returns_none_when_absent():
    plain = ("From: a@b\r\nTo: c@d\r\nSubject: not calendar\r\n\r\nNo ics here.\r\n").encode(
        "utf-8"
    )
    assert find_ics_content(plain) is None


def test_find_ics_returns_none_on_empty_input():
    assert find_ics_content(b"") is None


# --- extract_event (top-level) --------------------------------------------


def test_extract_event_prefers_ics_over_llm():
    raw = _mk_rfc822_with_ics()
    parsed = _mk_parsed()

    def _llm_should_not_be_called(sys, user, **kw):
        raise AssertionError("LLM was called despite valid .ics")

    ev = extract_event(parsed, raw_rfc822=raw, llm_chat=_llm_should_not_be_called)
    assert ev is not None
    assert ev.source == "ics"
    assert ev.summary == "Discovery Call: Alex + Sam Vendor"


def test_extract_event_falls_back_to_llm_when_no_ics():
    parsed = _mk_parsed(body_text="You are booked Wed Jul 15 at 3pm PT with Sam.")
    llm_response = (
        '{"summary": "Discovery call", '
        '"start": "2026-07-15T15:00:00-07:00", '
        '"end": "2026-07-15T15:30:00-07:00", '
        '"location": "", "description": ""}'
    )
    llm_stub = MagicMock(return_value=llm_response)
    ev = extract_event(parsed, raw_rfc822=None, llm_chat=llm_stub)
    assert ev is not None
    assert ev.source == "llm"
    assert ev.summary == "Discovery call"


def test_extract_event_returns_none_when_llm_empty():
    parsed = _mk_parsed(body_text="just a normal email")
    llm_stub = MagicMock(return_value="")
    assert extract_event(parsed, raw_rfc822=None, llm_chat=llm_stub) is None


def test_extract_event_returns_none_when_llm_reports_no_event():
    parsed = _mk_parsed(body_text="normal email")
    llm_stub = MagicMock(return_value='{"summary": "", "start": "", "end": ""}')
    assert extract_event(parsed, raw_rfc822=None, llm_chat=llm_stub) is None


# --- to_google_event shape ------------------------------------------------


def test_to_google_event_carries_source_message_id():
    ev = ExtractedEvent(
        summary="Test",
        start_iso="2026-07-15T09:00:00Z",
        end_iso="2026-07-15T10:00:00Z",
        source="ics",
    )
    body = ev.to_google_event(source_message_id="<abc@test>", artifact_id="ar_1")
    assert body["summary"] == "Test"
    assert body["start"]["dateTime"] == "2026-07-15T09:00:00Z"
    priv = body["extendedProperties"]["private"]
    assert priv["source_message_id"] == "<abc@test>"
    assert priv["source_extractor"] == "ics"
    assert priv["artifact_id"] == "ar_1"


# --- already_projected + project_event orchestration ---------------------


def test_already_projected_finds_existing_event():
    client = MagicMock()
    client.list_events.return_value = [
        {
            "id": "cal-evt-1",
            "extendedProperties": {"private": {"source_message_id": "<abc@test>"}},
        },
    ]
    assert already_projected(client, "<abc@test>") == "cal-evt-1"


def test_already_projected_returns_empty_when_no_match():
    client = MagicMock()
    client.list_events.return_value = []
    assert already_projected(client, "<abc@test>") == ""


def test_already_projected_returns_empty_when_client_raises():
    client = MagicMock()
    client.list_events.side_effect = RuntimeError("api down")
    # Silently returns empty — dedup is best-effort
    assert already_projected(client, "<abc@test>") == ""


def test_project_event_creates_when_new():
    client = MagicMock()
    client.list_events.return_value = []
    client.insert_event.return_value = {"id": "cal-evt-new"}
    parsed = _mk_parsed()
    raw = _mk_rfc822_with_ics()

    outcome = project_event(client, parsed, raw)
    assert outcome["created"] is True
    assert outcome["event_id"] == "cal-evt-new"
    assert outcome["source"] == "ics"
    assert outcome["error"] == ""
    client.insert_event.assert_called_once()


def test_project_event_skips_when_already_projected():
    client = MagicMock()
    client.list_events.return_value = [
        {
            "id": "cal-evt-dup",
            "extendedProperties": {
                "private": {"source_message_id": "<meet-2026-07-15@calendly>"},
            },
        }
    ]
    parsed = _mk_parsed()
    raw = _mk_rfc822_with_ics()

    outcome = project_event(client, parsed, raw)
    assert outcome["created"] is False
    assert outcome["event_id"] == "cal-evt-dup"
    assert outcome["error"] == "already_projected"
    client.insert_event.assert_not_called()


def test_project_event_returns_no_signal_when_extract_empty():
    client = MagicMock()
    client.list_events.return_value = []
    parsed = _mk_parsed(body_text="no meeting here")
    # No .ics, LLM stub returns empty
    llm_stub = MagicMock(return_value="")

    outcome = project_event(client, parsed, raw_rfc822=None, llm_chat=llm_stub)
    assert outcome["created"] is False
    assert outcome["error"] == "no_signal"
    client.insert_event.assert_not_called()


def test_project_event_swallows_insert_failure():
    client = MagicMock()
    client.list_events.return_value = []
    client.insert_event.side_effect = RuntimeError("calendar 503")
    parsed = _mk_parsed()
    raw = _mk_rfc822_with_ics()

    outcome = project_event(client, parsed, raw)
    assert outcome["created"] is False
    assert outcome["error"].startswith("insert_failed")
