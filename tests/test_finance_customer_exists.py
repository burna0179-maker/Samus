"""POST /customer-exists — FIN-08 finance-side validation route.

The route delegates to ``confirm_customer_exists`` which builds a StripeClient
from settings. We stub StripeClient inside backend.finance.service so the route
exercises its real fail-closed routing without any live Stripe call.
"""
from __future__ import annotations


def _override_settings(monkeypatch, *, stripe_api_key: str = "rk_test_unit"):
    class _S:
        pass
    s = _S()
    s.stripe_api_key = stripe_api_key
    import backend.finance.service as svc_mod
    monkeypatch.setattr(svc_mod, "get_settings", lambda: s)


def _stub_client(monkeypatch, *, customer=None, raise_exc=None, capture=None):
    """Replace StripeClient inside backend.finance.service with a fake."""

    class _FakeClient:
        def __init__(self, api_key, **_kw):
            if capture is not None:
                capture["api_key"] = api_key

        def retrieve_customer(self, customer_id):
            if capture is not None:
                capture["customer_id"] = customer_id
            if raise_exc is not None:
                raise raise_exc
            return customer

    import backend.finance.service as svc_mod
    monkeypatch.setattr(svc_mod, "StripeClient", _FakeClient)


def _client():
    from fastapi.testclient import TestClient
    from backend.finance.app import app
    return TestClient(app)


# ---------------------------------------------------------------------------
# POST /customer-exists
# ---------------------------------------------------------------------------

def test_customer_exists_true_when_stripe_confirms(monkeypatch):
    from backend.finance.models import StripeCustomer
    _override_settings(monkeypatch)
    capture: dict = {}
    _stub_client(
        monkeypatch,
        customer=StripeCustomer(id="cus_live", livemode=True),
        capture=capture,
    )
    r = _client().post("/customer-exists", json={"stripe_customer_id": "cus_live"})
    assert r.status_code == 200, r.text
    assert r.json() == {"exists": True}
    assert capture["customer_id"] == "cus_live"


def test_customer_exists_false_when_stripe_returns_none(monkeypatch):
    _override_settings(monkeypatch)
    _stub_client(monkeypatch, customer=None)  # 404 / no such customer
    r = _client().post("/customer-exists", json={"stripe_customer_id": "cus_nope"})
    assert r.status_code == 200, r.text
    assert r.json() == {"exists": False}


def test_customer_exists_false_when_stripe_raises(monkeypatch):
    from backend.finance.stripe_client import StripeError
    _override_settings(monkeypatch)
    _stub_client(monkeypatch, raise_exc=StripeError("stripe_http_401: bad key"))
    r = _client().post("/customer-exists", json={"stripe_customer_id": "cus_x"})
    assert r.status_code == 200, r.text
    # Fail-closed: a Stripe error is NOT confirmation.
    assert r.json() == {"exists": False}


def test_customer_exists_false_when_api_key_unset(monkeypatch):
    _override_settings(monkeypatch, stripe_api_key="")
    # No StripeClient stub needed — _new_stripe_client returns None.
    r = _client().post("/customer-exists", json={"stripe_customer_id": "cus_x"})
    assert r.status_code == 200, r.text
    assert r.json() == {"exists": False}


def test_customer_exists_false_for_blank_id(monkeypatch):
    _override_settings(monkeypatch)
    capture: dict = {}
    _stub_client(monkeypatch, customer=None, capture=capture)
    r = _client().post("/customer-exists", json={"stripe_customer_id": "   "})
    assert r.status_code == 200, r.text
    assert r.json() == {"exists": False}
    # Blank id short-circuits before any Stripe call.
    assert "customer_id" not in capture


def test_customer_exists_rejects_non_object_body(monkeypatch):
    _override_settings(monkeypatch)
    _stub_client(monkeypatch, customer=None)
    r = _client().post("/customer-exists", json=["not", "an", "object"])
    assert r.status_code == 400
    assert "expected_json_object" in r.text


# ---------------------------------------------------------------------------
# Guard wiring — same check_capability("finance", ...) gate as siblings.
# HMAC middleware is disabled process-wide in conftest; the per-route guard is
# the capability check. Proving the route is gated by it: drop the capability
# from the finance set and the route must 403, exactly like every sibling.
# ---------------------------------------------------------------------------

def test_customer_exists_enforces_capability_guard(monkeypatch):
    _override_settings(monkeypatch)
    _stub_client(monkeypatch, customer=None)
    import backend.common.capabilities as caps
    reduced = set(caps.SERVICE_CAPABILITIES["finance"]) - {"customer_exists"}
    patched = dict(caps.SERVICE_CAPABILITIES)
    patched["finance"] = reduced
    monkeypatch.setattr(caps, "SERVICE_CAPABILITIES", patched)
    r = _client().post("/customer-exists", json={"stripe_customer_id": "cus_live"})
    assert r.status_code == 403
    assert "capability denied" in r.text


def test_customer_exists_capability_registered():
    """Sanity: the live finance set exposes the capability the route guards on."""
    from backend.common.capabilities import SERVICE_CAPABILITIES
    assert "customer_exists" in SERVICE_CAPABILITIES["finance"]


# ---------------------------------------------------------------------------
# /work action parity
# ---------------------------------------------------------------------------

def test_work_endpoint_customer_exists_action(monkeypatch):
    from backend.finance.models import StripeCustomer
    _override_settings(monkeypatch)
    _stub_client(monkeypatch, customer=StripeCustomer(id="cus_live", livemode=True))
    r = _client().post("/work", json={
        "task_id": "t-ce",
        "payload": {"stripe_customer_id": "cus_live"},
        "metadata": {"action": "customer_exists"},
    })
    assert r.status_code == 200, r.text
    assert r.json() == {"exists": True}
