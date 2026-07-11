"""Tests for the outreach SES email channel — adapter + send_message routing."""

from __future__ import annotations

import json
from typing import Any

import pytest


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


class _StubSesClient:
    """Captures send_email kwargs and returns a canned MessageId."""

    def __init__(self, message_id: str = "ses_test_123") -> None:
        self.message_id = message_id
        self.calls: list[dict[str, Any]] = []

    def send_email(self, **kwargs: Any) -> dict[str, str]:
        self.calls.append(kwargs)
        return {"MessageId": self.message_id}


def _patch_boto3_client(monkeypatch, stub: Any) -> list[tuple[str, dict[str, Any]]]:
    """Replace boto3.client globally so any ('ses', ...) call returns stub."""
    from backend.outreach import ses_adapter

    calls: list[tuple[str, dict[str, Any]]] = []

    def _fake_client(service: str, **kwargs: Any) -> Any:
        calls.append((service, kwargs))
        return stub

    monkeypatch.setattr(ses_adapter.boto3, "client", _fake_client)
    return calls


def _set_from_email(monkeypatch, value: str) -> None:
    monkeypatch.setenv("SES_FROM_EMAIL", value)
    from backend.common.settings import reload_settings

    reload_settings()


# ---------------------------------------------------------------------------
# adapter unit tests
# ---------------------------------------------------------------------------


def test_send_email_via_ses_happy_path(monkeypatch):
    _set_from_email(monkeypatch, "samus@example.com")
    stub = _StubSesClient(message_id="ses_test_123")
    client_calls = _patch_boto3_client(monkeypatch, stub)

    from backend.outreach.ses_adapter import send_email_via_ses

    out = send_email_via_ses("lead@example.com", "Subject A", "hello body")

    assert out["message_id"] == "ses_test_123"
    assert out["channel"] == "email"
    assert out["to"] == "lead@example.com"
    assert out["ts"]  # ISO string present
    assert client_calls and client_calls[0][0] == "ses"

    assert len(stub.calls) == 1
    sent = stub.calls[0]
    assert sent["Source"] == "samus@example.com"
    assert sent["Destination"] == {"ToAddresses": ["lead@example.com"]}
    assert sent["Message"]["Subject"]["Data"] == "Subject A"
    assert sent["Message"]["Body"]["Text"]["Data"] == "hello body"


def test_send_email_via_ses_uses_settings_from_email_when_unset(monkeypatch):
    _set_from_email(monkeypatch, "fallback@example.com")
    stub = _StubSesClient()
    _patch_boto3_client(monkeypatch, stub)

    from backend.outreach.ses_adapter import send_email_via_ses

    send_email_via_ses("lead@example.com", "s", "b")  # no from_addr arg
    assert stub.calls[0]["Source"] == "fallback@example.com"


def test_send_email_via_ses_explicit_from_addr_overrides_settings(monkeypatch):
    _set_from_email(monkeypatch, "fallback@example.com")
    stub = _StubSesClient()
    _patch_boto3_client(monkeypatch, stub)

    from backend.outreach.ses_adapter import send_email_via_ses

    send_email_via_ses("lead@example.com", "s", "b", from_addr="explicit@example.com")
    assert stub.calls[0]["Source"] == "explicit@example.com"


def test_send_email_via_ses_raises_when_no_from_addr(monkeypatch):
    _set_from_email(monkeypatch, "")  # settings empty
    stub = _StubSesClient()
    _patch_boto3_client(monkeypatch, stub)

    from backend.outreach.ses_adapter import send_email_via_ses

    with pytest.raises(ValueError):
        send_email_via_ses("lead@example.com", "s", "b", from_addr=None)


def test_send_email_via_ses_wraps_client_error(monkeypatch):
    _set_from_email(monkeypatch, "samus@example.com")
    from backend.outreach.ses_adapter import ClientError  # stub-aware

    class _BadClient:
        def send_email(self, **kwargs: Any) -> dict[str, str]:
            raise ClientError(
                {"Error": {"Code": "MessageRejected", "Message": "Email address not verified"}},
                "SendEmail",
            )

    _patch_boto3_client(monkeypatch, _BadClient())

    from backend.outreach.ses_adapter import SesAdapterError, send_email_via_ses

    with pytest.raises(SesAdapterError) as excinfo:
        send_email_via_ses("lead@example.com", "s", "b")
    assert "MessageRejected" in str(excinfo.value) or "not verified" in str(excinfo.value)


# ---------------------------------------------------------------------------
# OutreachMessageRequest validator
# ---------------------------------------------------------------------------


def test_outreach_message_request_email_requires_to_and_subject():
    from pydantic import ValidationError
    from backend.outreach.models import OutreachMessageRequest

    with pytest.raises(ValidationError):
        OutreachMessageRequest(
            prospect_id="p1",
            channel="email",
            template_id="t",
            body="b",
        )  # to + subject missing

    with pytest.raises(ValidationError):
        OutreachMessageRequest(
            prospect_id="p1",
            channel="email",
            template_id="t",
            body="b",
            to="x@example.com",
        )  # subject missing

    # whitespace-only also rejected
    with pytest.raises(ValidationError):
        OutreachMessageRequest(
            prospect_id="p1",
            channel="email",
            template_id="t",
            body="b",
            to="   ",
            subject="s",
        )


def test_outreach_message_request_non_email_skips_email_validator():
    from backend.outreach.models import OutreachMessageRequest

    # sms doesn't require to/subject
    req = OutreachMessageRequest(
        prospect_id="p1",
        channel="sms",
        template_id="t",
        body="b",
    )
    assert req.to is None and req.subject is None


# ---------------------------------------------------------------------------
# send_message routing
# ---------------------------------------------------------------------------


def test_send_message_routes_email_to_ses(monkeypatch, tmp_path):
    monkeypatch.setenv("SAMUS_OUTREACH_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    captured: dict[str, Any] = {}

    def _fake_send(to: str, subject: str, body: str, **kw: Any) -> dict[str, str]:
        captured.update({"to": to, "subject": subject, "body": body, "kw": kw})
        return {
            "message_id": "msg_routed",
            "channel": "email",
            "to": to,
            "ts": "2026-05-15T00:00:00Z",
        }

    from backend.outreach import service as svc

    # The legacy outreach.ses_adapter import in service.py is retained for the
    # SesAdapterError exception type; the actual send routes through
    # backend.common.email_backend.send_email which dispatches by
    # settings.email_backend. Patch that dispatcher directly.
    monkeypatch.setattr("backend.common.email_backend.send_email", _fake_send)

    from backend.outreach.models import OutreachMessageRequest

    req = OutreachMessageRequest(
        prospect_id="p_route",
        channel="email",
        template_id="tmpl",
        body="hello",
        to="lead@example.com",
        subject="Hi",
    )
    out = svc.send_message(req)
    assert out["message_id"] == "msg_routed"
    assert captured["to"] == "lead@example.com"
    assert captured["subject"] == "Hi"
    assert captured["body"] == "hello"


def test_send_message_sms_still_raises():
    from backend.outreach.models import OutreachMessageRequest
    from backend.outreach.service import send_message

    req = OutreachMessageRequest(
        prospect_id="p_sms",
        channel="sms",
        template_id="t",
        body="b",
    )
    with pytest.raises(NotImplementedError):
        send_message(req)


def test_send_message_call_degraded_by_default(monkeypatch):
    """call channel now delegates to the voice workcell; OFF by default ->
    structured degraded receipt (never NotImplementedError)."""
    monkeypatch.delenv("SAMUS_OUTREACH_VOICE_SEND", raising=False)
    from backend.common.settings import reload_settings

    reload_settings()
    from backend.outreach.models import OutreachMessageRequest
    from backend.outreach.service import send_message

    req = OutreachMessageRequest(
        prospect_id="p_call",
        channel="call",
        template_id="t",
        body="b",
        phone="+15551234567",
    )
    out = send_message(req)
    assert out["status"] == "degraded"
    assert out["channel"] == "call"


def test_send_message_voicemail_degraded_by_default(monkeypatch):
    monkeypatch.delenv("SAMUS_OUTREACH_VOICE_SEND", raising=False)
    from backend.common.settings import reload_settings

    reload_settings()
    from backend.outreach.models import OutreachMessageRequest
    from backend.outreach.service import send_message

    req = OutreachMessageRequest(
        prospect_id="p_vm",
        channel="voicemail",
        template_id="t",
        body="b",
        phone="+15551234567",
    )
    out = send_message(req)
    assert out["status"] == "degraded"
    assert out["channel"] == "voicemail"


def test_send_message_appends_audit_on_success(monkeypatch, tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    monkeypatch.setenv("SAMUS_OUTREACH_AUDIT_PATH", str(audit_path))

    def _fake_send(to: str, subject: str, body: str, **kw: Any) -> dict[str, str]:
        return {
            "message_id": "msg_audit",
            "channel": "email",
            "to": to,
            "ts": "2026-05-15T00:00:00Z",
        }

    from backend.outreach import service as svc

    monkeypatch.setattr("backend.common.email_backend.send_email", _fake_send)

    from backend.outreach.models import OutreachMessageRequest

    req = OutreachMessageRequest(
        prospect_id="p_audit",
        channel="email",
        template_id="tmpl",
        body="big body content",
        to="lead@example.com",
        subject="Hi",
    )
    svc.send_message(req)

    assert audit_path.exists()
    lines = [line for line in audit_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert lines, "expected at least one audit line"
    record = json.loads(lines[-1])
    assert record["service"] == "outreach"
    assert record["action"] == "send_message"
    assert record["task_id"] == "p_audit"
    assert record["status"] == "completed"


def test_send_message_appends_failed_audit_and_reraises(monkeypatch, tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    monkeypatch.setenv("SAMUS_OUTREACH_AUDIT_PATH", str(audit_path))

    from backend.outreach import service as svc
    from backend.common.email_backend import EmailBackendError

    def _boom(*args: Any, **kw: Any) -> dict[str, str]:
        raise EmailBackendError("boom")

    monkeypatch.setattr("backend.common.email_backend.send_email", _boom)

    from backend.outreach.models import OutreachMessageRequest

    req = OutreachMessageRequest(
        prospect_id="p_fail",
        channel="email",
        template_id="tmpl",
        body="b",
        to="lead@example.com",
        subject="Hi",
    )
    with pytest.raises(EmailBackendError):
        svc.send_message(req)

    lines = [line for line in audit_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert lines, "expected a failed-audit line"
    record = json.loads(lines[-1])
    assert record["status"] == "failed"
    assert record["action"] == "send_message"
