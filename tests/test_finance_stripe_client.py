"""StripeClient — httpx mocked at the module level."""

from __future__ import annotations

import json

import httpx
import pytest


class _FakeHttpx:
    """Per-module httpx stub. Falls through to real httpx for exception classes."""

    def __init__(self, client_cls):
        self.Client = client_cls

    def __getattr__(self, name):
        return getattr(httpx, name)


def _build_client(
    monkeypatch, *, status: int = 200, body: dict | None = None, raise_exc: Exception | None = None
):
    """Patch backend.finance.stripe_client.httpx with a controllable fake."""

    class _Resp:
        def __init__(self):
            self.status_code = status
            self._body = body or {}
            self.text = json.dumps(self._body)

        def json(self):
            return self._body

    class _Client:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, headers=None, params=None):
            if raise_exc is not None:
                raise raise_exc
            return _Resp()

    import backend.finance.stripe_client as mod

    monkeypatch.setattr(mod, "httpx", _FakeHttpx(_Client))
    return mod.StripeClient(api_key="rk_test_unit")


def test_client_rejects_empty_key():
    from backend.finance.stripe_client import StripeClient

    with pytest.raises(ValueError):
        StripeClient(api_key="")


def test_fetch_balance_parses_typed():
    """Smoke: shape mirrors a real /v1/balance response."""
    import contextlib

    body = {
        "object": "balance",
        "available": [{"amount": 26953, "currency": "usd"}],
        "pending": [{"amount": 100, "currency": "usd"}],
        "livemode": True,
    }
    with contextlib.ExitStack() as stack:
        monkeypatch = pytest.MonkeyPatch()
        stack.callback(monkeypatch.undo)
        client = _build_client(monkeypatch, body=body)
        bal = client.fetch_balance()
    assert bal.available_usd_dollars() == 269.53
    assert bal.pending_usd_dollars() == 1.00
    assert bal.livemode is True


def test_fetch_charges_returns_typed_list(monkeypatch):
    body = {
        "object": "list",
        "data": [
            {
                "id": "ch_1",
                "amount": 50000,
                "currency": "usd",
                "status": "succeeded",
                "paid": True,
                "created": 1700000000,
                "description": "test",
                "customer": "cus_x",
            },
            {
                "id": "ch_2",
                "amount": 25000,
                "currency": "usd",
                "status": "succeeded",
                "paid": True,
                "created": 1700001000,
                "description": None,
                "customer": None,
            },
        ],
    }
    client = _build_client(monkeypatch, body=body)
    charges = client.fetch_charges(limit=5)
    assert len(charges) == 2
    assert charges[0].id == "ch_1"
    assert charges[0].amount == 50000
    assert charges[1].customer is None


def test_fetch_charges_skips_malformed_row(monkeypatch):
    body = {
        "data": [
            {
                "id": "ch_good",
                "amount": 100,
                "currency": "usd",
                "status": "succeeded",
                "paid": True,
                "created": 1,
                "description": "x",
            },
            {"garbage": True},  # missing required fields
        ],
    }
    client = _build_client(monkeypatch, body=body)
    rows = client.fetch_charges()
    assert len(rows) == 1
    assert rows[0].id == "ch_good"


def test_fetch_payouts_clamps_limit_to_100(monkeypatch):
    """Limit > 100 must clamp; the fake only records that params is bounded."""
    seen: dict = {}

    class _Resp:
        status_code = 200
        text = "{}"

        def json(self):
            return {"data": []}

    class _Client:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, headers=None, params=None):
            seen["params"] = params or {}
            return _Resp()

    import backend.finance.stripe_client as mod

    monkeypatch.setattr(mod, "httpx", _FakeHttpx(_Client))
    client = mod.StripeClient(api_key="rk_test_unit")
    client.fetch_payouts(limit=500)
    assert seen["params"]["limit"] == 100


def test_http_error_raises_stripe_error(monkeypatch):
    from backend.finance.stripe_client import StripeError

    body = {"error": {"type": "invalid_request_error", "message": "Invalid API Key provided"}}
    client = _build_client(monkeypatch, status=401, body=body)
    with pytest.raises(StripeError) as ei:
        client.fetch_balance()
    assert "stripe_http_401" in str(ei.value)
    assert "Invalid API Key" in str(ei.value)


def test_transport_error_raises_stripe_error(monkeypatch):
    from backend.finance.stripe_client import StripeError

    client = _build_client(monkeypatch, raise_exc=httpx.ConnectError("down"))
    with pytest.raises(StripeError) as ei:
        client.fetch_balance()
    assert "stripe_transport_error" in str(ei.value)


def test_path_must_start_with_slash():
    from backend.finance.stripe_client import StripeClient

    c = StripeClient(api_key="rk_test_unit")
    with pytest.raises(ValueError):
        c._get("balance")  # missing leading slash


# ---------------------------------------------------------------------------
# retrieve_customer — FIN-08 existence check
# ---------------------------------------------------------------------------


def test_retrieve_customer_returns_typed_on_200(monkeypatch):
    body = {
        "id": "cus_live",
        "object": "customer",
        "email": "x@y.com",
        "livemode": True,
        "created": 1700000000,
    }
    client = _build_client(monkeypatch, body=body)
    cust = client.retrieve_customer("cus_live")
    assert cust is not None
    assert cust.id == "cus_live"
    assert cust.livemode is True


def test_retrieve_customer_returns_none_on_404(monkeypatch):
    body = {"error": {"type": "invalid_request_error", "message": "No such customer: 'cus_nope'"}}
    client = _build_client(monkeypatch, status=404, body=body)
    assert client.retrieve_customer("cus_nope") is None


def test_retrieve_customer_returns_none_for_blank_id(monkeypatch):
    client = _build_client(monkeypatch, body={"id": "cus_x"})
    assert client.retrieve_customer("") is None
    assert client.retrieve_customer("   ") is None


def test_retrieve_customer_returns_none_for_deleted_record(monkeypatch):
    body = {"id": "cus_gone", "object": "customer", "deleted": True}
    client = _build_client(monkeypatch, body=body)
    assert client.retrieve_customer("cus_gone") is None


def test_retrieve_customer_raises_on_non_404_error(monkeypatch):
    """Auth/rate-limit/5xx must PROPAGATE so the caller fails closed."""
    from backend.finance.stripe_client import StripeError

    body = {"error": {"type": "invalid_request_error", "message": "Invalid API Key provided"}}
    client = _build_client(monkeypatch, status=401, body=body)
    with pytest.raises(StripeError) as ei:
        client.retrieve_customer("cus_live")
    assert "stripe_http_401" in str(ei.value)


def test_retrieve_customer_raises_on_transport_error(monkeypatch):
    from backend.finance.stripe_client import StripeError

    client = _build_client(monkeypatch, raise_exc=httpx.ConnectError("down"))
    with pytest.raises(StripeError):
        client.retrieve_customer("cus_live")
