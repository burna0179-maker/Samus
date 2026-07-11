"""Cut 3 integration: subscription webhook reads client_reference_id and
calls upsell_queue.mark_converted() to credit the right touch."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from datetime import datetime, timedelta, timezone

import pytest


# ---------------------------------------------------------------------------
# Reused helpers (mirror tests/test_finance_subscription_webhook.py shape)
# ---------------------------------------------------------------------------


def _sign(payload_bytes: bytes, secret: str, *, timestamp: int | None = None) -> str:
    ts = timestamp if timestamp is not None else int(time.time())
    signed_payload = f"{ts}.".encode("utf-8") + payload_bytes
    sig = hmac.new(secret.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()
    return f"t={ts},v1={sig}"


def _subscription_event(
    *,
    event_id: str,
    email: str,
    subscription_id: str = "sub_test_42",
    amount_cents: int = 30000,
    client_reference_id: str = "",
) -> dict:
    """Stripe checkout.session.completed for mode='subscription'."""
    obj = {
        "object": "checkout.session",
        "mode": "subscription",
        "customer_details": {"email": email},
        "amount_total": amount_cents,
        "currency": "usd",
        "payment_status": "paid",
        "subscription": subscription_id,
        "metadata": {},
    }
    if client_reference_id:
        obj["client_reference_id"] = client_reference_id
    return {
        "id": event_id,
        "type": "checkout.session.completed",
        "livemode": True,
        "created": int(time.time()),
        "data": {"object": obj},
    }


def _override_settings(
    monkeypatch,
    *,
    stripe_webhook_secret: str = "whsec_test",
    auto_fulfill_offer_codes: list[str] | None = None,
):
    class _S:
        pass

    s = _S()
    s.stripe_webhook_secret = stripe_webhook_secret
    s.auto_fulfill_offer_codes = list(auto_fulfill_offer_codes or [])
    import backend.finance.webhook as web

    monkeypatch.setattr(web, "get_settings", lambda: s)


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
        self.calls: list[tuple] = []

    def get_by_email(self, email):
        self.calls.append(("get_by_email", email))
        return self.customers.get(email.lower())

    def create_customer(self, *, email, name="", company="", source="manual", metadata=None):
        self.calls.append(("create_customer", email, source))
        c = _FakeCustomer(id_=f"cust_{email.replace('@', '_')}", email=email)
        self.customers[email.lower()] = c
        return c

    def advance_state(self, *, customer_id, to_state, reason="", metadata=None):
        self.calls.append(("advance_state", customer_id, to_state))
        for c in self.customers.values():
            if c.id == customer_id:
                prev = c.current_state
                c.current_state = to_state
                return _FakeEvent(from_state=prev, to_state=to_state)
        raise ValueError(f"unknown_id: {customer_id}")


@pytest.fixture(autouse=True)
def _isolate_logs(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "SAMUS_STRIPE_EVENT_LOG",
        str(tmp_path / "stripe_events.jsonl"),
    )
    monkeypatch.setenv(
        "SAMUS_UPSELL_QUEUE_PATH",
        str(tmp_path / "upsell_queue.jsonl"),
    )


# ---------------------------------------------------------------------------
# Attribution: subscription with valid client_reference_id -> mark_converted
# ---------------------------------------------------------------------------


def test_subscription_with_upsell_ref_marks_converted(tmp_path, monkeypatch):
    """End-to-end: enqueue upsell + mark sent → subscription webhook for
    same customer fires with client_reference_id=upsell_<event_id> →
    upsell_queue has a 'converted' transition row."""
    _override_settings(monkeypatch)
    store = _FakeStore()
    # Pre-existing customer in delivered (audit was fulfilled)
    store.customers["c@x.com"] = _FakeCustomer(
        id_="cust_c",
        email="c@x.com",
        current_state="delivered",
    )

    # Set up the upsell queue: enqueue + mark_sent (simulating runner ran)
    from backend.finance.upsell_queue import (
        _read_all_rows,
        enqueue_upsell,
        mark_sent,
    )

    enqueue_upsell(
        customer_id="cust_c",
        customer_email="c@x.com",
        source_offer_code="seo_audit",
        delivered_at=datetime.now(timezone.utc) - timedelta(days=40),
    )
    # Get the touch-1 queued row and "send" it
    queued_rows = [r for r in _read_all_rows() if r.kind == "queued" and r.touch_num == 1]
    assert len(queued_rows) == 1
    touch_1 = queued_rows[0]
    mark_sent(queued_row=touch_1, message_id="msg_t1")

    # Stripe fires checkout.session.completed with the client_reference_id
    # that the upsell email URL would have included.
    event = _subscription_event(
        event_id="evt_conv",
        email="c@x.com",
        subscription_id="sub_first_seo_opt",
        amount_cents=30000,  # $300/mo SEO Optimization
        client_reference_id=f"upsell_{touch_1.event_id}",
    )
    body = json.dumps(event).encode("utf-8")
    header = _sign(body, "whsec_test")
    from backend.finance.webhook import handle_stripe_webhook

    result = handle_stripe_webhook(body, header, customer_store=store)
    assert result.process_status == "processed"
    assert result.subscription_id == "sub_first_seo_opt"
    assert result.customer_advanced_to == "renewed"

    # The upsell queue should now have a 'converted' row for touch 1
    rows_after = _read_all_rows()
    converted = [r for r in rows_after if r.kind == "converted"]
    assert len(converted) == 1
    assert converted[0].touch_num == 1
    assert converted[0].customer_id == "cust_c"
    assert converted[0].subscription_id == "sub_first_seo_opt"


def test_subscription_without_client_reference_id_no_attribution(monkeypatch):
    """A direct subscription purchase (no upsell link) must not write
    a converted row — there's nothing to attribute."""
    _override_settings(monkeypatch)
    store = _FakeStore()
    store.customers["x@x.com"] = _FakeCustomer(
        id_="cust_x",
        email="x@x.com",
        current_state="delivered",
    )
    # No prior upsell rows in the queue.

    event = _subscription_event(
        event_id="evt_direct",
        email="x@x.com",
        subscription_id="sub_direct",
        client_reference_id="",  # no ref
    )
    body = json.dumps(event).encode("utf-8")
    header = _sign(body, "whsec_test")
    from backend.finance.webhook import handle_stripe_webhook

    handle_stripe_webhook(body, header, customer_store=store)

    from backend.finance.upsell_queue import _read_all_rows

    assert not any(r.kind == "converted" for r in _read_all_rows())


def test_subscription_with_malformed_ref_no_attribution_no_crash(monkeypatch):
    """A client_reference_id that doesn't match an upsell touch must NOT
    crash the webhook; just log + no-op."""
    _override_settings(monkeypatch)
    store = _FakeStore()
    store.customers["c@x.com"] = _FakeCustomer(
        id_="cust_c",
        email="c@x.com",
        current_state="delivered",
    )

    event = _subscription_event(
        event_id="evt_orphan",
        email="c@x.com",
        subscription_id="sub_orphan",
        client_reference_id="upsell_does_not_exist_12345",
    )
    body = json.dumps(event).encode("utf-8")
    header = _sign(body, "whsec_test")
    from backend.finance.webhook import handle_stripe_webhook

    result = handle_stripe_webhook(body, header, customer_store=store)
    # Webhook still processed (state advance happened)
    assert result.process_status == "processed"
    # No converted row was written
    from backend.finance.upsell_queue import _read_all_rows

    assert not any(r.kind == "converted" for r in _read_all_rows())


def test_payment_mode_session_does_not_attempt_attribution(monkeypatch):
    """Cut 3 attribution must only trigger on subscription mode; a payment
    mode session with a coincidentally-named ref must not call mark_converted."""
    _override_settings(monkeypatch)
    store = _FakeStore()

    obj = {
        "object": "checkout.session",
        "mode": "payment",
        "customer_details": {"email": "y@x.com"},
        "amount_total": 14900,
        "currency": "usd",
        "payment_status": "paid",
        "metadata": {"hf_offer_code": "seo_audit"},
        "client_reference_id": "upsell_should_not_match",
    }
    event = {
        "id": "evt_pay_with_ref",
        "type": "checkout.session.completed",
        "livemode": True,
        "created": int(time.time()),
        "data": {"object": obj},
    }
    body = json.dumps(event).encode("utf-8")
    header = _sign(body, "whsec_test")
    from backend.finance.webhook import handle_stripe_webhook

    handle_stripe_webhook(body, header, customer_store=store)
    from backend.finance.upsell_queue import _read_all_rows

    assert not any(r.kind == "converted" for r in _read_all_rows())


def test_subscription_attribution_for_touch_2(monkeypatch):
    """Verify the touch_num is correctly extracted from the matched queue row
    (not hardcoded to 1) — touch 2 closing should credit touch 2."""
    _override_settings(monkeypatch)
    store = _FakeStore()
    store.customers["c@x.com"] = _FakeCustomer(
        id_="cust_c",
        email="c@x.com",
        current_state="delivered",
    )

    from backend.finance.upsell_queue import (
        _read_all_rows,
        enqueue_upsell,
        mark_sent,
    )

    enqueue_upsell(
        customer_id="cust_c",
        customer_email="c@x.com",
        source_offer_code="seo_audit",
        delivered_at=datetime.now(timezone.utc) - timedelta(days=40),
    )
    touch_2 = [r for r in _read_all_rows() if r.kind == "queued" and r.touch_num == 2][0]
    mark_sent(queued_row=touch_2, message_id="msg_t2")

    event = _subscription_event(
        event_id="evt_t2_conv",
        email="c@x.com",
        client_reference_id=f"upsell_{touch_2.event_id}",
    )
    body = json.dumps(event).encode("utf-8")
    header = _sign(body, "whsec_test")
    from backend.finance.webhook import handle_stripe_webhook

    handle_stripe_webhook(body, header, customer_store=store)

    converted = [r for r in _read_all_rows() if r.kind == "converted"]
    assert len(converted) == 1
    assert converted[0].touch_num == 2  # NOT hardcoded to 1
