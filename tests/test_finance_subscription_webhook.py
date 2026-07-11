"""Cut 2 — Stripe subscription-mode checkout webhook handler tests.

Covers the new ``session.mode == 'subscription'`` dispatch in
``handle_stripe_webhook``: state advance to 'renewed' (only from
'delivered'), MRR computation, no auto-fulfill, graceful handling of
non-existent customers and non-delivered customers.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time


# ---------------------------------------------------------------------------
# Helpers (mirror test_finance_webhook.py's fixture style)
# ---------------------------------------------------------------------------

def _sign(payload_bytes: bytes, secret: str, *, timestamp: int | None = None) -> str:
    ts = timestamp if timestamp is not None else int(time.time())
    signed_payload = f"{ts}.".encode("utf-8") + payload_bytes
    sig = hmac.new(secret.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()
    return f"t={ts},v1={sig}"


def _subscription_session_event(
    *,
    event_id: str = "evt_sub_1",
    email: str = "alice@example.com",
    offer: str = "seo_optimization",
    amount_cents: int = 30000,  # $300/mo
    subscription_id: str = "sub_1Test",
    price_id: str = "price_1TKu0aBdo4u4gpS7I6ZPKIy6",
    payment_status: str = "paid",
    client_reference_id: str = "",
) -> dict:
    obj: dict = {
        "object": "checkout.session",
        "customer_details": {"email": email},
        "amount_total": amount_cents,
        "currency": "usd",
        "payment_status": payment_status,
        "mode": "subscription",
        "subscription": subscription_id,
        "metadata": {"hf_offer_code": offer},
    }
    if price_id:
        # Mirror how Stripe inlines the price when line_items are expanded.
        obj["line_items"] = {
            "data": [{"price": {"id": price_id}}],
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


def _override_settings(monkeypatch, *, stripe_webhook_secret: str = "whsec_test",
                       auto_fulfill_offer_codes: list[str] | None = None):
    class _S:
        pass
    s = _S()
    s.stripe_webhook_secret = stripe_webhook_secret
    s.auto_fulfill_offer_codes = list(auto_fulfill_offer_codes or [])
    import backend.finance.webhook as web
    monkeypatch.setattr(web, "get_settings", lambda: s)


def _isolate_log(monkeypatch, tmp_path):
    monkeypatch.setenv("SAMUS_STRIPE_EVENT_LOG", str(tmp_path / "stripe_events.jsonl"))


# ---------------------------------------------------------------------------
# In-memory CustomerStore stub (copied from test_finance_webhook.py)
# ---------------------------------------------------------------------------

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
        self.fail_on_create = False
        self.fail_on_advance = False

    def get_by_email(self, email):
        self.calls.append(("get_by_email", email))
        return self.customers.get(email.lower())

    def create_customer(self, *, email, name="", company="", source="manual",
                        metadata=None):
        self.calls.append(("create_customer", email, source))
        if self.fail_on_create:
            raise RuntimeError("simulated neo4j down")
        c = _FakeCustomer(id_=f"cust_{email.replace('@', '_')}", email=email)
        self.customers[email.lower()] = c
        return c

    def advance_state(self, *, customer_id, to_state, reason="", metadata=None):
        self.calls.append(("advance_state", customer_id, to_state))
        if self.fail_on_advance:
            raise RuntimeError("simulated neo4j down")
        for c in self.customers.values():
            if c.id == customer_id:
                prev = c.current_state
                c.current_state = to_state
                return _FakeEvent(from_state=prev, to_state=to_state)
        raise ValueError(f"unknown_id: {customer_id}")


# ---------------------------------------------------------------------------
# _extract_subscription_fields unit
# ---------------------------------------------------------------------------

def test_extract_subscription_fields_pulls_mode_and_ids():
    from backend.finance.webhook import _extract_subscription_fields
    event = _subscription_session_event(
        subscription_id="sub_abc", price_id="price_xyz",
        client_reference_id="upsell_touch_2_abc",
    )
    mode, sub_id, price_id, cref = _extract_subscription_fields(event["data"])
    assert mode == "subscription"
    assert sub_id == "sub_abc"
    assert price_id == "price_xyz"
    assert cref == "upsell_touch_2_abc"


def test_extract_subscription_fields_falls_back_to_metadata_price_id():
    """When line_items isn't expanded, look at subscription_data.metadata."""
    from backend.finance.webhook import _extract_subscription_fields
    event = _subscription_session_event(price_id="")  # no line_items branch
    event["data"]["object"]["subscription_data"] = {
        "metadata": {"hf_target_price_id": "price_from_metadata"},
    }
    _, _, price_id, _ = _extract_subscription_fields(event["data"])
    assert price_id == "price_from_metadata"


def test_extract_subscription_fields_empty_for_payment_mode():
    """A payment-mode session yields mode='payment' and empty sub fields."""
    from backend.finance.webhook import _extract_subscription_fields
    event = {
        "data": {"object": {
            "object": "checkout.session",
            "mode": "payment",
        }},
    }
    mode, sub_id, price_id, cref = _extract_subscription_fields(event["data"])
    assert mode == "payment"
    assert sub_id == ""
    assert price_id == ""
    assert cref == ""


def test_extract_subscription_fields_handles_missing_object():
    """An empty event_data returns empties without raising."""
    from backend.finance.webhook import _extract_subscription_fields
    mode, sub_id, price_id, cref = _extract_subscription_fields({})
    assert (mode, sub_id, price_id, cref) == ("", "", "", "")


# ---------------------------------------------------------------------------
# Subscription-mode dispatch in handle_stripe_webhook
# ---------------------------------------------------------------------------

def test_subscription_advances_delivered_customer_to_renewed(tmp_path, monkeypatch):
    _isolate_log(monkeypatch, tmp_path)
    _override_settings(monkeypatch)
    store = _FakeStore()
    # Audit buyer who already received their deliverable.
    store.customers["upgrade@example.com"] = _FakeCustomer(
        id_="cust_upgrade", email="upgrade@example.com",
        current_state="delivered",
    )

    event = _subscription_session_event(
        event_id="evt_sub_advance",
        email="upgrade@example.com",
        subscription_id="sub_real_42",
        amount_cents=30000,
    )
    body = json.dumps(event).encode("utf-8")
    header = _sign(body, "whsec_test")

    from backend.finance.webhook import handle_stripe_webhook
    result = handle_stripe_webhook(body, header, customer_store=store)

    assert result.process_status == "processed"
    assert result.session_mode == "subscription"
    assert result.subscription_id == "sub_real_42"
    assert result.subscription_mrr_usd == 300.0
    assert result.customer_advanced_to == "renewed"
    # advance_state should have been called once, with to_state='renewed'.
    advances = [c for c in store.calls if c[0] == "advance_state"]
    assert len(advances) == 1
    assert advances[0][2] == "renewed"
    # Customer in-store reflects the new state.
    assert store.customers["upgrade@example.com"].current_state == "renewed"


def test_subscription_unpaid_does_not_advance_but_records_event(
    tmp_path, monkeypatch, caplog,
):
    """RT FIN-01 — an 'unpaid' subscription for a delivered customer must NOT
    advance to renewed, but the event is still recorded."""
    _isolate_log(monkeypatch, tmp_path)
    _override_settings(monkeypatch)
    store = _FakeStore()
    store.customers["upgrade@example.com"] = _FakeCustomer(
        id_="cust_upgrade", email="upgrade@example.com",
        current_state="delivered",
    )

    event = _subscription_session_event(
        event_id="evt_sub_unpaid", email="upgrade@example.com",
        subscription_id="sub_unpaid_1", payment_status="unpaid",
    )
    body = json.dumps(event).encode("utf-8")
    header = _sign(body, "whsec_test")

    import logging
    with caplog.at_level(logging.WARNING, logger="samus.finance.webhook"):
        from backend.finance.webhook import handle_stripe_webhook
        result = handle_stripe_webhook(body, header, customer_store=store)

    assert result.process_status == "processed"
    # Withheld — no advance to renewed.
    assert result.customer_advanced_to == "delivered"
    assert store.customers["upgrade@example.com"].current_state == "delivered"
    assert not any(c[0] == "advance_state" for c in store.calls)
    assert any(
        "subscription_renew_withheld_pending_payment" in rec.message
        for rec in caplog.records
    )
    # Event still persisted.
    from backend.finance.webhook import load_recent_events
    rows = [e for e in load_recent_events(window_days=7)
            if e.event_id == "evt_sub_unpaid"]
    assert len(rows) == 1
    assert rows[0].payment_status == "unpaid"


def test_subscription_no_payment_required_advances_to_renewed(
    tmp_path, monkeypatch,
):
    """RT FIN-01 — a free-trial subscription completes as 'no_payment_required'
    and is still allowed to advance a delivered customer to renewed."""
    _isolate_log(monkeypatch, tmp_path)
    _override_settings(monkeypatch)
    store = _FakeStore()
    store.customers["trial@example.com"] = _FakeCustomer(
        id_="cust_trial", email="trial@example.com",
        current_state="delivered",
    )

    event = _subscription_session_event(
        event_id="evt_sub_trial", email="trial@example.com",
        subscription_id="sub_trial_1", payment_status="no_payment_required",
    )
    body = json.dumps(event).encode("utf-8")
    header = _sign(body, "whsec_test")

    from backend.finance.webhook import handle_stripe_webhook
    result = handle_stripe_webhook(body, header, customer_store=store)

    assert result.process_status == "processed"
    assert result.customer_advanced_to == "renewed"
    advances = [c for c in store.calls if c[0] == "advance_state"]
    assert len(advances) == 1
    assert advances[0][2] == "renewed"


def test_subscription_for_prospect_records_event_no_state_mutation(
    tmp_path, monkeypatch, caplog,
):
    """Brand-new subscriber (not a prior audit buyer): record but don't advance."""
    _isolate_log(monkeypatch, tmp_path)
    _override_settings(monkeypatch)
    store = _FakeStore()
    # Pre-existing customer at 'prospect' — e.g. cold list import.
    store.customers["new@example.com"] = _FakeCustomer(
        id_="cust_new", email="new@example.com", current_state="prospect",
    )

    event = _subscription_session_event(
        event_id="evt_sub_prospect",
        email="new@example.com",
        subscription_id="sub_new_99",
    )
    body = json.dumps(event).encode("utf-8")
    header = _sign(body, "whsec_test")

    import logging
    with caplog.at_level(logging.WARNING, logger="samus.finance.webhook"):
        from backend.finance.webhook import handle_stripe_webhook
        result = handle_stripe_webhook(body, header, customer_store=store)

    assert result.process_status == "processed"
    assert result.session_mode == "subscription"
    assert result.subscription_id == "sub_new_99"
    # No state mutation.
    assert result.customer_advanced_to == "prospect"
    assert store.customers["new@example.com"].current_state == "prospect"
    # advance_state should NOT have been called.
    assert not any(c[0] == "advance_state" for c in store.calls)
    # And the warning fired.
    assert any(
        "subscription_for_non_delivered_customer" in rec.message
        for rec in caplog.records
    )

    # Event row still persisted with subscription fields.
    from backend.finance.webhook import load_recent_events
    rows = [
        e for e in load_recent_events(window_days=7)
        if e.event_id == "evt_sub_prospect"
    ]
    assert len(rows) == 1
    row = rows[0]
    assert row.session_mode == "subscription"
    assert row.subscription_id == "sub_new_99"
    assert row.subscription_mrr_usd == 300.0


def test_subscription_for_new_email_creates_customer_no_advance(
    tmp_path, monkeypatch,
):
    """No matching customer in store: create at 'prospect', do NOT advance."""
    _isolate_log(monkeypatch, tmp_path)
    _override_settings(monkeypatch)
    store = _FakeStore()

    event = _subscription_session_event(
        event_id="evt_sub_brand_new", email="brandnew@example.com",
        subscription_id="sub_brand_new",
    )
    body = json.dumps(event).encode("utf-8")
    header = _sign(body, "whsec_test")

    from backend.finance.webhook import handle_stripe_webhook
    result = handle_stripe_webhook(body, header, customer_store=store)

    # Handler doesn't crash — best-effort path.
    assert result.process_status == "processed"
    assert result.session_mode == "subscription"
    # Customer was created (handler always tries get_by_email -> create on miss).
    kinds = [c[0] for c in store.calls]
    assert "create_customer" in kinds
    # But no state advance (new customer is at 'prospect', not 'delivered').
    assert not any(c[0] == "advance_state" for c in store.calls)


def test_subscription_skips_auto_fulfill_even_when_whitelisted(
    tmp_path, monkeypatch,
):
    """Subscription sessions never trigger auto-fulfill, regardless of whitelist."""
    _isolate_log(monkeypatch, tmp_path)
    _override_settings(
        monkeypatch,
        # 'seo_optimization' is whitelisted AND the session has a URL.
        auto_fulfill_offer_codes=["seo_optimization"],
    )
    store = _FakeStore()
    store.customers["upgrade@example.com"] = _FakeCustomer(
        id_="cust_upgrade", email="upgrade@example.com",
        current_state="delivered",
    )

    scheduled: list = []
    import backend.finance.webhook as web
    monkeypatch.setattr(
        web, "_schedule_auto_fulfill",
        lambda **kw: scheduled.append(kw),
    )

    event = _subscription_session_event(
        event_id="evt_sub_no_autofulfill",
        email="upgrade@example.com", offer="seo_optimization",
    )
    # Even with a website_url custom_field present, subscription path skips.
    event["data"]["object"]["custom_fields"] = [
        {
            "key": "website_url", "type": "text",
            "text": {"value": "https://upgrade.example.com"},
        },
    ]
    body = json.dumps(event).encode("utf-8")
    header = _sign(body, "whsec_test")

    from backend.finance.webhook import handle_stripe_webhook
    result = handle_stripe_webhook(body, header, customer_store=store)

    assert result.process_status == "processed"
    assert result.session_mode == "subscription"
    assert result.auto_fulfill_scheduled is False
    assert scheduled == []


def test_subscription_mrr_calculated_from_amount_total(tmp_path, monkeypatch):
    """MRR = amount_total / 100, in dollars."""
    _isolate_log(monkeypatch, tmp_path)
    _override_settings(monkeypatch)
    store = _FakeStore()
    store.customers["x@example.com"] = _FakeCustomer(
        id_="cust_x", email="x@example.com", current_state="delivered",
    )

    event = _subscription_session_event(
        event_id="evt_sub_mrr_calc",
        email="x@example.com", amount_cents=12500,  # $125.00/mo
    )
    body = json.dumps(event).encode("utf-8")
    header = _sign(body, "whsec_test")

    from backend.finance.webhook import handle_stripe_webhook
    result = handle_stripe_webhook(body, header, customer_store=store)

    assert result.subscription_mrr_usd == 125.0

    # Verify the persisted row carries the same MRR field.
    from backend.finance.webhook import load_recent_events
    rows = [
        e for e in load_recent_events(window_days=7)
        if e.event_id == "evt_sub_mrr_calc"
    ]
    assert len(rows) == 1
    assert rows[0].subscription_mrr_usd == 125.0
    assert rows[0].subscription_id == "sub_1Test"


def test_subscription_skips_receipt_send(tmp_path, monkeypatch):
    """Subscription path must NOT call send_payment_receipt (Stripe sends one)."""
    _isolate_log(monkeypatch, tmp_path)
    _override_settings(monkeypatch)
    store = _FakeStore()
    store.customers["sub@example.com"] = _FakeCustomer(
        id_="cust_sub", email="sub@example.com", current_state="delivered",
    )

    called: list = []
    import backend.finance.webhook as web
    monkeypatch.setattr(
        web, "send_payment_receipt",
        lambda **kw: called.append(kw) or {
            "sent": True, "message_id": "msg_x", "error": "",
        },
    )

    event = _subscription_session_event(
        event_id="evt_sub_no_receipt", email="sub@example.com",
    )
    body = json.dumps(event).encode("utf-8")
    header = _sign(body, "whsec_test")

    from backend.finance.webhook import handle_stripe_webhook
    result = handle_stripe_webhook(body, header, customer_store=store)

    assert result.process_status == "processed"
    assert called == []  # receipt suppressed for subscription mode


def test_subscription_client_reference_id_persisted(tmp_path, monkeypatch):
    """Cut 3 attribution column — extracted + stored, NOT acted on here."""
    _isolate_log(monkeypatch, tmp_path)
    _override_settings(monkeypatch)
    store = _FakeStore()
    store.customers["c@example.com"] = _FakeCustomer(
        id_="cust_c", email="c@example.com", current_state="delivered",
    )

    event = _subscription_session_event(
        event_id="evt_sub_cref", email="c@example.com",
        client_reference_id="upsell_touch_2_abc123",
    )
    body = json.dumps(event).encode("utf-8")
    header = _sign(body, "whsec_test")

    from backend.finance.webhook import handle_stripe_webhook
    handle_stripe_webhook(body, header, customer_store=store)

    from backend.finance.webhook import load_recent_events
    rows = [
        e for e in load_recent_events(window_days=7)
        if e.event_id == "evt_sub_cref"
    ]
    assert len(rows) == 1
    assert rows[0].client_reference_id == "upsell_touch_2_abc123"


def test_payment_mode_unchanged_by_cut2(tmp_path, monkeypatch):
    """Regression guard: classic audit purchase still advances prospect->paid."""
    _isolate_log(monkeypatch, tmp_path)
    _override_settings(monkeypatch)
    store = _FakeStore()

    # Reuse the payment-mode event shape from test_finance_webhook.py.
    obj = {
        "object": "checkout.session",
        "customer_details": {"email": "audit@example.com"},
        "amount_total": 14900,
        "currency": "usd",
        "payment_status": "paid",
        "mode": "payment",
        "metadata": {"hf_offer_code": "seo_audit"},
    }
    event = {
        "id": "evt_pay_regress",
        "type": "checkout.session.completed",
        "livemode": True,
        "created": int(time.time()),
        "data": {"object": obj},
    }
    body = json.dumps(event).encode("utf-8")
    header = _sign(body, "whsec_test")

    from backend.finance.webhook import handle_stripe_webhook
    result = handle_stripe_webhook(body, header, customer_store=store)

    assert result.process_status == "processed"
    assert result.session_mode == "payment"
    assert result.subscription_id == ""
    assert result.subscription_mrr_usd is None
    assert result.customer_advanced_to == "paid"
