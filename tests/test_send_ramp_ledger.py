"""Send-ramp ledger (B-6) — a real live send appends one ramp row.

Covers the regression where the live cold-send path recorded nothing to
``send_ramp.jsonl``, so the warmup-ramp's daily-volume accounting was blind to
actual outbound. The fix is RECORD-ONLY: ``send_message`` appends a ramp row
after a real send succeeds; it does not gate/throttle.
"""

from __future__ import annotations

import datetime as _dt
import json


# ---------------------------------------------------------------------------
# unit: the ledger module records the canonical row shape
# ---------------------------------------------------------------------------


def test_record_send_appends_canonical_row(tmp_path, monkeypatch):
    ledger = tmp_path / "outreach" / "send_ramp.jsonl"
    monkeypatch.setenv("SAMUS_SEND_RAMP_LEDGER_PATH", str(ledger))

    from backend.common import send_ramp

    day = _dt.date(2026, 6, 24)
    assert send_ramp.record_send(to="owner@acme.com", today=day) is True

    rows = [json.loads(l) for l in ledger.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert rows == [{"date": "2026-06-24", "to": "owner@acme.com"}]
    assert send_ramp.sent_today(today=day) == 1


def test_record_send_fails_open_on_io_error(tmp_path, monkeypatch):
    """A ledger write failure must return False, never raise (fail-open)."""
    ledger = tmp_path / "outreach" / "send_ramp.jsonl"
    monkeypatch.setenv("SAMUS_SEND_RAMP_LEDGER_PATH", str(ledger))

    from backend.common import send_ramp

    def _boom(*_a, **_k):
        raise OSError("simulated read-only filesystem")

    # Break the mkdir so the append path raises internally.
    monkeypatch.setattr(send_ramp.Path, "mkdir", _boom)
    assert send_ramp.record_send(to="x@y.com") is False  # swallowed, no raise


# ---------------------------------------------------------------------------
# integration: a successful send_message email appends exactly one ramp row
# ---------------------------------------------------------------------------


def _email_req(**over):
    from backend.outreach.models import OutreachMessageRequest

    base = dict(
        prospect_id="pr_acme",
        channel="email",
        template_id="cash_engine_initial",
        body="hello",
        to="owner@acme.com",
        subject="Quick question",
        company="Acme HVAC",
        phone="555-0100",
    )
    base.update(over)
    return OutreachMessageRequest(**base)


def test_successful_send_message_appends_send_ramp_row(tmp_path, monkeypatch):
    """The live send path ticks the send-ramp ledger after a real send."""
    ledger = tmp_path / "outreach" / "send_ramp.jsonl"
    monkeypatch.setenv("SAMUS_SEND_RAMP_LEDGER_PATH", str(ledger))
    monkeypatch.setenv("SAMUS_OUTREACH_AUDIT_PATH", str(tmp_path / "audit.jsonl"))

    import backend.common.email_backend as email_backend
    import backend.outreach.service as svc

    sent_calls: list[str] = []

    def _fake_send_email(to, subject, body, **kwargs):
        sent_calls.append(to)
        return {"message_id": "m_1", "channel": "email", "to": to, "ts": "2026-06-24T09:00:00Z"}

    # send_message imports send_email from email_backend at call time.
    monkeypatch.setattr(email_backend, "send_email", _fake_send_email)
    # Neutralize the best-effort CRM dispatch so the test is hermetic.
    monkeypatch.setattr(svc, "_dispatch_outreach_to_crm", lambda *a, **k: None)

    result = svc.send_message(_email_req())
    assert result["channel"] == "email"
    assert sent_calls == ["owner@acme.com"]  # the real send fired once

    from backend.common import send_ramp

    rows = send_ramp.read_rows(ledger)
    assert len(rows) == 1
    assert rows[0]["to"] == "owner@acme.com"
    assert rows[0]["date"] == _dt.date.today().isoformat()


def test_failed_send_message_appends_no_ramp_row(tmp_path, monkeypatch):
    """A send that raises must NOT append a ramp row (record-on-success only)."""
    ledger = tmp_path / "outreach" / "send_ramp.jsonl"
    monkeypatch.setenv("SAMUS_SEND_RAMP_LEDGER_PATH", str(ledger))
    monkeypatch.setenv("SAMUS_OUTREACH_AUDIT_PATH", str(tmp_path / "audit.jsonl"))

    import backend.common.email_backend as email_backend
    import backend.outreach.service as svc

    def _boom_send_email(to, subject, body, **kwargs):
        raise email_backend.EmailBackendError("provider down")

    monkeypatch.setattr(email_backend, "send_email", _boom_send_email)

    import pytest

    with pytest.raises(email_backend.EmailBackendError):
        svc.send_message(_email_req())

    from backend.common import send_ramp

    assert send_ramp.read_rows(ledger) == []  # nothing recorded for a failed send
