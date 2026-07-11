"""Tests for the outreach campaign CLI send path (SendGrid backend routing)."""
from __future__ import annotations

import backend.common.email_backend as email_backend
import backend.outreach.run_campaign as rc
import backend.outreach.service as svc
from backend.outreach.models import OutreachMessageRequest


def _msg(to="a@x.com", pid="apollo_p1") -> OutreachMessageRequest:
    return OutreachMessageRequest(
        prospect_id=pid,
        channel="email",
        template_id="apollo_cold_v1",
        to=to,
        subject="Quick question about Acme",
        body="Hi Dana,\n\nbody\n\n---\n123 Main St\nUnsubscribe: https://x/u",
        company="Acme",
    )


def test_send_all_routes_through_email_backend_and_records(tmp_path, monkeypatch):
    calls: list[dict] = []

    def _fake_send_email(*, to, subject, body, from_name=None, **kw):
        calls.append({"to": to, "subject": subject, "from_name": from_name})
        return {"message_id": "sg-123", "channel": "email", "to": to, "ts": "2026-05-29T00:00:00Z"}

    crm_dispatched: list[str] = []
    monkeypatch.setattr(email_backend, "send_email", _fake_send_email)
    monkeypatch.setattr(svc, "_dispatch_outreach_to_crm",
                        lambda req, ts: crm_dispatched.append(req.prospect_id))

    ledger = tmp_path / "campaign.jsonl"
    supp = tmp_path / "emailed.txt"
    sent, failed = rc._send_all([_msg(), _msg(to="b@x.com", pid="apollo_p2")],
                                str(ledger), str(supp))

    assert (sent, failed) == (2, 0)
    assert [c["to"] for c in calls] == ["a@x.com", "b@x.com"]
    # CRM follow-up preserved
    assert crm_dispatched == ["apollo_p1", "apollo_p2"]
    # ledger records the backend tag + suppression file grows
    lines = ledger.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    assert '"backend": "email_backend"' in lines[0] or '"backend":"email_backend"' in lines[0]
    assert set(supp.read_text(encoding="utf-8").split()) == {"a@x.com", "b@x.com"}


def test_send_all_one_failure_does_not_abort(tmp_path, monkeypatch):
    def _flaky(*, to, subject, body, from_name=None, **kw):
        if to == "bad@x.com":
            raise RuntimeError("sendgrid_http_400")
        return {"message_id": "ok", "channel": "email", "to": to, "ts": "t"}

    monkeypatch.setattr(email_backend, "send_email", _flaky)
    monkeypatch.setattr(svc, "_dispatch_outreach_to_crm", lambda req, ts: None)

    ledger = tmp_path / "c.jsonl"
    supp = tmp_path / "s.txt"
    sent, failed = rc._send_all([_msg(to="bad@x.com"), _msg(to="good@x.com")],
                                str(ledger), str(supp))

    assert (sent, failed) == (1, 1)
    assert supp.read_text(encoding="utf-8").split() == ["good@x.com"]
