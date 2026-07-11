"""Outreach -> CRM dispatch — a sent outreach message enters the follow-up cadence.

When outreach.send_message succeeds (email via SES) it best-effort POSTs a
Conversation (outcome='outreach_sent') + a CallState (state='outreach_sent',
next_attempt_at = sent + 2 days) to samus-crm, so the morning brief can surface
the prospect as a follow-up call. A CRM outage must never break the send.
"""

from __future__ import annotations


class _Resp:
    def __init__(self, status_code: int = 200):
        self.status_code = status_code
        self.text = ""


def _stub_settings(monkeypatch, *, crm_url="http://crm.local", hmac="secret"):
    class _S:
        gateway_urls = {"crm": crm_url}

    s = _S()
    s.shared_hmac_key = hmac
    import backend.outreach.service as svc

    monkeypatch.setattr(svc, "get_settings", lambda: s)


def _stub_ses(monkeypatch, *, ts="2026-05-22T09:00:00Z"):
    """Make the email send succeed with a fixed result dict.

    Outreach.service now routes through ``backend.common.email_backend.send_email``
    (function-local import) which dispatches by ``settings.email_backend``. Patch
    that dispatcher directly so the test bypasses the SendGrid/SES selection.
    """

    def _fake_send(to, subject, body, **kw):
        return {"message_id": "m_1", "channel": "email", "to": to, "ts": ts}

    monkeypatch.setattr("backend.common.email_backend.send_email", _fake_send)


def _capture_posts(monkeypatch, *, raises=None):
    """Capture signed_post_json_sync calls; returns the capture list."""
    posts: list[tuple] = []
    import backend.outreach.service as svc

    def _fake_post(base_url, path, payload, **kw):
        posts.append((base_url, path, payload))
        if raises is not None:
            raise raises
        return _Resp(200)

    monkeypatch.setattr(svc, "signed_post_json_sync", _fake_post)
    return posts


def _email_req(**over):
    from backend.outreach.models import OutreachMessageRequest

    base = dict(
        prospect_id="pr_acme",
        channel="email",
        template_id="cold_v7",
        body="hello",
        to="owner@acme.com",
        subject="Quick question",
        company="Acme HVAC",
        phone="555-0100",
    )
    base.update(over)
    return OutreachMessageRequest(**base)


def test_outreach_message_request_accepts_company_phone():
    """OutreachMessageRequest carries the optional company + phone."""
    req = _email_req()
    assert req.company == "Acme HVAC"
    assert req.phone == "555-0100"


def test_send_message_dispatches_conversation_and_call_state(tmp_path, monkeypatch):
    """A sent email writes an outreach_sent Conversation + CallState to CRM."""
    monkeypatch.setenv("SAMUS_OUTREACH_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    _stub_settings(monkeypatch)
    _stub_ses(monkeypatch, ts="2026-05-22T09:00:00Z")
    posts = _capture_posts(monkeypatch)

    from backend.outreach.service import send_message

    result = send_message(_email_req())

    assert result["channel"] == "email"
    assert len(posts) == 2
    conv_post = next(p for p in posts if p[1] == "/crm/conversations")
    state_post = next(p for p in posts if p[1].startswith("/crm/call-state/"))

    conv = conv_post[2]
    assert conv["outcome"] == "outreach_sent"
    assert conv["channel"] == "email"
    assert conv["prospect_id"] == "pr_acme"
    assert conv["source"] == "outreach"
    assert conv["structured_data"]["company"] == "Acme HVAC"
    assert conv["structured_data"]["phone"] == "555-0100"
    assert conv["structured_data"]["emailed_on"] == "2026-05-22"
    assert conv["structured_data"]["follow_up_on"] == "2026-05-24"  # sent + 2d

    state = state_post[2]
    assert state_post[1] == "/crm/call-state/pr_acme"
    assert state["state"] == "outreach_sent"
    assert state["next_attempt_at"] == "2026-05-24"
    assert state["prospect_id"] == "pr_acme"


def test_send_message_succeeds_when_crm_dispatch_raises(tmp_path, monkeypatch):
    """A CRM outage during dispatch must not break the already-sent message."""
    monkeypatch.setenv("SAMUS_OUTREACH_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    _stub_settings(monkeypatch)
    _stub_ses(monkeypatch)
    _capture_posts(monkeypatch, raises=RuntimeError("simulated crm outage"))

    from backend.outreach.service import send_message

    result = send_message(_email_req())
    assert result["channel"] == "email"  # the send itself still succeeded


def test_send_message_skips_dispatch_when_crm_url_unset(tmp_path, monkeypatch):
    """No crm_url configured -> dispatch is a clean no-op, not an error."""
    monkeypatch.setenv("SAMUS_OUTREACH_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    _stub_settings(monkeypatch, crm_url="")
    _stub_ses(monkeypatch)
    posts = _capture_posts(monkeypatch)

    from backend.outreach.service import send_message

    result = send_message(_email_req())
    assert result["channel"] == "email"
    assert posts == []  # short-circuited before any POST
