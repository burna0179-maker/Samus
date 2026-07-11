"""Gmail inbox poller — RFC822 parsing + drain orchestration.

The Gmail API client is replaced by a fake (no network); CRM service
functions are stubbed so we can assert exactly what the poller asks
them to create.
"""
from __future__ import annotations

from typing import Any

import pytest

from backend.intake.gmail_poller import (
    DrainPassResult,
    InboundEmailHandled,
    ParsedInboundEmail,
    drain_once,
    handle_parsed_email,
    parse_rfc822,
)


# ---------------------------------------------------------------------------
# RFC822 parsing
# ---------------------------------------------------------------------------

_PLAIN_TEXT_MSG = (
    b"Message-ID: <abc123@mail.example.com>\r\n"
    b"From: Jane Doe <jane@customer.com>\r\n"
    b"To: ahartman@hustleforge.tech\r\n"
    b"Subject: Re: SEO audit follow-up\r\n"
    b"Date: Mon, 12 May 2026 10:15:00 +0000\r\n"
    b"Content-Type: text/plain; charset=utf-8\r\n"
    b"\r\n"
    b"Hi Alex,\r\n\r\nThanks for the audit. When can we hop on a call?\r\n"
)


def test_parse_rfc822_extracts_headers_and_plain_body():
    p = parse_rfc822(_PLAIN_TEXT_MSG)
    assert p.message_id == "<abc123@mail.example.com>"
    assert p.from_addr == "jane@customer.com"
    assert "Jane Doe" in p.from_display
    assert p.to_addrs == ["ahartman@hustleforge.tech"]
    assert p.subject == "Re: SEO audit follow-up"
    assert "Thanks for the audit" in p.body_text
    assert p.body_format == "text/plain"
    assert p.attachment_names == []


def test_parse_rfc822_multipart_prefers_plain_over_html():
    raw = (
        b"Message-ID: <m2@x>\r\n"
        b"From: bob@x.com\r\n"
        b"To: a@b.com\r\n"
        b"Subject: hi\r\n"
        b'Content-Type: multipart/alternative; boundary="bnd"\r\n'
        b"\r\n"
        b"--bnd\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
        b"plain version\r\n"
        b"--bnd\r\n"
        b"Content-Type: text/html; charset=utf-8\r\n\r\n"
        b"<html><body>html version</body></html>\r\n"
        b"--bnd--\r\n"
    )
    p = parse_rfc822(raw)
    assert "plain version" in p.body_text
    assert p.body_format == "text/plain"


# ---------------------------------------------------------------------------
# handle_parsed_email — CRM artifact + task wiring (with stubs)
# ---------------------------------------------------------------------------

def _stub_crm(monkeypatch, *, artifact_id="art_1", task_id="ot_1",
              artifact_status="created", task_status="created",
              capture: dict | None = None):
    """Replace CRM service functions used by the handler."""

    def _create_artifact(req):
        if capture is not None:
            capture["artifact_req"] = req
        from backend.crm.models import CreateArtifactResult
        return CreateArtifactResult(
            status=artifact_status, artifact_id=artifact_id, ts="now",
        )

    def _create_task(req):
        if capture is not None:
            capture["task_req"] = req
        from backend.crm.models import CreateOperatorTaskResult
        return CreateOperatorTaskResult(
            status=task_status, operator_task_id=task_id, ts="now",
        )

    def _find_opp(email):
        if capture is not None:
            capture["find_opp_email"] = email
        return capture.get("opp_id_returns", "") if capture else ""

    import backend.crm.service as crm_svc
    monkeypatch.setattr(crm_svc, "create_artifact", _create_artifact)
    monkeypatch.setattr(crm_svc, "create_operator_task", _create_task)
    monkeypatch.setattr(crm_svc, "find_opportunity_for_email", _find_opp)


def _stub_billing(monkeypatch, *, state="unknown"):
    """Replace finance customer summary with a real CustomerBillingSummary.

    Real summary object so one_line_summary() renders naturally per state —
    no method monkey-patching needed.
    """
    from backend.finance.models import (
        CustomerBillingSummary,
        CustomerChargeRow,
        CustomerSubscriptionRow,
    )

    def _summary(email, *, charges_limit=5):
        kwargs: dict = {
            "email": email, "state": state, "ts": "now",
        }
        if state == "subscriber":
            kwargs["stripe_customer_id"] = "cus_x"
            kwargs["mrr_usd"] = 300.0
            kwargs["active_subscriptions"] = [CustomerSubscriptionRow(
                subscription_id="sub_1", status="active", mrr_usd=300.0,
            )]
        elif state == "customer":
            kwargs["stripe_customer_id"] = "cus_x"
            kwargs["total_paid_usd"] = 100.0
            kwargs["recent_charges"] = [CustomerChargeRow(
                charge_id="ch_1", amount_usd=100.0, currency="usd",
                status="succeeded", paid=True,
                created_iso="2026-05-01T00:00:00Z",
            )]
        elif state == "lookup_failed":
            kwargs["lookup_error"] = "stripe_api_key_unset"
        return CustomerBillingSummary(**kwargs)

    import backend.finance.service as fin_svc
    monkeypatch.setattr(fin_svc, "get_customer_billing_summary", _summary)


def test_handle_parsed_email_creates_artifact_and_task_for_unknown_sender(
    monkeypatch, tmp_path,
):
    monkeypatch.setenv(
        "SAMUS_GMAIL_INBOX_LEDGER", str(tmp_path / "inbound.jsonl"),
    )
    capture: dict = {"opp_id_returns": ""}
    _stub_crm(monkeypatch, capture=capture)
    _stub_billing(monkeypatch, state="unknown")

    parsed = parse_rfc822(_PLAIN_TEXT_MSG)
    handled = handle_parsed_email(parsed)

    assert handled.persisted is True
    assert handled.artifact_id == "art_1"
    assert handled.operator_task_id == "ot_1"
    assert handled.opportunity_id == ""
    assert handled.billing_state == "unknown"

    art = capture["artifact_req"]
    assert art.kind == "inbound_email"
    assert art.owner_entity_kind == "contact"
    assert art.owner_entity_id == "jane@customer.com"
    assert art.inline_data["message_id"] == parsed.message_id
    assert art.inline_data["billing_state"] == "unknown"

    task = capture["task_req"]
    assert task.kind == "reply_email"
    assert "SEO audit follow-up" in task.title
    assert "no Stripe customer" in task.description
    assert task.source == "intake.gmail_poller"
    assert task.source_ref == parsed.message_id


def test_handle_parsed_email_attaches_to_open_opportunity_when_found(
    monkeypatch, tmp_path,
):
    monkeypatch.setenv(
        "SAMUS_GMAIL_INBOX_LEDGER", str(tmp_path / "inbound.jsonl"),
    )
    capture: dict = {"opp_id_returns": "op_abc"}
    _stub_crm(monkeypatch, capture=capture)
    _stub_billing(monkeypatch, state="subscriber")

    parsed = parse_rfc822(_PLAIN_TEXT_MSG)
    handled = handle_parsed_email(parsed)

    assert handled.opportunity_id == "op_abc"
    art = capture["artifact_req"]
    assert art.owner_entity_kind == "opportunity"
    assert art.owner_entity_id == "op_abc"

    task = capture["task_req"]
    assert task.related_entity_kind == "opportunity"
    assert task.related_entity_id == "op_abc"
    assert "op_abc" in task.description
    assert "$300.00/mo" in task.description


def test_handle_parsed_email_survives_billing_lookup_exception(
    monkeypatch, tmp_path,
):
    monkeypatch.setenv(
        "SAMUS_GMAIL_INBOX_LEDGER", str(tmp_path / "inbound.jsonl"),
    )
    capture: dict = {"opp_id_returns": ""}
    _stub_crm(monkeypatch, capture=capture)

    import backend.finance.service as fin_svc
    def _boom(email, *, charges_limit=5):
        raise RuntimeError("stripe down")
    monkeypatch.setattr(fin_svc, "get_customer_billing_summary", _boom)

    parsed = parse_rfc822(_PLAIN_TEXT_MSG)
    handled = handle_parsed_email(parsed)
    assert handled.artifact_id == "art_1"
    assert handled.operator_task_id == "ot_1"
    assert handled.billing_state == "lookup_failed"
    task = capture["task_req"]
    assert "billing lookup raised" in task.description


# ---------------------------------------------------------------------------
# drain_once — full pass orchestration with a fake Gmail API client
# ---------------------------------------------------------------------------

class _FakeApiClient:
    """Minimal Gmail-API context-manager fake for drain_once tests.

    Mirrors the GmailApiClient surface the poller actually calls:
    list_unread_message_ids, fetch_raw, mark_read.
    """

    def __init__(self, messages: dict[str, bytes]):
        # messages: gmail_id -> raw RFC822 bytes
        self._messages = messages
        self._marked_read: list[str] = []
        self.list_call_count = 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def list_unread_message_ids(self, *, max_results: int = 25) -> list[str]:
        self.list_call_count += 1
        return list(self._messages.keys())[:max_results]

    def fetch_raw(self, message_id: str) -> bytes:
        return self._messages[message_id]

    def mark_read(self, message_id: str) -> None:
        self._marked_read.append(message_id)


def _override_poller_settings(monkeypatch, tmp_path, **overrides):
    """Override get_settings used by the poller module."""
    defaults = {
        "gmail_inbox_email": "samushustleforge@gmail.com",
        "gmail_oauth_client_id": "fake.apps.googleusercontent.com",
        "gmail_oauth_client_secret": "fake-secret",
        "gmail_oauth_token_path": str(tmp_path / "token.json"),
        "gmail_inbox_max_per_pass": 25,
        "gmail_inbox_ledger_path": str(tmp_path / "inbound.jsonl"),
    }
    defaults.update(overrides)

    class _S:
        pass
    s = _S()
    for k, v in defaults.items():
        setattr(s, k, v)
    import backend.intake.gmail_poller as poll_mod
    monkeypatch.setattr(poll_mod, "get_settings", lambda: s)


def test_drain_once_disabled_when_credentials_missing(monkeypatch, tmp_path):
    _override_poller_settings(
        monkeypatch, tmp_path,
        gmail_inbox_email="", gmail_oauth_client_id="", gmail_oauth_client_secret="",
    )
    result = drain_once()
    assert result.enabled is False
    assert result.fetched == 0


def test_drain_once_disabled_when_only_client_secret_missing(monkeypatch, tmp_path):
    _override_poller_settings(
        monkeypatch, tmp_path,
        gmail_oauth_client_secret="",
    )
    result = drain_once()
    assert result.enabled is False


def test_drain_once_processes_one_message_end_to_end(monkeypatch, tmp_path):
    _override_poller_settings(monkeypatch, tmp_path)
    _stub_crm(monkeypatch, capture={"opp_id_returns": ""})
    _stub_billing(monkeypatch, state="unknown")

    fake = _FakeApiClient({"msg_id_aaa": _PLAIN_TEXT_MSG})
    result = drain_once(api_factory=lambda: fake)
    assert result.enabled is True
    assert result.fetched == 1
    assert result.processed == 1
    assert result.duplicates == 0
    assert result.failed == 0
    assert fake._marked_read == ["msg_id_aaa"]

    ledger_path = tmp_path / "inbound.jsonl"
    assert ledger_path.exists()
    contents = ledger_path.read_text(encoding="utf-8")
    assert "<abc123@mail.example.com>" in contents
    assert "msg_id_aaa" in contents
    assert "art_1" in contents
    assert "ot_1" in contents


def test_drain_once_treats_known_message_id_as_duplicate(monkeypatch, tmp_path):
    _override_poller_settings(monkeypatch, tmp_path)
    ledger_path = tmp_path / "inbound.jsonl"
    ledger_path.write_text(
        '{"message_id": "<abc123@mail.example.com>"}\n', encoding="utf-8",
    )
    _stub_crm(monkeypatch, capture={"opp_id_returns": ""})
    _stub_billing(monkeypatch, state="unknown")

    fake = _FakeApiClient({"msg_id_aaa": _PLAIN_TEXT_MSG})
    result = drain_once(api_factory=lambda: fake)
    assert result.duplicates == 1
    assert result.processed == 0
    # Still removed from UNREAD so the next pass doesn't re-see it.
    assert fake._marked_read == ["msg_id_aaa"]


def test_drain_once_caps_at_max_per_pass(monkeypatch, tmp_path):
    _override_poller_settings(
        monkeypatch, tmp_path, gmail_inbox_max_per_pass=2,
    )
    _stub_crm(monkeypatch, capture={"opp_id_returns": ""})
    _stub_billing(monkeypatch, state="unknown")

    msgs = {}
    for i in range(3):
        msgs[f"msg_{i}"] = (
            f"Message-ID: <m{i}@x>\r\nFrom: a@b.com\r\nTo: c@d.com\r\n"
            f"Subject: s{i}\r\nDate: Mon, 12 May 2026 10:15:00 +0000\r\n"
            "Content-Type: text/plain\r\n\r\nbody\r\n"
        ).encode("ascii")
    fake = _FakeApiClient(msgs)
    result = drain_once(api_factory=lambda: fake)
    # The factory is asked for max_results, and our fake honors it -> 2 fetched.
    assert result.fetched == 2
    assert result.processed == 2
    assert len(fake._marked_read) == 2


def test_drain_once_api_error_surfaces_in_result(monkeypatch, tmp_path):
    _override_poller_settings(monkeypatch, tmp_path)
    from backend.intake.gmail_api_client import GmailApiError

    def _factory():
        raise GmailApiError("oauth_http_401: invalid_grant")

    result = drain_once(api_factory=_factory)
    assert result.enabled is True
    assert result.fetched == 0
    assert "gmail_api_error" in result.connect_error
    assert "invalid_grant" in result.connect_error


def test_drain_once_network_error_surfaces_in_result(monkeypatch, tmp_path):
    _override_poller_settings(monkeypatch, tmp_path)

    def _factory():
        raise OSError("[Errno 113] No route to host")

    result = drain_once(api_factory=_factory)
    assert result.enabled is True
    assert "network_error" in result.connect_error
