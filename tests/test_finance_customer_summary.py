"""get_customer_billing_summary — per-customer Stripe lookup orchestrator.

Mocks StripeClient at the service-module level (mirroring
test_finance_service.py) so we exercise the routing + state-derivation
logic without any real Stripe call.
"""

from __future__ import annotations


from backend.finance.models import (
    CustomerBillingSummary,
    StripeCharge,
    StripeCustomer,
    StripeRecurring,
    StripeSubscription,
    StripeSubscriptionItem,
    StripeSubscriptionItemPrice,
)


def _override_settings(monkeypatch, *, stripe_api_key: str = "rk_test_unit"):
    class _S:
        pass

    s = _S()
    s.stripe_api_key = stripe_api_key
    import backend.finance.service as svc_mod

    monkeypatch.setattr(svc_mod, "get_settings", lambda: s)


def _stub_client(
    monkeypatch,
    *,
    customers=None,
    subscriptions=None,
    charges=None,
    customers_raise=None,
    subs_raise=None,
    charges_raise=None,
    capture=None,
):
    """Replace StripeClient inside backend.finance.service with a fake."""

    class _FakeClient:
        def __init__(self, api_key, **_kw):
            if capture is not None:
                capture["api_key"] = api_key

        def fetch_customers_by_email(self, email, *, limit=10):
            if capture is not None:
                capture["customers_email"] = email
            if customers_raise is not None:
                raise customers_raise
            return customers or []

        def fetch_subscriptions(self, *, status="active", limit=100, customer=None):
            if capture is not None:
                capture["subs_customer"] = customer
                capture["subs_status"] = status
            if subs_raise is not None:
                raise subs_raise
            return subscriptions or []

        def fetch_charges(self, limit=10, *, customer=None):
            if capture is not None:
                capture["charges_customer"] = customer
                capture["charges_limit"] = limit
            if charges_raise is not None:
                raise charges_raise
            return charges or []

    import backend.finance.service as svc_mod

    monkeypatch.setattr(svc_mod, "StripeClient", _FakeClient)


# ---------------------------------------------------------------------------
# State derivation
# ---------------------------------------------------------------------------


def test_returns_lookup_failed_when_api_key_unset(monkeypatch):
    _override_settings(monkeypatch, stripe_api_key="")
    from backend.finance.service import get_customer_billing_summary

    out = get_customer_billing_summary("alice@example.com")
    assert isinstance(out, CustomerBillingSummary)
    assert out.state == "lookup_failed"
    assert out.lookup_error == "stripe_api_key_unset"
    assert out.email == "alice@example.com"


def test_returns_unknown_when_no_customer_for_email(monkeypatch):
    _override_settings(monkeypatch)
    _stub_client(monkeypatch, customers=[])
    from backend.finance.service import get_customer_billing_summary

    out = get_customer_billing_summary("ghost@example.com")
    assert out.state == "unknown"
    assert out.stripe_customer_id == ""
    assert out.recent_charges == []
    assert out.active_subscriptions == []


def test_customer_with_charges_only_is_state_customer(monkeypatch):
    _override_settings(monkeypatch)
    customer = StripeCustomer(
        id="cus_paid",
        email="paid@x.com",
        created=1,
        livemode=True,
    )
    charge = StripeCharge(
        id="ch_1",
        amount=29000,
        currency="usd",
        status="succeeded",
        paid=True,
        created=1700000000,
        description="audit",
        customer="cus_paid",
    )
    _stub_client(monkeypatch, customers=[customer], charges=[charge])
    from backend.finance.service import get_customer_billing_summary

    out = get_customer_billing_summary("paid@x.com")
    assert out.state == "customer"
    assert out.stripe_customer_id == "cus_paid"
    assert out.total_paid_usd == 290.0
    assert len(out.recent_charges) == 1
    assert out.recent_charges[0].amount_usd == 290.0
    assert out.recent_charges[0].created_iso.endswith("Z")
    assert out.mrr_usd == 0.0
    assert out.active_subscriptions == []


def test_customer_with_active_subscription_is_state_subscriber(monkeypatch):
    _override_settings(monkeypatch)
    customer = StripeCustomer(
        id="cus_sub",
        email="sub@x.com",
        created=2,
        livemode=True,
    )
    sub = StripeSubscription(
        id="sub_seo",
        status="active",
        items=[
            StripeSubscriptionItem(
                id="si_1",
                quantity=1,
                price=StripeSubscriptionItemPrice(
                    id="price_300",
                    unit_amount=30000,
                    currency="usd",
                    recurring=StripeRecurring(interval="month", interval_count=1),
                ),
            )
        ],
    )
    _stub_client(monkeypatch, customers=[customer], subscriptions=[sub])
    from backend.finance.service import get_customer_billing_summary

    out = get_customer_billing_summary("sub@x.com")
    assert out.state == "subscriber"
    assert out.mrr_usd == 300.0
    assert len(out.active_subscriptions) == 1
    assert out.active_subscriptions[0].subscription_id == "sub_seo"
    assert out.active_subscriptions[0].price_ids == ["price_300"]


def test_picks_livemode_row_when_multiple_customers_per_email(monkeypatch):
    """Stripe permits duplicate Customer rows per email; we pick livemode+recent."""
    _override_settings(monkeypatch)
    test_row = StripeCustomer(
        id="cus_test",
        email="dupe@x.com",
        created=5000,
        livemode=False,
    )
    live_row = StripeCustomer(
        id="cus_live",
        email="dupe@x.com",
        created=1000,
        livemode=True,
    )
    capture: dict = {}
    _stub_client(
        monkeypatch,
        customers=[test_row, live_row],
        capture=capture,
    )
    from backend.finance.service import get_customer_billing_summary

    out = get_customer_billing_summary("dupe@x.com")
    # Primary = livemode True (even though created earlier).
    assert out.stripe_customer_id == "cus_live"
    assert out.stripe_customer_ids == ["cus_live", "cus_test"]
    # Subscriptions/charges fetched scoped to the primary id.
    assert capture["subs_customer"] == "cus_live"
    assert capture["charges_customer"] == "cus_live"


def test_subscription_fetch_failure_is_partial_not_fatal(monkeypatch):
    _override_settings(monkeypatch)
    customer = StripeCustomer(id="cus_x", email="x@y.com", livemode=True)
    charge = StripeCharge(
        id="ch_y",
        amount=10000,
        currency="usd",
        status="succeeded",
        paid=True,
        created=1700000000,
    )
    from backend.finance.stripe_client import StripeError

    _stub_client(
        monkeypatch,
        customers=[customer],
        charges=[charge],
        subs_raise=StripeError("transient"),
    )
    from backend.finance.service import get_customer_billing_summary

    out = get_customer_billing_summary("x@y.com")
    # Charges came through; sub call failed but didn't crash.
    assert out.state == "customer"
    assert out.total_paid_usd == 100.0
    assert "subscriptions_failed" in out.lookup_error


def test_customer_lookup_transport_error_returns_lookup_failed(monkeypatch):
    _override_settings(monkeypatch)
    from backend.finance.stripe_client import StripeError

    _stub_client(monkeypatch, customers_raise=StripeError("502 bad gateway"))
    from backend.finance.service import get_customer_billing_summary

    out = get_customer_billing_summary("anyone@x.com")
    assert out.state == "lookup_failed"
    # LEAK-FIN-MRR: opaque token only — the raw StripeError ("502 bad gateway")
    # carries internals and must not leak to the caller; it stays in the log.
    assert out.lookup_error == "lookup_failed"
    assert "502" not in out.lookup_error


# ---------------------------------------------------------------------------
# one_line_summary rendering (for inbound-email task descriptions)
# ---------------------------------------------------------------------------


def test_one_line_summary_unknown():
    s = CustomerBillingSummary(email="ghost@x.com", state="unknown", ts="now")
    assert s.one_line_summary() == "no Stripe customer for ghost@x.com"


def test_one_line_summary_lookup_failed_surfaces_error():
    s = CustomerBillingSummary(
        email="x@y.com",
        state="lookup_failed",
        ts="now",
        lookup_error="stripe_api_key_unset",
    )
    assert "stripe_api_key_unset" in s.one_line_summary()


def test_one_line_summary_subscriber_with_charges():
    from backend.finance.models import CustomerChargeRow, CustomerSubscriptionRow

    s = CustomerBillingSummary(
        email="x@y.com",
        state="subscriber",
        ts="now",
        stripe_customer_id="cus_abc",
        mrr_usd=300.0,
        total_paid_usd=920.0,
        active_subscriptions=[
            CustomerSubscriptionRow(
                subscription_id="sub_1",
                status="active",
                mrr_usd=300.0,
            )
        ],
        recent_charges=[
            CustomerChargeRow(
                charge_id="ch_1",
                amount_usd=920.0,
                currency="usd",
                status="succeeded",
                paid=True,
                created_iso="2026-05-01T00:00:00Z",
            )
        ],
    )
    line = s.one_line_summary()
    assert "cus_abc" in line
    assert "$300.00/mo" in line
    assert "$920.00" in line
