"""Tests for backend.intake.calendar_projection — universal planner API."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from backend.intake.calendar_projection import (
    _default_end,
    _ensure_iso_utc,
    _format_title,
    project_event,
)


def _stub_settings():
    """Return a settings stub that satisfies the enabled-config check."""
    s = MagicMock()
    s.gmail_inbox_email = "samushustleforge@gmail.com"
    s.gmail_oauth_client_id = "cid"
    s.gmail_oauth_client_secret = "csec"
    s.gmail_oauth_token_path = "/tmp/token.json"
    return s


@pytest.fixture()
def _client_stub(monkeypatch):
    """Patch CalendarApiClient at its source module (lazy import target)."""
    stub = MagicMock()
    stub.__enter__.return_value = stub
    stub.__exit__.return_value = None
    stub.check_scope_or_raise.return_value = None
    stub.list_events.return_value = []
    stub.insert_event.return_value = {"id": "new-event-1"}
    monkeypatch.setattr(
        "backend.intake.calendar_api_client.CalendarApiClient",
        lambda **kw: stub,
    )
    monkeypatch.setattr(
        "backend.intake.calendar_projection.get_settings",
        _stub_settings,
    )
    return stub


# --- pure helpers ---------------------------------------------------------

def test_format_title_uses_prefix_and_client_tag():
    assert _format_title(
        "deliverable", "SEO audit due", client_id="sample_school",
    ) == "[DELIVERABLE] Sample School: SEO audit due"


def test_format_title_no_client_still_works():
    assert _format_title("engagement", "Weekly team review") == \
        "[ENGAGEMENT] Weekly team review"


def test_ensure_iso_utc_from_z():
    assert _ensure_iso_utc("2026-07-15T15:30:00Z") == "2026-07-15T15:30:00Z"


def test_ensure_iso_utc_from_offset():
    assert _ensure_iso_utc("2026-07-15T08:30:00-07:00") == "2026-07-15T15:30:00Z"


def test_ensure_iso_utc_from_naive_is_treated_as_utc():
    assert _ensure_iso_utc("2026-07-15T15:30:00") == "2026-07-15T15:30:00Z"


def test_ensure_iso_utc_invalid_returns_empty():
    assert _ensure_iso_utc("not-iso") == ""


def test_default_end_meeting_request_is_30min():
    assert _default_end("2026-07-15T15:00:00Z", "meeting_request") == \
        "2026-07-15T15:30:00Z"


def test_default_end_deliverable_is_15min_marker():
    assert _default_end("2026-07-15T15:00:00Z", "deliverable") == \
        "2026-07-15T15:15:00Z"


# --- project_event happy paths -------------------------------------------

def test_project_deliverable_happy_path(_client_stub):
    out = project_event(
        title="SEO audit for Conquerors",
        start_iso="2026-07-20T15:00:00Z",
        projection_kind="deliverable",
        client_id="sample_school",
        source_id="deliverable/seo/2026-07-20",
        source_workcell="seo",
    )
    assert out["created"] is True
    assert out["event_id"] == "new-event-1"
    assert out["already_existed"] is False

    body = _client_stub.insert_event.call_args[0][0]
    assert body["summary"].startswith("[DELIVERABLE]")
    assert "Sample School" in body["summary"]
    priv = body["extendedProperties"]["private"]
    assert priv["source"] == "samus.projection"
    assert priv["projection_kind"] == "deliverable"
    assert priv["client_id"] == "sample_school"
    assert priv["source_id"] == "deliverable/seo/2026-07-20"
    assert priv["created_by_workcell"] == "seo"


def test_project_meeting_request_uses_30min_default(_client_stub):
    project_event(
        title="Kerry Brown wants to talk",
        start_iso="2026-07-15T10:00:00Z",
        projection_kind="meeting_request",
        client_id="sample_school",
    )
    body = _client_stub.insert_event.call_args[0][0]
    assert body["start"]["dateTime"] == "2026-07-15T10:00:00Z"
    assert body["end"]["dateTime"] == "2026-07-15T10:30:00Z"
    assert body["summary"].startswith("[MEETING-REQUEST]")


def test_project_all_day_deliverable(_client_stub):
    project_event(
        title="Contract renewal deadline",
        start_iso="2026-08-01T00:00:00Z",
        projection_kind="deliverable",
        all_day=True,
    )
    body = _client_stub.insert_event.call_args[0][0]
    assert body["start"] == {"date": "2026-08-01"}
    assert body["end"] == {"date": "2026-08-02"}


def test_project_campaign_milestone_tags_campaign_id(_client_stub):
    project_event(
        title="Kickoff call",
        start_iso="2026-07-15T15:00:00Z",
        projection_kind="campaign_milestone",
        campaign_id="sample_school_phased_2026",
        client_id="sample_school",
        source_id="campaign/kickoff",
        source_workcell="campaigns",
    )
    body = _client_stub.insert_event.call_args[0][0]
    priv = body["extendedProperties"]["private"]
    assert priv["campaign_id"] == "sample_school_phased_2026"
    assert priv["projection_kind"] == "campaign_milestone"


def test_project_hiring_milestone_carries_kind(_client_stub):
    project_event(
        title="Posting closes",
        start_iso="2026-08-15T23:59:00Z",
        projection_kind="hiring_milestone",
        client_id="sample_cleaning",
        campaign_id="sample_cleaning_hiring_2026",
        source_id="hiring/posting_close",
        source_workcell="campaigns",
        all_day=True,
    )
    body = _client_stub.insert_event.call_args[0][0]
    assert body["summary"].startswith("[HIRING]")
    assert body["extendedProperties"]["private"]["projection_kind"] == "hiring_milestone"


# --- idempotency ---------------------------------------------------------

def test_project_dedupe_via_source_id(_client_stub):
    _client_stub.list_events.return_value = [
        {
            "id": "existing-42",
            "extendedProperties": {
                "private": {
                    "source": "samus.projection",
                    "source_id": "seo/audit/2026-07-20",
                }
            },
        }
    ]
    out = project_event(
        title="SEO audit for Conquerors",
        start_iso="2026-07-20T15:00:00Z",
        projection_kind="deliverable",
        source_id="seo/audit/2026-07-20",
    )
    assert out["created"] is False
    assert out["already_existed"] is True
    assert out["event_id"] == "existing-42"
    _client_stub.insert_event.assert_not_called()
    # Verify we used the privateExtendedProperty filter, not q=
    kwargs = _client_stub.list_events.call_args.kwargs
    assert kwargs.get("q") in (None, "")
    assert "source_id=seo/audit/2026-07-20" in (
        kwargs.get("private_extended_property") or []
    )
    assert "source=samus.projection" in (
        kwargs.get("private_extended_property") or []
    )


def test_source_id_match_requires_samus_projection_source(_client_stub):
    # An event with matching source_id but different source (e.g., an
    # operator-added event that happens to include that string) must NOT
    # dedupe — we only dedupe against our own projections.
    _client_stub.list_events.return_value = [
        {
            "id": "operator-42",
            "extendedProperties": {
                "private": {
                    "source": "operator_import",
                    "source_id": "seo/audit/2026-07-20",
                }
            },
        }
    ]
    out = project_event(
        title="SEO audit for Conquerors",
        start_iso="2026-07-20T15:00:00Z",
        projection_kind="deliverable",
        source_id="seo/audit/2026-07-20",
    )
    assert out["created"] is True
    assert out["already_existed"] is False


# --- validation + fail-soft ----------------------------------------------

def test_missing_title_reports_error(_client_stub):
    out = project_event(
        title="", start_iso="2026-07-20T15:00:00Z",
        projection_kind="deliverable",
    )
    assert out["created"] is False
    assert "missing_required" in out["error"]
    _client_stub.insert_event.assert_not_called()


def test_missing_start_iso_reports_error(_client_stub):
    out = project_event(
        title="X", start_iso="", projection_kind="deliverable",
    )
    assert out["created"] is False
    assert "missing_required" in out["error"]


def test_invalid_start_iso_reports_error(_client_stub):
    out = project_event(
        title="X", start_iso="tomorrow", projection_kind="deliverable",
    )
    assert out["created"] is False
    assert "invalid_start_iso" in out["error"]


def test_unknown_projection_kind_reports_error(_client_stub):
    out = project_event(
        title="X", start_iso="2026-07-20T15:00:00Z",
        projection_kind="party",  # type: ignore[arg-type]
    )
    assert out["created"] is False
    assert "unknown_projection_kind" in out["error"]


def test_scope_missing_reports_scope_error(_client_stub):
    from backend.intake.calendar_api_client import CalendarApiError
    _client_stub.check_scope_or_raise.side_effect = CalendarApiError(
        "calendar_scope_missing: token lacks calendar.events."
    )
    out = project_event(
        title="X", start_iso="2026-07-20T15:00:00Z",
        projection_kind="deliverable",
    )
    assert out["created"] is False
    assert "scope" in out["error"]


def test_insert_5xx_reports_insert_failed(_client_stub):
    _client_stub.insert_event.side_effect = RuntimeError("calendar 503")
    out = project_event(
        title="X", start_iso="2026-07-20T15:00:00Z",
        projection_kind="deliverable",
    )
    assert out["created"] is False
    assert "insert_failed" in out["error"]


def test_config_missing_disables_projection(monkeypatch):
    fake = MagicMock()
    fake.gmail_inbox_email = ""
    fake.gmail_oauth_client_id = ""
    fake.gmail_oauth_client_secret = ""
    monkeypatch.setattr(
        "backend.intake.calendar_projection.get_settings", lambda: fake,
    )
    out = project_event(
        title="X", start_iso="2026-07-20T15:00:00Z",
        projection_kind="deliverable",
    )
    assert out["created"] is False
    assert out["error"] == "calendar_disabled_config_missing"
