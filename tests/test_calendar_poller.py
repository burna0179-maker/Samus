"""Tests for backend.intake.calendar_poller — two-way sync."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from backend.intake.calendar_poller import (
    _client_id_for_event,
    _is_samus_created,
    _parse_calendar_dt,
    poll_calendar_once,
)


_NOW = datetime(2026, 7, 11, 10, 0, 0, tzinfo=timezone.utc)


def _mk_event(
    *,
    event_id: str = "evt-1",
    summary: str = "Team standup",
    start_iso: str = "2026-07-12T15:00:00Z",
    end_iso: str = "2026-07-12T15:30:00Z",
    location: str = "",
    description: str = "",
    private_source: str = "",
    attendees=None,
    artifact_id: str = "",
) -> dict:
    ev = {
        "id": event_id,
        "summary": summary,
        "start": {"dateTime": start_iso},
        "end": {"dateTime": end_iso},
        "location": location,
        "description": description,
    }
    if attendees:
        ev["attendees"] = attendees
    priv: dict = {}
    if private_source:
        priv["source"] = private_source
    if artifact_id:
        priv["artifact_id"] = artifact_id
    if priv:
        ev["extendedProperties"] = {"private": priv}
    return ev


@pytest.fixture()
def _isolated_ledger(tmp_path, monkeypatch):
    """Point the poller at a tmp ledger; verified in tests via _load_ledger."""
    p = tmp_path / "calendar_events_seen.jsonl"
    monkeypatch.setenv("SAMUS_CALENDAR_EVENTS_SEEN_PATH", str(p))
    return p


@pytest.fixture()
def _stub_settings(monkeypatch):
    """Settings with all-OAuth-secrets present so the poll enters the loop."""
    fake = MagicMock()
    fake.gmail_inbox_email = "samushustleforge@gmail.com"
    fake.gmail_oauth_client_id = "cid"
    fake.gmail_oauth_client_secret = "csec"
    fake.gmail_oauth_token_path = "/tmp/token.json"
    monkeypatch.setattr(
        "backend.intake.calendar_poller.get_settings",
        lambda: fake,
    )


def _client_factory(events, scope_ok=True):
    """Build a factory returning a mock CalendarApiClient context manager."""

    def _factory():
        c = MagicMock()
        if not scope_ok:
            c.__enter__ = lambda self=c: (_ for _ in ()).throw(
                RuntimeError("cannot enter — should not happen in these tests"),
            )
        c.check_scope_or_raise = MagicMock(
            side_effect=None if scope_ok else RuntimeError("calendar_scope_missing"),
        )
        c.list_events_range = MagicMock(return_value=events)
        c.__enter__ = MagicMock(return_value=c)
        c.__exit__ = MagicMock(return_value=None)
        return c

    return _factory


# --- pure helpers ---------------------------------------------------------


def test_parse_dt_utc_z():
    got = _parse_calendar_dt({"dateTime": "2026-07-12T15:30:00Z"})
    assert got == datetime(2026, 7, 12, 15, 30, tzinfo=timezone.utc)


def test_parse_dt_offset():
    got = _parse_calendar_dt({"dateTime": "2026-07-12T08:30:00-07:00"})
    assert got == datetime(2026, 7, 12, 15, 30, tzinfo=timezone.utc)


def test_parse_dt_all_day():
    got = _parse_calendar_dt({"date": "2026-07-12"})
    assert got == datetime(2026, 7, 12, tzinfo=timezone.utc)


def test_parse_dt_returns_none_on_junk():
    assert _parse_calendar_dt({}) is None
    assert _parse_calendar_dt({"dateTime": "not-iso"}) is None


def test_is_samus_created_true_when_source_tag_present():
    ev = _mk_event(private_source="samus.intake")
    assert _is_samus_created(ev) is True


def test_is_samus_created_false_when_no_extended_properties():
    ev = _mk_event()
    assert _is_samus_created(ev) is False


def test_is_samus_created_false_when_source_is_other():
    ev = _mk_event(private_source="operator_import")
    assert _is_samus_created(ev) is False


def test_client_id_matched_from_summary(monkeypatch):
    from backend.crm.client_directory import KnownClient

    kc = KnownClient(
        email="<client-email>@example.com",
        client_id="sample_school",
        campaign_id="sample_school_phased_2026",
        template_id="school_phased_maintenance",
        role="approval_contact",
        display_name="Kerry Brown",
    )
    monkeypatch.setattr(
        "backend.crm.client_directory.all_known_clients",
        lambda: [kc],
    )
    monkeypatch.setattr(
        "backend.crm.client_directory.lookup_client",
        lambda email: kc if email.lower() == kc.email else None,
    )
    ev = _mk_event(summary="Sync w/ Sample School")
    assert _client_id_for_event(ev) == "sample_school"


def test_client_id_matched_from_attendee(monkeypatch):
    from backend.crm.client_directory import KnownClient

    kc = KnownClient(
        email="<client-email>@example.com",
        client_id="sample_school",
        campaign_id="c",
        template_id="t",
        role="approval_contact",
    )
    monkeypatch.setattr(
        "backend.crm.client_directory.all_known_clients",
        lambda: [kc],
    )
    monkeypatch.setattr(
        "backend.crm.client_directory.lookup_client",
        lambda email: kc if email.lower() == kc.email else None,
    )
    ev = _mk_event(
        summary="Weekly call",
        attendees=[{"email": "<client-email>@example.com"}],
    )
    assert _client_id_for_event(ev) == "sample_school"


def test_client_id_empty_when_no_match(monkeypatch):
    monkeypatch.setattr(
        "backend.crm.client_directory.all_known_clients",
        lambda: [],
    )
    monkeypatch.setattr(
        "backend.crm.client_directory.lookup_client",
        lambda e: None,
    )
    ev = _mk_event(summary="Just a solo work block")
    assert _client_id_for_event(ev) == ""


# --- poll_calendar_once orchestration ------------------------------------


def test_poll_disabled_when_settings_missing(_isolated_ledger, monkeypatch):
    fake = MagicMock()
    fake.gmail_inbox_email = ""
    fake.gmail_oauth_client_id = ""
    fake.gmail_oauth_client_secret = ""
    monkeypatch.setattr(
        "backend.intake.calendar_poller.get_settings",
        lambda: fake,
    )
    out = poll_calendar_once()
    assert out.enabled is False


def test_poll_scope_missing_reports_connect_error(_isolated_ledger, _stub_settings):
    out = poll_calendar_once(client_factory=_client_factory([], scope_ok=False))
    assert out.enabled is True
    assert "scope_missing" in out.connect_error


def test_poll_ingests_new_operator_event(_isolated_ledger, _stub_settings, monkeypatch):
    event = _mk_event(event_id="op-1", summary="Kickoff meeting")
    monkeypatch.setattr(
        "backend.intake.calendar_poller._create_artifact_for_event",
        lambda ev, cid: "ar_new",
    )
    scheduled_calls = []
    monkeypatch.setattr(
        "backend.intake.calendar_poller._emit_scheduled",
        lambda ev, *, client_id, artifact_id, origin: scheduled_calls.append(
            {"origin": origin, "artifact_id": artifact_id},
        ),
    )
    monkeypatch.setattr(
        "backend.intake.calendar_poller._emit_completed",
        MagicMock(),
    )

    out = poll_calendar_once(
        client_factory=_client_factory([event]),
        now=_NOW,
    )
    assert out.enabled is True
    assert out.fetched == 1
    assert out.ingested == 1
    assert out.scheduled_emitted == 1
    assert out.completed_emitted == 0
    assert len(scheduled_calls) == 1
    assert scheduled_calls[0]["origin"] == "operator"
    assert scheduled_calls[0]["artifact_id"] == "ar_new"


def test_poll_skips_samus_created_events(_isolated_ledger, _stub_settings, monkeypatch):
    event = _mk_event(event_id="s-1", private_source="samus.intake", artifact_id="ar_pre")
    monkeypatch.setattr(
        "backend.intake.calendar_poller._create_artifact_for_event",
        MagicMock(side_effect=AssertionError("must not be called for samus-owned")),
    )
    monkeypatch.setattr(
        "backend.intake.calendar_poller._emit_scheduled",
        MagicMock(side_effect=AssertionError("must not fire for samus-owned")),
    )
    out = poll_calendar_once(
        client_factory=_client_factory([event]),
        now=_NOW,
    )
    assert out.skipped_samus_owned == 1
    assert out.ingested == 0
    assert out.scheduled_emitted == 0


def test_poll_emits_completion_on_second_pass(_isolated_ledger, _stub_settings, monkeypatch):
    # Event ends BEFORE the poll's now — should emit completion immediately.
    past_end = "2026-07-11T09:00:00Z"  # 1 hour before _NOW
    past_start = "2026-07-11T08:30:00Z"
    event = _mk_event(event_id="past-1", start_iso=past_start, end_iso=past_end)
    monkeypatch.setattr(
        "backend.intake.calendar_poller._create_artifact_for_event",
        lambda ev, cid: "ar_past",
    )
    scheduled_calls: list = []
    completed_calls: list = []
    monkeypatch.setattr(
        "backend.intake.calendar_poller._emit_scheduled",
        lambda ev, *, client_id, artifact_id, origin: scheduled_calls.append(origin),
    )
    monkeypatch.setattr(
        "backend.intake.calendar_poller._emit_completed",
        lambda ev, *, client_id, artifact_id: completed_calls.append(artifact_id),
    )
    out = poll_calendar_once(
        client_factory=_client_factory([event]),
        now=_NOW,
    )
    assert out.scheduled_emitted == 1
    assert out.completed_emitted == 1
    assert scheduled_calls == ["operator"]
    assert completed_calls == ["ar_past"]


def test_second_pass_does_not_re_ingest_or_re_emit(_isolated_ledger, _stub_settings, monkeypatch):
    event = _mk_event(event_id="stable-1")
    monkeypatch.setattr(
        "backend.intake.calendar_poller._create_artifact_for_event",
        lambda ev, cid: "ar_stable",
    )
    monkeypatch.setattr(
        "backend.intake.calendar_poller._emit_scheduled",
        MagicMock(),
    )
    monkeypatch.setattr(
        "backend.intake.calendar_poller._emit_completed",
        MagicMock(),
    )
    # First pass: ingests.
    first = poll_calendar_once(
        client_factory=_client_factory([event]),
        now=_NOW,
    )
    assert first.ingested == 1
    # Second pass same event, same _NOW: already-seen path.
    second = poll_calendar_once(
        client_factory=_client_factory([event]),
        now=_NOW,
    )
    assert second.ingested == 0
    assert second.already_seen == 1
    assert second.completed_emitted == 0


def test_completion_fires_on_delayed_second_pass(_isolated_ledger, _stub_settings, monkeypatch):
    # Event ends slightly in the future when first seen; passes end time on
    # the second poll.
    event = _mk_event(
        event_id="future-then-past",
        start_iso="2026-07-11T10:15:00Z",
        end_iso="2026-07-11T10:45:00Z",
    )
    monkeypatch.setattr(
        "backend.intake.calendar_poller._create_artifact_for_event",
        lambda ev, cid: "ar_delayed",
    )
    monkeypatch.setattr(
        "backend.intake.calendar_poller._emit_scheduled",
        MagicMock(),
    )
    completed_calls: list = []
    monkeypatch.setattr(
        "backend.intake.calendar_poller._emit_completed",
        lambda ev, *, client_id, artifact_id: completed_calls.append(artifact_id),
    )

    first = poll_calendar_once(
        client_factory=_client_factory([event]),
        now=_NOW,  # 10:00Z — event still upcoming
    )
    assert first.scheduled_emitted == 1
    assert first.completed_emitted == 0

    # Second pass 1 hour later — event now finished
    later = _NOW + timedelta(hours=1)
    second = poll_calendar_once(
        client_factory=_client_factory([event]),
        now=later,
    )
    assert second.already_seen == 0
    assert second.completed_emitted == 1
    assert completed_calls == ["ar_delayed"]
