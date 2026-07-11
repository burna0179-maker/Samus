"""AI Digital Receptionist — client call-summary report (render + build/send)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from backend.voice import client_summary
from backend.voice.client_summary import (
    build_and_send_summary,
    load_calls_in_window,
    render_call_summary,
)
from backend.voice.models import InboundCallRecord, InboundSummary


def _rec(call_id: str, *, written: str, duration: int = 120,
         answered: bool = True, voicemail: bool = False,
         appointment: bool = False, caller: str = "+14155550100") -> InboundCallRecord:
    return InboundCallRecord(
        call_id=call_id, customer_slug="acme", caller_number=caller,
        duration_sec=duration, answered=answered, voicemail_left=voicemail,
        written_at=written, ended_at=written,
        inbound_summary=InboundSummary(appointment_requested=appointment),
    )


# ---------------------------------------------------------------------------
# render_call_summary — pure
# ---------------------------------------------------------------------------

def test_render_call_summary_counts_and_sections():
    until = datetime(2026, 5, 20, tzinfo=timezone.utc)
    since = until - timedelta(days=7)
    calls = [
        _rec("c1", written="2026-05-18T10:00:00Z", duration=120, appointment=True),
        _rec("c2", written="2026-05-19T11:00:00Z", duration=60, voicemail=True),
        _rec("c3", written="2026-05-19T12:00:00Z", duration=0, answered=False),
    ]
    body = render_call_summary(
        business_name="Acme Plumbing", customer_slug="acme",
        calls=calls, since=since, until=until, ts="2026-05-20T00:00:00Z",
    )
    assert "# Call Summary - Acme Plumbing" in body
    assert "**Calls received:** 3" in body
    assert "**Appointment requests:** 1" in body
    assert "**Voicemails / messages taken:** 1" in body
    assert "## Needs your attention" in body
    assert "## All calls" in body


def test_render_call_summary_empty_period():
    until = datetime(2026, 5, 20, tzinfo=timezone.utc)
    body = render_call_summary(
        business_name="Acme", customer_slug="acme", calls=[],
        since=until - timedelta(days=7), until=until,
    )
    assert "**Calls received:** 0" in body
    assert "No calls received this period" in body


# ---------------------------------------------------------------------------
# load_calls_in_window
# ---------------------------------------------------------------------------

def test_load_calls_in_window_filters_by_time(tmp_path, monkeypatch):
    monkeypatch.setenv("SAMUS_ARTIFACT_ROOT", str(tmp_path))
    calls_root = tmp_path / "customers" / "acme" / "calls"
    for cid, written in [
        ("in_window", "2026-05-18T10:00:00Z"),
        ("too_old", "2026-04-01T10:00:00Z"),
    ]:
        d = calls_root / cid
        d.mkdir(parents=True)
        (d / "call.json").write_text(
            _rec(cid, written=written).model_dump_json(), encoding="utf-8",
        )
    until = datetime(2026, 5, 20, tzinfo=timezone.utc)
    found = load_calls_in_window("acme", since=until - timedelta(days=7), until=until)
    assert [c.call_id for c in found] == ["in_window"]


# ---------------------------------------------------------------------------
# build_and_send_summary
# ---------------------------------------------------------------------------

def _write_receptionist_config(root: Path, slug: str, body: str) -> None:
    d = root / "customers" / slug / "receptionist"
    d.mkdir(parents=True, exist_ok=True)
    (d / "config.yaml").write_text(body, encoding="utf-8")


def test_build_and_send_summary_writes_and_emails(tmp_path, monkeypatch):
    monkeypatch.setenv("SAMUS_ARTIFACT_ROOT", str(tmp_path))
    from backend.voice import receptionist_config as rc
    rc.clear_cache()
    _write_receptionist_config(tmp_path, "acme",
                               "business_name: Acme\nsummary_email: owner@acme.test\n")
    # One call inside the weekly window.
    d = tmp_path / "customers" / "acme" / "calls" / "c1"
    d.mkdir(parents=True)
    now = datetime.now(timezone.utc)
    (d / "call.json").write_text(
        _rec("c1", written=now.strftime("%Y-%m-%dT%H:%M:%SZ")).model_dump_json(),
        encoding="utf-8",
    )

    sent: list[tuple] = []

    def _fake_send(to, subject, body, **kw):
        sent.append((to, subject))
        return {"message_id": "m1", "channel": "test"}

    result = build_and_send_summary("acme", cadence="weekly",
                                    send_email_fn=_fake_send)
    assert result["ok"] is True
    assert result["calls"] == 1
    assert result["emailed"] is True
    assert sent and sent[0][0] == "owner@acme.test"
    assert Path(result["report_path"]).is_file()


def test_build_and_send_summary_no_config(tmp_path, monkeypatch):
    monkeypatch.setenv("SAMUS_ARTIFACT_ROOT", str(tmp_path))
    from backend.voice import receptionist_config as rc
    rc.clear_cache()
    result = build_and_send_summary("ghost", send=False)
    assert result["ok"] is False
    assert result["error"] == "no_receptionist_config"
