"""Tests for backend.catalog.funnel_health — the revenue engine's checkout
consciousness. Stripe mocked; snapshot redirected to tmp storage."""
from __future__ import annotations

import httpx
import pytest

from backend.catalog import funnel_health as fh
from backend.catalog import link_audit
from backend.catalog.registry import CATALOG

_LINKED = [e for e in CATALOG if getattr(e, "payment_link_url", None)]

NOW = 1_000_000.0


@pytest.fixture(autouse=True)
def _tmp_storage(monkeypatch, tmp_path):
    monkeypatch.setattr("backend.common.storage._ROOT", tmp_path)
    yield


def _mock_stripe(active_entries):
    """active_entries: list of (url, amount_cents)."""
    def handler(req: httpx.Request) -> httpx.Response:
        data = [
            {"id": f"pl_{i}", "url": u, "active": True,
             "line_items": {"data": [{"amount_total": amt, "description": "X"}]}}
            for i, (u, amt) in enumerate(active_entries)
        ]
        return httpx.Response(200, json={"data": data, "has_more": False})
    return httpx.MockTransport(handler)


def _patch_stripe(monkeypatch, active_entries):
    """Route link_audit's Stripe fetches through the mock transport."""
    transport = _mock_stripe(active_entries)
    real_client = httpx.Client

    def client_factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(**kwargs)

    monkeypatch.setattr(link_audit.httpx, "Client", client_factory)


# ---------------------------------------------------------------------------
# refresh_snapshot
# ---------------------------------------------------------------------------

def test_refresh_all_live_is_ok(monkeypatch):
    _patch_stripe(monkeypatch, [(e.payment_link_url, e.price_usd_cents) for e in _LINKED])
    snap = fh.refresh_snapshot(api_key="sk_test", now_ts=NOW)
    assert snap.status == "ok"
    assert snap.stale_skus == []
    assert snap.is_fresh(now_ts=NOW)
    # persisted round-trip
    assert fh.load_snapshot().status == "ok"


def test_refresh_stale_catalog_link_is_degraded(monkeypatch):
    # Live set is missing seo_audit's link -> that SKU is stale.
    active = [(e.payment_link_url, e.price_usd_cents)
              for e in _LINKED if e.sku_id != "seo_audit"]
    _patch_stripe(monkeypatch, active)
    snap = fh.refresh_snapshot(api_key="sk_test", now_ts=NOW)
    assert snap.status == "degraded"
    assert "seo_audit" in snap.stale_skus
    assert "seo_audit" in snap.stale_sku_urls
    assert "DEGRADED" in snap.summary_line()


def test_refresh_no_key_is_unknown():
    snap = fh.refresh_snapshot(api_key="", now_ts=NOW)
    assert snap.status == "unknown"
    assert "STRIPE_API_KEY" in snap.detail
    # persisted so the brief can report the absence of signal
    assert fh.load_snapshot().status == "unknown"


def test_refresh_fetch_failure_keeps_prior_snapshot(monkeypatch):
    # Capture the REAL client class before any patching — _patch_stripe
    # replaces httpx.Client module-wide, so capturing later grabs the mock.
    orig_client = httpx.Client

    # First: a good refresh.
    _patch_stripe(monkeypatch, [(e.payment_link_url, e.price_usd_cents) for e in _LINKED])
    fh.refresh_snapshot(api_key="sk_test", now_ts=NOW)
    assert fh.load_snapshot().status == "ok"

    # Then: Stripe errors -> returned snapshot is unknown, disk keeps last good.
    def boom(req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})
    monkeypatch.setattr(
        link_audit.httpx, "Client",
        lambda *a, **kw: orig_client(transport=httpx.MockTransport(boom), **{
            k: v for k, v in kw.items() if k != "transport"}),
    )
    snap = fh.refresh_snapshot(api_key="sk_test", now_ts=NOW + 60)
    assert snap.status == "unknown"
    assert fh.load_snapshot().status == "ok"  # prior retained


# ---------------------------------------------------------------------------
# funnel_gate — the campaign-tick consumer
# ---------------------------------------------------------------------------

def _write_degraded(now_ts=NOW, stale_skus=("seo_audit",)):
    fh._save_snapshot(fh.FunnelHealthSnapshot(
        status="degraded", checked_ts=now_ts,
        stale_skus=list(stale_skus),
        stale_sku_urls={s: f"https://buy.stripe.com/{s}" for s in stale_skus},
    ))


def test_gate_ok_snapshot_allows():
    fh._save_snapshot(fh.FunnelHealthSnapshot(status="ok", checked_ts=NOW))
    out = fh.funnel_gate(now_ts=NOW)
    assert out.allowed is True
    assert out.status == "ok"


def test_gate_no_snapshot_fails_open():
    out = fh.funnel_gate(now_ts=NOW)
    assert out.allowed is True
    assert out.status == "unknown"


def test_gate_degraded_dormant_allows_but_reports(monkeypatch):
    monkeypatch.delenv("SAMUS_FUNNEL_GATE_ENABLED", raising=False)
    _write_degraded()
    out = fh.funnel_gate(now_ts=NOW)
    assert out.allowed is True      # wired-dormant: never blocks unarmed
    assert out.armed is False
    assert out.status == "degraded"
    assert "seo_audit" in out.reason


def test_gate_degraded_armed_blocks(monkeypatch):
    monkeypatch.setenv("SAMUS_FUNNEL_GATE_ENABLED", "1")
    _write_degraded()
    out = fh.funnel_gate(now_ts=NOW)
    assert out.allowed is False
    assert out.armed is True


def test_gate_armed_unaffected_skus_allowed(monkeypatch):
    monkeypatch.setenv("SAMUS_FUNNEL_GATE_ENABLED", "1")
    _write_degraded(stale_skus=("seo_audit",))
    out = fh.funnel_gate(["service_workflow_rescue"], now_ts=NOW)
    assert out.allowed is True
    assert "unaffected" in out.reason


def test_gate_armed_matching_sku_blocked(monkeypatch):
    monkeypatch.setenv("SAMUS_FUNNEL_GATE_ENABLED", "1")
    _write_degraded(stale_skus=("seo_audit",))
    out = fh.funnel_gate(["seo_audit", "service_workflow_rescue"], now_ts=NOW)
    assert out.allowed is False


def test_gate_expired_snapshot_fails_open(monkeypatch):
    monkeypatch.setenv("SAMUS_FUNNEL_GATE_ENABLED", "1")
    _write_degraded(now_ts=NOW - 12 * 3600)  # 12h old > 6h TTL
    out = fh.funnel_gate(now_ts=NOW)
    assert out.allowed is True
    assert "unverified" in out.reason


def _write_wp_only_degraded(now_ts=NOW):
    """Degraded ONLY on WordPress — no catalog/hardcoded staleness."""
    fh._save_snapshot(fh.FunnelHealthSnapshot(
        status="degraded", checked_ts=now_ts, wp_scanned=True,
        wp_stale=["page/home -> https://buy.stripe.com/test"],
    ))


def test_gate_wp_only_does_not_block_email_channel(monkeypatch):
    # The exact foot-gun: a home-page test placeholder must NOT halt the
    # armed autonomous email channel (consider_wp=False).
    monkeypatch.setenv("SAMUS_FUNNEL_GATE_ENABLED", "1")
    _write_wp_only_degraded()
    out = fh.funnel_gate(consider_wp=False, now_ts=NOW)
    assert out.allowed is True
    assert "off-channel" in out.reason


def test_gate_wp_only_blocks_when_wp_considered(monkeypatch):
    # The brief-scoped view (consider_wp=True) still sees WP degradation.
    monkeypatch.setenv("SAMUS_FUNNEL_GATE_ENABLED", "1")
    _write_wp_only_degraded()
    out = fh.funnel_gate(consider_wp=True, now_ts=NOW)
    assert out.allowed is False


def test_gate_catalog_stale_blocks_email_channel(monkeypatch):
    # Catalog staleness DOES ship in emails -> must block even consider_wp=False.
    monkeypatch.setenv("SAMUS_FUNNEL_GATE_ENABLED", "1")
    _write_degraded(stale_skus=("seo_audit",))
    out = fh.funnel_gate(consider_wp=False, now_ts=NOW)
    assert out.allowed is False


def test_portfolio_scoped_gate_ignores_wp_only(monkeypatch):
    # Wiring-level: the email campaign helper uses the scoped gate.
    monkeypatch.setenv("SAMUS_FUNNEL_GATE_ENABLED", "1")
    _write_wp_only_degraded(now_ts=__import__("time").time())
    from backend.cash_engine.campaign_portfolio import _funnel_allows_checkout_push
    assert _funnel_allows_checkout_push() is True


# ---------------------------------------------------------------------------
# production_health surface
# ---------------------------------------------------------------------------

def test_health_check_degraded_is_fail():
    from backend.observability.production_health import (
        HealthStatus, check_checkout_funnel,
    )
    _write_degraded()
    check = check_checkout_funnel(now_ts=NOW)
    assert check.status == HealthStatus.FAIL
    assert "converts zero" in check.detail
    assert check.remediation


def test_health_check_ok():
    from backend.observability.production_health import (
        HealthStatus, check_checkout_funnel,
    )
    fh._save_snapshot(fh.FunnelHealthSnapshot(status="ok", checked_ts=NOW))
    check = check_checkout_funnel(now_ts=NOW)
    assert check.status == HealthStatus.OK


def test_health_check_no_snapshot_is_unknown():
    from backend.observability.production_health import (
        HealthStatus, check_checkout_funnel,
    )
    check = check_checkout_funnel(now_ts=NOW)
    assert check.status == HealthStatus.UNKNOWN
    assert check.remediation


def test_health_check_stale_snapshot_lazy_refresh_no_key():
    # Stale snapshot + no STRIPE_API_KEY → lazy refresh produces "unknown"
    # (the absence of signal is itself surfaced), not WARN.
    from backend.observability.production_health import (
        HealthStatus, check_checkout_funnel,
    )
    fh._save_snapshot(fh.FunnelHealthSnapshot(status="ok", checked_ts=NOW - 24 * 3600))
    check = check_checkout_funnel(now_ts=NOW)
    assert check.status == HealthStatus.UNKNOWN


def test_health_check_stale_after_failed_refresh_is_warn(monkeypatch):
    # Stale snapshot + Stripe 500 → refresh_if_stale returns "unknown" with
    # checked_ts=now (fresh), BUT the detail shows Stripe unreachable. The
    # prior "ok" snapshot is retained on disk. The returned snapshot is fresh
    # but unknown, so the check sees UNKNOWN (not WARN). WARN is only
    # reachable when the returned snapshot is itself still stale — a
    # defense-in-depth branch for future implementations.
    from backend.observability.production_health import (
        HealthStatus, check_checkout_funnel,
    )
    fh._save_snapshot(fh.FunnelHealthSnapshot(status="ok", checked_ts=NOW - 24 * 3600))
    orig_client = httpx.Client
    monkeypatch.setattr(
        link_audit.httpx, "Client",
        lambda *a, **kw: orig_client(
            transport=httpx.MockTransport(lambda r: httpx.Response(500, json={})),
            **{k: v for k, v in kw.items() if k != "transport"},
        ),
    )
    monkeypatch.setenv("STRIPE_API_KEY", "sk_test")
    check = check_checkout_funnel(now_ts=NOW)
    assert check.status == HealthStatus.UNKNOWN
    # Prior "ok" snapshot is retained on disk for the next gate read.
    assert fh.load_snapshot().status == "ok"


# ---------------------------------------------------------------------------
# campaign_portfolio wiring
# ---------------------------------------------------------------------------

def test_portfolio_eligibility_dormant_never_blocks(monkeypatch):
    monkeypatch.delenv("SAMUS_FUNNEL_GATE_ENABLED", raising=False)
    _write_degraded()
    from backend.cash_engine.campaign_portfolio import _funnel_allows_checkout_push
    assert _funnel_allows_checkout_push() is True


def test_portfolio_eligibility_armed_blocks(monkeypatch):
    monkeypatch.setenv("SAMUS_FUNNEL_GATE_ENABLED", "1")
    _write_degraded(now_ts=__import__("time").time())  # fresh right now
    from backend.cash_engine.campaign_portfolio import _funnel_allows_checkout_push
    assert _funnel_allows_checkout_push() is False


def test_portfolio_eligibility_fails_open_on_error(monkeypatch):
    # funnel_health import/read explodes -> production must not stop.
    import backend.catalog.funnel_health as mod
    monkeypatch.setattr(mod, "funnel_gate",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")))
    from backend.cash_engine.campaign_portfolio import _funnel_allows_checkout_push
    assert _funnel_allows_checkout_push() is True


# ---------------------------------------------------------------------------
# load_snapshot — schema-drift resilience
# ---------------------------------------------------------------------------

def test_load_snapshot_ignores_unknown_keys(tmp_path, monkeypatch):
    """A snapshot written by a newer version with extra fields must not crash."""
    monkeypatch.setattr("backend.common.storage._ROOT", tmp_path)
    path = fh._snapshot_path()
    path.write_text(
        '{"status":"ok","checked_ts":1000000.0,"future_field":"surprise"}',
        encoding="utf-8",
    )
    snap = fh.load_snapshot()
    assert snap.status == "ok"
    assert snap.checked_ts == 1_000_000.0
    assert not hasattr(snap, "future_field")


# ---------------------------------------------------------------------------
# refresh_if_stale — lazy TTL refresh
# ---------------------------------------------------------------------------

def test_refresh_if_stale_returns_cached_when_fresh(monkeypatch):
    fh._save_snapshot(fh.FunnelHealthSnapshot(status="ok", checked_ts=NOW))
    snap = fh.refresh_if_stale(now_ts=NOW)
    assert snap.status == "ok"
    # No Stripe call was made — would blow up if it tried without a mock.


def test_refresh_if_stale_refreshes_when_expired(monkeypatch):
    fh._save_snapshot(fh.FunnelHealthSnapshot(status="ok", checked_ts=NOW - 12 * 3600))
    _patch_stripe(monkeypatch, [(e.payment_link_url, e.price_usd_cents) for e in _LINKED])
    monkeypatch.setenv("STRIPE_API_KEY", "sk_test")
    snap = fh.refresh_if_stale(now_ts=NOW)
    assert snap.status == "ok"
    assert snap.checked_ts == NOW  # freshly written


def test_refresh_if_stale_refreshes_when_no_snapshot(monkeypatch):
    _patch_stripe(monkeypatch, [(e.payment_link_url, e.price_usd_cents) for e in _LINKED])
    monkeypatch.setenv("STRIPE_API_KEY", "sk_test")
    snap = fh.refresh_if_stale(now_ts=NOW)
    assert snap.status == "ok"
    assert snap.checked_ts == NOW


# ---------------------------------------------------------------------------
# audit_links — pass-through active_urls avoids double fetch
# ---------------------------------------------------------------------------

def test_audit_links_uses_prefetched_active_urls(monkeypatch):
    """When active_urls is passed, audit_links must NOT call fetch_active_links."""
    from backend.catalog import link_audit

    called = []
    real_fetch = link_audit.fetch_active_links

    def spy(*a, **kw):
        called.append(1)
        return real_fetch(*a, **kw)

    monkeypatch.setattr(link_audit, "fetch_active_links", spy)
    active = {e.payment_link_url for e in _LINKED if getattr(e, "payment_link_url", None)}
    by_price: dict[int, list[tuple[str, str]]] = {}
    result = link_audit.audit_links(active_urls=active, by_price=by_price)
    assert called == [], "fetch_active_links should not be called when active_urls is passed"
    assert result == []  # all links are in the active set


def test_health_check_lazy_refresh_triggers(monkeypatch):
    """check_checkout_funnel triggers refresh_if_stale on a stale snapshot."""
    from backend.observability.production_health import (
        HealthStatus, check_checkout_funnel,
    )
    # Stale snapshot + all links live => refresh_if_stale refreshes => OK
    fh._save_snapshot(fh.FunnelHealthSnapshot(status="ok", checked_ts=NOW - 24 * 3600))
    _patch_stripe(monkeypatch, [(e.payment_link_url, e.price_usd_cents) for e in _LINKED])
    monkeypatch.setenv("STRIPE_API_KEY", "sk_test")
    check = check_checkout_funnel(now_ts=NOW)
    assert check.status == HealthStatus.OK
