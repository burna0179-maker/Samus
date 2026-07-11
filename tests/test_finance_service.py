"""Finance service — orchestration tests with Stripe mocked at module level."""

from __future__ import annotations


def _reset_idempotency(monkeypatch):
    from backend.common.idempotency import IdempotencyStore
    import backend.common.idempotency as idem_mod

    fresh = IdempotencyStore()
    monkeypatch.setattr(idem_mod, "GLOBAL_IDEMPOTENCY_STORE", fresh)
    import backend.finance.service as svc_mod

    monkeypatch.setattr(svc_mod, "GLOBAL_IDEMPOTENCY_STORE", fresh)


def _override_settings(monkeypatch, *, stripe_api_key: str = "rk_test"):
    class _S:
        pass

    settings = _S()
    settings.stripe_api_key = stripe_api_key
    import backend.finance.service as svc_mod

    monkeypatch.setattr(svc_mod, "get_settings", lambda: settings)


def _stub_stripe_client(
    monkeypatch,
    *,
    balance=None,
    charges=None,
    payouts=None,
    subscriptions=None,
    raise_exc: Exception | None = None,
):
    """Replace StripeClient in service module with a fake."""
    from backend.finance.models import StripeBalance, StripeBalanceLine

    bal = balance or StripeBalance(
        available=[StripeBalanceLine(amount=30000, currency="usd")],
        pending=[],
        livemode=True,
    )

    class _FakeClient:
        def __init__(self, api_key):
            self.api_key = api_key

        def fetch_balance(self):
            if raise_exc:
                raise raise_exc
            return bal

        def fetch_charges(self, limit=10):
            if raise_exc:
                raise raise_exc
            return charges or []

        def fetch_payouts(self, limit=10):
            if raise_exc:
                raise raise_exc
            return payouts or []

        def fetch_subscriptions(self, *, status="active", limit=100, customer=None):
            if raise_exc:
                raise raise_exc
            return subscriptions or []

    import backend.finance.service as svc_mod

    monkeypatch.setattr(svc_mod, "StripeClient", _FakeClient)


def _seed_codb(monkeypatch, tmp_path, *, monthly_total: float = 150.0):
    """Write a tiny CODB registry and point the loader at it."""
    yaml_path = tmp_path / "codb.yaml"
    yaml_path.write_text(
        f"costs:\n"
        f"  - id: test-aws\n"
        f"    name: Test AWS\n"
        f"    category: infrastructure\n"
        f"    criticality: critical\n"
        f"    estimated_monthly_usd: {monthly_total}\n"
        f"    notes: ''\n"
        f"revenue_targets:\n"
        f"  monthly_minimum_usd: 500\n"
        f"  runway_alert_days: 60\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SAMUS_CODB_REGISTRY_PATH", str(yaml_path))


# ---------------------------------------------------------------------------
# get_snapshot
# ---------------------------------------------------------------------------


def test_snapshot_happy_path(tmp_path, monkeypatch):
    _reset_idempotency(monkeypatch)
    _override_settings(monkeypatch, stripe_api_key="rk_test")
    monkeypatch.setenv("SAMUS_FINANCE_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    _seed_codb(monkeypatch, tmp_path, monthly_total=150.0)

    from backend.finance.models import (
        SnapshotRequest,
        StripeBalance,
        StripeBalanceLine,
        StripeCharge,
    )

    _stub_stripe_client(
        monkeypatch,
        balance=StripeBalance(
            available=[StripeBalanceLine(amount=30000, currency="usd")],
            pending=[StripeBalanceLine(amount=500, currency="usd")],
            livemode=True,
        ),
        charges=[
            StripeCharge(
                id="ch_1",
                amount=50000,
                currency="usd",
                status="succeeded",
                paid=True,
                created=1,
                description="x",
            ),
            StripeCharge(
                id="ch_2",
                amount=25000,
                currency="usd",
                status="succeeded",
                paid=True,
                created=2,
                description=None,
            ),
        ],
    )

    from backend.finance.service import get_snapshot

    snap = get_snapshot(SnapshotRequest())
    assert snap.stripe_reachable is True
    assert snap.stripe_error is None
    assert snap.balance is not None
    assert snap.balance.available_usd_dollars() == 300.0
    assert snap.recent_charges_count == 2
    assert snap.recent_charges_usd_total == 750.0
    assert snap.codb_summary.total_monthly_burn_usd == 150.0
    # daily burn = 5; runway = 300/5 = 60 days; alert at exactly threshold = False
    assert snap.runway.days_of_runway == 60.0
    assert snap.runway.alert_triggered is False


def test_snapshot_surfaces_live_mrr_from_active_subscriptions(tmp_path, monkeypatch):
    """A live active subscription must show up as mrr_usd in the snapshot even
    when it never came through the webhook pipeline (no charges/payouts)."""
    _reset_idempotency(monkeypatch)
    _override_settings(monkeypatch, stripe_api_key="rk_test")
    monkeypatch.setenv("SAMUS_FINANCE_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    _seed_codb(monkeypatch, tmp_path, monthly_total=150.0)

    from backend.finance.models import (
        SnapshotRequest,
        StripeSubscription,
        StripeSubscriptionItem,
        StripeSubscriptionItemPrice,
        StripeRecurring,
    )

    # A single $300/mo active subscription, no charges/payouts in the window.
    sub = StripeSubscription(
        id="sub_live_1",
        status="active",
        items=[
            StripeSubscriptionItem(
                id="si_1",
                quantity=1,
                price=StripeSubscriptionItemPrice(
                    id="price_1",
                    unit_amount=30000,
                    currency="usd",
                    recurring=StripeRecurring(interval="month", interval_count=1),
                ),
            )
        ],
    )
    _stub_stripe_client(monkeypatch, charges=[], payouts=[], subscriptions=[sub])

    from backend.finance.service import get_snapshot

    snap = get_snapshot(SnapshotRequest())
    assert snap.stripe_reachable is True
    assert snap.active_subscriptions_count == 1
    assert snap.mrr_usd == 300.0
    # Revenue is independent of the (empty) recent-charges window.
    assert snap.recent_charges_count == 0


def test_snapshot_missing_stripe_key_degrades_gracefully(tmp_path, monkeypatch):
    _reset_idempotency(monkeypatch)
    _override_settings(monkeypatch, stripe_api_key="")  # no key
    monkeypatch.setenv("SAMUS_FINANCE_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    _seed_codb(monkeypatch, tmp_path, monthly_total=150.0)

    from backend.finance.models import SnapshotRequest
    from backend.finance.service import get_snapshot

    snap = get_snapshot(SnapshotRequest())
    assert snap.stripe_reachable is False
    assert snap.stripe_error == "stripe_api_key_unset"
    assert snap.balance is None
    # Runway with balance=0 should be 0 days and alert triggered.
    assert snap.runway.available_balance_usd == 0.0
    assert snap.runway.days_of_runway == 0.0
    assert snap.runway.alert_triggered is True


def test_snapshot_stripe_error_is_opaque_and_does_not_leak(tmp_path, monkeypatch):
    _reset_idempotency(monkeypatch)
    _override_settings(monkeypatch, stripe_api_key="rk_test")
    monkeypatch.setenv("SAMUS_FINANCE_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    _seed_codb(monkeypatch, tmp_path, monthly_total=60.0)

    from backend.finance.stripe_client import StripeError

    _stub_stripe_client(monkeypatch, raise_exc=StripeError("stripe_http_401: bad key"))

    from backend.finance.models import SnapshotRequest
    from backend.finance.service import get_snapshot

    snap = get_snapshot(SnapshotRequest())
    assert snap.stripe_reachable is False
    # LEAK-FIN-MRR: the raw StripeError text can carry account/request internals
    # and the snapshot is serializable to a response, so the caller gets only an
    # opaque token; the detail stays server-side (logged).
    assert snap.stripe_error == "stripe_error"
    assert "stripe_http_401" not in (snap.stripe_error or "")
    assert "bad key" not in (snap.stripe_error or "")
    assert snap.balance is None


def test_snapshot_idempotent_cache_hit(tmp_path, monkeypatch):
    _reset_idempotency(monkeypatch)
    _override_settings(monkeypatch, stripe_api_key="rk_test")
    monkeypatch.setenv("SAMUS_FINANCE_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    _seed_codb(monkeypatch, tmp_path, monthly_total=30.0)

    call_count = {"n": 0}

    class _FakeClient:
        def __init__(self, api_key):
            pass

        def fetch_balance(self):
            call_count["n"] += 1
            from backend.finance.models import StripeBalance, StripeBalanceLine

            return StripeBalance(
                available=[StripeBalanceLine(amount=10000, currency="usd")],
                pending=[],
                livemode=False,
            )

        def fetch_charges(self, limit=10):
            return []

        def fetch_payouts(self, limit=10):
            return []

        def fetch_subscriptions(self, *, status="active", limit=100, customer=None):
            return []

    import backend.finance.service as svc_mod

    monkeypatch.setattr(svc_mod, "StripeClient", _FakeClient)

    from backend.finance.models import SnapshotRequest

    a = svc_mod.get_snapshot(SnapshotRequest())
    b = svc_mod.get_snapshot(SnapshotRequest())
    # Cache hit means Stripe was only hit once.
    assert call_count["n"] == 1
    assert a.model_dump() == b.model_dump()


# ---------------------------------------------------------------------------
# get_runway
# ---------------------------------------------------------------------------


def test_runway_with_override_skips_stripe(tmp_path, monkeypatch):
    _reset_idempotency(monkeypatch)
    _override_settings(monkeypatch, stripe_api_key="rk_test")
    _seed_codb(monkeypatch, tmp_path, monthly_total=60.0)

    class _FailClient:
        def __init__(self, api_key):
            raise AssertionError("StripeClient must not be built when override given")

    import backend.finance.service as svc_mod

    monkeypatch.setattr(svc_mod, "StripeClient", _FailClient)

    from backend.finance.service import get_runway

    r = get_runway(override_balance_usd=120.0)
    # daily burn = 2, runway = 60
    assert r.daily_burn_usd == 2.0
    assert r.days_of_runway == 60.0


def test_runway_falls_back_to_zero_when_stripe_fails(tmp_path, monkeypatch):
    _reset_idempotency(monkeypatch)
    _override_settings(monkeypatch, stripe_api_key="rk_test")
    _seed_codb(monkeypatch, tmp_path, monthly_total=30.0)
    from backend.finance.stripe_client import StripeError

    _stub_stripe_client(monkeypatch, raise_exc=StripeError("transport"))

    from backend.finance.service import get_runway

    r = get_runway()
    assert r.available_balance_usd == 0.0
    assert r.alert_triggered is True


# ---------------------------------------------------------------------------
# get_codb_summary
# ---------------------------------------------------------------------------


def test_codb_summary_uses_default_registry(monkeypatch):
    # Make sure no test before this left an override.
    monkeypatch.delenv("SAMUS_CODB_REGISTRY_PATH", raising=False)
    from backend.finance.service import get_codb_summary

    s = get_codb_summary()
    assert s.total_monthly_burn_usd > 0
    assert len(s.by_category) >= 3  # multiple categories in seed
    assert s.cuttable_low_first  # at least one item
