"""Payment-receipt template + send-glue tests, plus webhook integration tests
that confirm the receipt is fired on a processed checkout.session.completed
and that a receipt failure does NOT break the webhook flow.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any

import pytest


# ---------------------------------------------------------------------------
# Pure render tests
# ---------------------------------------------------------------------------

def test_offer_display_name_known_acronyms():
    from backend.finance.receipts import _offer_display_name
    assert _offer_display_name("seo_audit") == "SEO Audit"
    assert _offer_display_name("seo-implementation") == "SEO Implementation"
    assert _offer_display_name("crm_setup") == "CRM Setup"
    assert _offer_display_name("api_integration") == "API Integration"


def test_offer_display_name_title_cases_plain_words():
    from backend.finance.receipts import _offer_display_name
    assert _offer_display_name("workflow_rescue") == "Workflow Rescue"
    assert _offer_display_name("monthly_retainer") == "Monthly Retainer"


def test_offer_display_name_falls_back_on_empty_or_whitespace():
    from backend.finance.receipts import _offer_display_name
    assert _offer_display_name("") == "your purchase"
    assert _offer_display_name("   ") == "your purchase"


def test_format_amount_none_amount_returns_placeholder():
    from backend.finance.receipts import _format_amount
    assert _format_amount(None, "usd") == "(amount unavailable)"


def test_format_amount_uppercases_currency_and_groups_thousands():
    from backend.finance.receipts import _format_amount
    assert _format_amount(149.0, "usd") == "$149.00 USD"
    assert _format_amount(1234.5, "usd") == "$1,234.50 USD"
    assert _format_amount(50000.0, "eur") == "$50,000.00 EUR"


def test_render_payment_receipt_happy_path_includes_all_fields():
    from backend.finance.receipts import render_payment_receipt
    subject, text, html = render_payment_receipt(
        amount_total_usd=149.0,
        currency="usd",
        hf_offer_code="seo_audit",
        event_id="evt_test_123",
        received_at="2026-05-15T18:00:00Z",
    )
    assert "SEO Audit" in subject
    assert "$149.00 USD" in subject

    for needle in ("SEO Audit", "(seo_audit)", "$149.00 USD",
                   "2026-05-15T18:00:00Z", "evt_test_123",
                   "HustleForge"):
        assert needle in text, f"text body missing: {needle!r}"
        assert needle in html, f"html body missing: {needle!r}"

    # HTML is structured (not just text-wrapped)
    assert "<table" in html
    assert "<code>" in html


def test_render_payment_receipt_handles_missing_offer_code():
    from backend.finance.receipts import render_payment_receipt
    subject, text, html = render_payment_receipt(
        amount_total_usd=500.0, currency="usd",
        hf_offer_code="", event_id="evt_x", received_at="2026-05-15T00:00:00Z",
    )
    assert "your purchase" in subject
    assert "your purchase" in text
    assert "your purchase" in html
    # No empty parenthetical when offer code is absent
    assert "()" not in text
    assert "<code></code>" not in html


def test_render_payment_receipt_handles_missing_amount():
    from backend.finance.receipts import render_payment_receipt
    subject, text, html = render_payment_receipt(
        amount_total_usd=None, currency="usd",
        hf_offer_code="seo_audit", event_id="evt_x",
        received_at="2026-05-15T00:00:00Z",
    )
    assert "(amount unavailable)" in subject
    assert "(amount unavailable)" in text
    assert "(amount unavailable)" in html


# ---------------------------------------------------------------------------
# send_payment_receipt — never raises; returns outcome dict
# ---------------------------------------------------------------------------

def test_send_payment_receipt_happy_path(monkeypatch):
    """When send_email succeeds, receipt returns sent=True with message id."""
    captured: dict = {}

    def _fake_send_email(**kw: Any) -> dict[str, str]:
        captured.update(kw)
        return {"message_id": "sg_ok_42", "channel": "email",
                "to": kw["to"], "ts": "2026-05-15T00:00:00Z"}

    import backend.finance.receipts as mod
    monkeypatch.setattr(mod, "send_email", _fake_send_email)

    out = mod.send_payment_receipt(
        customer_email="alice@example.com",
        amount_total_usd=149.0, currency="usd",
        hf_offer_code="seo_audit", event_id="evt_1",
        received_at="2026-05-15T00:00:00Z",
    )
    assert out == {"sent": True, "message_id": "sg_ok_42", "error": ""}
    assert captured["to"] == "alice@example.com"
    assert "SEO Audit" in captured["subject"]
    assert captured["html_body"]  # html included
    assert captured["body"]  # text body


def test_send_payment_receipt_no_email_returns_short_circuit(monkeypatch):
    """Empty email -> sent=False, error='no_customer_email', no backend call."""
    called: list = []

    def _fake_send_email(**kw: Any) -> dict[str, str]:
        called.append(kw)
        return {"message_id": "should_not_get_here"}

    import backend.finance.receipts as mod
    monkeypatch.setattr(mod, "send_email", _fake_send_email)

    out = mod.send_payment_receipt(
        customer_email="",
        amount_total_usd=149.0, currency="usd",
        hf_offer_code="seo_audit", event_id="evt_1",
        received_at="2026-05-15T00:00:00Z",
    )
    assert out == {"sent": False, "message_id": "", "error": "no_customer_email"}
    assert called == []  # backend never invoked


def test_send_payment_receipt_whitespace_email_short_circuits(monkeypatch):
    called: list = []
    import backend.finance.receipts as mod
    monkeypatch.setattr(mod, "send_email", lambda **kw: called.append(kw))

    out = mod.send_payment_receipt(
        customer_email="   ",
        amount_total_usd=149.0, currency="usd",
        hf_offer_code="seo_audit", event_id="evt_x",
        received_at="2026-05-15T00:00:00Z",
    )
    assert out["sent"] is False
    assert out["error"] == "no_customer_email"
    assert called == []


def test_send_payment_receipt_swallows_emailbackenderror(monkeypatch):
    """EmailBackendError from the selector -> recorded, not raised."""
    from backend.common.email_backend import EmailBackendError
    import backend.finance.receipts as mod

    def _boom(**kw: Any) -> dict[str, str]:
        raise EmailBackendError("sendgrid_http_403: not verified")

    monkeypatch.setattr(mod, "send_email", _boom)

    out = mod.send_payment_receipt(
        customer_email="a@b.com",
        amount_total_usd=100.0, currency="usd",
        hf_offer_code="seo_audit", event_id="evt_e",
        received_at="2026-05-15T00:00:00Z",
    )
    assert out["sent"] is False
    assert out["message_id"] == ""
    assert "not verified" in out["error"]


def test_send_payment_receipt_swallows_value_error(monkeypatch):
    """ValueError (e.g., api_key unset) -> recorded, not raised."""
    import backend.finance.receipts as mod

    def _boom(**kw: Any) -> dict[str, str]:
        raise ValueError("send_email_via_sendgrid requires api_key")

    monkeypatch.setattr(mod, "send_email", _boom)

    out = mod.send_payment_receipt(
        customer_email="a@b.com",
        amount_total_usd=100.0, currency="usd",
        hf_offer_code="seo_audit", event_id="evt_v",
        received_at="2026-05-15T00:00:00Z",
    )
    assert out["sent"] is False
    assert "api_key" in out["error"]


def test_send_payment_receipt_swallows_not_implemented(monkeypatch):
    """E.g., EMAIL_BACKEND=ses but ses not lifted to common -> recorded."""
    import backend.finance.receipts as mod

    def _boom(**kw: Any) -> dict[str, str]:
        raise NotImplementedError("common email adapter does not yet route to SES")

    monkeypatch.setattr(mod, "send_email", _boom)

    out = mod.send_payment_receipt(
        customer_email="a@b.com",
        amount_total_usd=100.0, currency="usd",
        hf_offer_code="seo_audit", event_id="evt_ni",
        received_at="2026-05-15T00:00:00Z",
    )
    assert out["sent"] is False
    assert "SES" in out["error"] or "ses" in out["error"]


def test_send_payment_receipt_truncates_long_errors(monkeypatch):
    """Defensive: very long backend errors get truncated to 200 chars."""
    import backend.finance.receipts as mod
    long_msg = "x" * 1000

    def _boom(**kw: Any) -> dict[str, str]:
        raise ValueError(long_msg)

    monkeypatch.setattr(mod, "send_email", _boom)
    out = mod.send_payment_receipt(
        customer_email="a@b.com", amount_total_usd=1.0, currency="usd",
        hf_offer_code="x", event_id="evt_long",
        received_at="2026-05-15T00:00:00Z",
    )
    assert len(out["error"]) == 200


# ---------------------------------------------------------------------------
# Webhook integration — receipt is fired + recorded; receipt failure does
# NOT change process_status or raise
# ---------------------------------------------------------------------------

def _sign(payload_bytes: bytes, secret: str, *, timestamp: int | None = None) -> str:
    ts = timestamp if timestamp is not None else int(time.time())
    signed_payload = f"{ts}.".encode("utf-8") + payload_bytes
    sig = hmac.new(secret.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()
    return f"t={ts},v1={sig}"


def _checkout_session_event(*, event_id: str = "evt_recpt_1",
                            email: str = "alice@example.com",
                            offer: str = "seo_audit",
                            amount_cents: int = 14900) -> dict:
    return {
        "id": event_id, "type": "checkout.session.completed",
        "livemode": True, "created": int(time.time()),
        "data": {"object": {
            "object": "checkout.session",
            "customer_details": {"email": email},
            "amount_total": amount_cents, "currency": "usd",
            "payment_status": "paid",
            "metadata": {"hf_offer_code": offer},
        }},
    }


def _override_settings(monkeypatch, *, stripe_webhook_secret: str = "whsec_test"):
    class _S:
        pass
    s = _S()
    s.stripe_webhook_secret = stripe_webhook_secret
    import backend.finance.webhook as web
    monkeypatch.setattr(web, "get_settings", lambda: s)


def _isolate_log(monkeypatch, tmp_path):
    monkeypatch.setenv("SAMUS_STRIPE_EVENT_LOG", str(tmp_path / "stripe_events.jsonl"))


# Minimal CustomerStore stub mirrored from test_finance_webhook.py.
class _FakeCustomer:
    def __init__(self, id_: str, email: str, current_state: str = "prospect"):
        self.id = id_
        self.email = email
        self.name = ""
        self.company = ""
        self.source = "stripe_webhook"
        self.created_at = time.time()
        self.current_state = current_state
        self.current_state_since = time.time()
        self.metadata: dict = {}


class _FakeEvent:
    def __init__(self, from_state, to_state):
        self.event_id = "evt_x"
        self.customer_id = "cust_x"
        self.from_state = from_state
        self.to_state = to_state
        self.date = time.time()
        self.reason = ""
        self.metadata: dict = {}


class _FakeStore:
    def __init__(self):
        self.customers: dict[str, _FakeCustomer] = {}

    def get_by_email(self, email):
        return self.customers.get(email.lower())

    def create_customer(self, *, email, name="", company="", source="manual",
                        metadata=None):
        c = _FakeCustomer(id_=f"cust_{email.replace('@', '_')}", email=email)
        self.customers[email.lower()] = c
        return c

    def advance_state(self, *, customer_id, to_state, reason="", metadata=None):
        for c in self.customers.values():
            if c.id == customer_id:
                prev = c.current_state
                c.current_state = to_state
                return _FakeEvent(from_state=prev, to_state=to_state)
        raise ValueError("unknown_id")


def test_webhook_processed_event_fires_receipt_and_records_outcome(monkeypatch, tmp_path):
    """Happy path: receipt-send succeeds, fields land on the JSONL event row."""
    _isolate_log(monkeypatch, tmp_path)
    _override_settings(monkeypatch)
    captured: dict = {}

    def _fake_send_payment_receipt(**kw: Any) -> dict[str, Any]:
        captured.update(kw)
        return {"sent": True, "message_id": "sg_via_webhook", "error": ""}

    import backend.finance.webhook as web
    monkeypatch.setattr(web, "send_payment_receipt", _fake_send_payment_receipt)

    body = json.dumps(_checkout_session_event(event_id="evt_recpt_ok")).encode()
    header = _sign(body, "whsec_test")
    result = web.handle_stripe_webhook(body, header, customer_store=_FakeStore())

    assert result.process_status == "processed"
    # The receipt-send was called with the event fields
    assert captured["customer_email"] == "alice@example.com"
    assert captured["amount_total_usd"] == 149.0  # 14900 cents
    assert captured["hf_offer_code"] == "seo_audit"
    assert captured["event_id"] == "evt_recpt_ok"

    # And the event-log row records the receipt outcome
    log_path = web.event_log_path()
    rows = [json.loads(l) for l in log_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    last = rows[-1]
    assert last["receipt_sent"] is True
    assert last["receipt_message_id"] == "sg_via_webhook"
    assert last["receipt_error"] == ""


def test_webhook_receipt_failure_does_not_change_process_status(monkeypatch, tmp_path):
    """Receipt-send failure -> process_status stays 'processed', error recorded."""
    _isolate_log(monkeypatch, tmp_path)
    _override_settings(monkeypatch)

    def _fake_failed(**kw: Any) -> dict[str, Any]:
        return {"sent": False, "message_id": "",
                "error": "sendgrid_http_403: not verified"}

    import backend.finance.webhook as web
    monkeypatch.setattr(web, "send_payment_receipt", _fake_failed)

    body = json.dumps(_checkout_session_event(event_id="evt_recpt_fail")).encode()
    header = _sign(body, "whsec_test")
    result = web.handle_stripe_webhook(body, header, customer_store=_FakeStore())

    # Webhook still SUCCEEDS — Stripe should not retry just because email broke
    assert result.process_status == "processed"
    assert result.customer_advanced_to == "paid"

    # But the failure is captured on the event row
    log_path = web.event_log_path()
    rows = [json.loads(l) for l in log_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    last = rows[-1]
    assert last["receipt_sent"] is False
    assert last["receipt_message_id"] == ""
    assert "not verified" in last["receipt_error"]


def test_webhook_receipt_raise_would_bubble_but_send_payment_receipt_never_raises(monkeypatch, tmp_path):
    """Belt-and-suspenders: even if send_payment_receipt itself raised
    (which it's documented not to), the webhook would propagate — confirming
    the swallow-and-return contract is the only thing between Stripe and a
    spurious 500.
    """
    _isolate_log(monkeypatch, tmp_path)
    _override_settings(monkeypatch)

    def _accidentally_raises(**kw: Any) -> dict[str, Any]:
        raise RuntimeError("contract violation — send_payment_receipt must not raise")

    import backend.finance.webhook as web
    monkeypatch.setattr(web, "send_payment_receipt", _accidentally_raises)

    body = json.dumps(_checkout_session_event(event_id="evt_contract")).encode()
    header = _sign(body, "whsec_test")
    with pytest.raises(RuntimeError):
        web.handle_stripe_webhook(body, header, customer_store=_FakeStore())


def test_webhook_idempotency_prevents_double_receipt_on_stripe_retry(monkeypatch, tmp_path):
    """Stripe retries same event_id -> duplicate caught upstream; receipt-send
    fires at most once per event.
    """
    _isolate_log(monkeypatch, tmp_path)
    _override_settings(monkeypatch)
    call_count = {"n": 0}

    def _counting_send(**kw: Any) -> dict[str, Any]:
        call_count["n"] += 1
        return {"sent": True, "message_id": f"sg_{call_count['n']}", "error": ""}

    import backend.finance.webhook as web
    monkeypatch.setattr(web, "send_payment_receipt", _counting_send)

    body = json.dumps(_checkout_session_event(event_id="evt_idemp")).encode()
    header = _sign(body, "whsec_test")
    store = _FakeStore()
    first = web.handle_stripe_webhook(body, header, customer_store=store)
    second = web.handle_stripe_webhook(body, header, customer_store=store)

    assert first.process_status == "processed"
    assert second.process_status == "duplicate"
    assert call_count["n"] == 1  # receipt fired ONCE despite two webhook calls