"""Billing / payment-link rollup tests."""
from __future__ import annotations


def _stub_stripe(monkeypatch, *, links: list | None = None,
                 raise_exc: Exception | None = None):
    """Replace StripeClient in the billing module with a fake."""
    class _FakeClient:
        def __init__(self, api_key):
            self.api_key = api_key
        def fetch_payment_links(self, *, active=True, limit=100):
            if raise_exc:
                raise raise_exc
            return links or []
    import backend.finance.billing as billing_mod
    monkeypatch.setattr(billing_mod, "StripeClient", _FakeClient)


def _override_settings(monkeypatch, *, key: str = "rk_test"):
    class _S: pass
    s = _S(); s.stripe_api_key = key
    import backend.finance.billing as billing_mod
    monkeypatch.setattr(billing_mod, "get_settings", lambda: s)


def test_summarize_extracts_offer_code():
    from backend.finance.billing import summarize_links
    from backend.finance.models import StripePaymentLink
    links = [
        StripePaymentLink(id="plink_1", url="https://x/1", active=True,
                          livemode=True, currency="usd", is_subscription=False,
                          metadata={"hf_offer_code": "seo_audit",
                                    "samus_managed": "true"}),
    ]
    out = summarize_links(links)
    assert out[0].offer_code == "seo_audit"
    assert out[0].samus_managed is True


def test_summarize_sorts_livemode_first_then_alpha():
    from backend.finance.billing import summarize_links
    from backend.finance.models import StripePaymentLink

    def _link(id_, code, livemode, samus_managed=True):
        return StripePaymentLink(
            id=id_, url=f"https://x/{id_}", active=True, livemode=livemode,
            currency="usd", is_subscription=False,
            metadata={"hf_offer_code": code, "samus_managed": str(samus_managed).lower()},
        )
    # 3 livemode (b, a, c) + 1 test (z). Expect livemode-first, alpha within.
    out = summarize_links([
        _link("p1", "b_zeta", True),
        _link("p2", "z_test", False),
        _link("p3", "a_alpha", True),
        _link("p4", "c_gamma", True),
    ])
    codes = [s.offer_code for s in out]
    assert codes == ["a_alpha", "b_zeta", "c_gamma", "z_test"]


def test_summarize_offercodeless_sorts_last():
    from backend.finance.billing import summarize_links
    from backend.finance.models import StripePaymentLink
    out = summarize_links([
        StripePaymentLink(id="plink_a", url="https://x/a", livemode=True,
                          metadata={"hf_offer_code": "named_one",
                                    "samus_managed": "true"}),
        StripePaymentLink(id="plink_b", url="https://x/b", livemode=True,
                          metadata={"samus_managed": "true"}),
    ])
    assert out[0].offer_code == "named_one"
    assert out[1].offer_code == ""  # the offer-code-less one sorted last


def test_fetch_rollup_no_key_degrades(monkeypatch):
    _override_settings(monkeypatch, key="")
    from backend.finance.billing import fetch_rollup
    r = fetch_rollup("2026-05-15T00:00:00Z")
    assert r.stripe_reachable is False
    assert r.stripe_error == "stripe_api_key_unset"
    assert r.count_total == 0


def test_fetch_rollup_stripe_error_degrades(monkeypatch):
    _override_settings(monkeypatch)
    from backend.finance.stripe_client import StripeError
    _stub_stripe(monkeypatch, raise_exc=StripeError("stripe_http_403: forbidden"))
    from backend.finance.billing import fetch_rollup
    r = fetch_rollup("t")
    assert r.stripe_reachable is False
    assert "403" in r.stripe_error


def test_fetch_rollup_counts(monkeypatch):
    _override_settings(monkeypatch)
    from backend.finance.models import StripePaymentLink
    _stub_stripe(monkeypatch, links=[
        StripePaymentLink(id="p1", url="https://x/1", active=True,
                          livemode=True, is_subscription=False,
                          metadata={"hf_offer_code": "a"}),
        StripePaymentLink(id="p2", url="https://x/2", active=True,
                          livemode=True, is_subscription=True,
                          metadata={"hf_offer_code": "b"}),
        StripePaymentLink(id="p3", url="https://x/3", active=True,
                          livemode=False, is_subscription=False,
                          metadata={"hf_offer_code": "c"}),
    ])
    from backend.finance.billing import fetch_rollup
    r = fetch_rollup("t")
    assert r.stripe_reachable is True
    assert r.count_total == 3
    assert r.count_subscription == 1
    assert r.count_one_time == 2
    assert r.livemode_count == 2


def test_payment_link_envelope_flattens_subscription_data():
    from backend.finance.models import StripePaymentLink
    raw = {
        "id": "plink_x", "url": "https://x/y", "active": True,
        "livemode": True, "currency": "usd",
        "subscription_data": {"description": None},  # presence => is_subscription
        "metadata": {"hf_offer_code": "ai_ops_partner"},
    }
    link = StripePaymentLink.model_validate_envelope(raw)
    assert link.is_subscription is True
    assert link.metadata["hf_offer_code"] == "ai_ops_partner"


def test_payment_link_envelope_no_subscription_data():
    from backend.finance.models import StripePaymentLink
    raw = {
        "id": "plink_y", "url": "https://x/z", "active": True,
        "livemode": True, "currency": "usd",
        "subscription_data": None,
        "metadata": {"hf_offer_code": "seo_audit"},
    }
    link = StripePaymentLink.model_validate_envelope(raw)
    assert link.is_subscription is False


def test_app_endpoint_returns_rollup_when_no_key(monkeypatch):
    """End-to-end through the FastAPI app — degrade path."""
    import backend.finance.billing as billing_mod
    class _S: pass
    s = _S(); s.stripe_api_key = ""
    monkeypatch.setattr(billing_mod, "get_settings", lambda: s)
    from fastapi.testclient import TestClient
    from backend.finance.app import app
    client = TestClient(app)
    r = client.get("/payment_links")
    assert r.status_code == 200
    body = r.json()
    assert body["stripe_reachable"] is False
    assert body["stripe_error"] == "stripe_api_key_unset"
    assert body["links"] == []
