"""Tests for the signal_filter pre-qualification gate wired into prospecting.

Covers the ProspectRecord→enrichment-dict adapter, the admit/reject decision,
the best-effort skip path when signal_filter is unavailable, the per-prospect
fail-open path, and the Step 2c integration inside process_discovery.
"""
from __future__ import annotations

from backend.prospecting.models import DiscoveryRequest, ProspectRecord
from backend.prospecting.signal_gate import (
    admit_prospect,
    apply_signal_filter_gate,
)


def _strong_prospect(**overrides) -> ProspectRecord:
    """A high-signal prospect that should clear the admission threshold."""
    base = dict(
        prospect_id="pr_strong",
        company_name="Strong Co",
        phone="(530) 555-1000",
        website_url="https://strongco.example",
        website_status="live",
        seo_score=85,
        owner_email="owner@strongco.example",
        social_facebook="https://facebook.com/strongco",
        review_rating="4.8",
        review_count="120",
        industry="hvac",
    )
    base.update(overrides)
    return ProspectRecord(**base)


def _weak_prospect(**overrides) -> ProspectRecord:
    """A low-signal prospect that should fail the admission threshold.

    Has a (parked) website on purpose: a no-website prospect bypasses the
    gate entirely (absence of a site is the pitch, not a disqualifier), so
    the canonical "weak" record must be weak-with-a-junk-site.
    """
    base = dict(
        prospect_id="pr_weak",
        company_name="Weak Co",
        phone="",
        website_url="https://weakco.example",
        website_status="parked",
        seo_score=0,
        review_rating="",
        review_count="0",
        industry="hvac",
    )
    base.update(overrides)
    return ProspectRecord(**base)


# ── adapter / decision ──────────────────────────────────────────────────────


def test_admit_prospect_admits_strong_record():
    assert admit_prospect(_strong_prospect()) is True


def test_admit_prospect_rejects_weak_record():
    assert admit_prospect(_weak_prospect()) is False


def test_apply_gate_drops_only_weak_prospects():
    prospects = [_strong_prospect(), _weak_prospect()]
    admitted, rejected = apply_signal_filter_gate(prospects)
    assert rejected == 1
    assert [p.prospect_id for p in admitted] == ["pr_strong"]


def test_apply_gate_bypasses_no_website_prospects():
    """A no-website prospect is admitted unconditionally — the gate's
    presence-weighted axes cap it at 0.60 < threshold 0.62, but absence of a
    website IS the web-design signal the morning call list wants surfaced."""
    no_site = _weak_prospect(
        prospect_id="pr_nosite", website_url="", website_status="no_website",
    )
    admitted, rejected = apply_signal_filter_gate([no_site, _weak_prospect()])
    assert rejected == 1
    assert [p.prospect_id for p in admitted] == ["pr_nosite"]


def test_apply_gate_empty_list_is_noop():
    admitted, rejected = apply_signal_filter_gate([])
    assert admitted == []
    assert rejected == 0


# ── best-effort: signal_filter unavailable ──────────────────────────────────


def test_apply_gate_skips_when_signal_filter_import_fails(monkeypatch):
    """A missing signal_filter module leaves the prospect list untouched."""
    import builtins

    real_import = builtins.__import__

    def _blocked_import(name, *args, **kwargs):
        if name.startswith("backend.signal_filter"):
            raise ImportError("signal_filter unavailable in this build")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _blocked_import)

    prospects = [_strong_prospect(), _weak_prospect()]
    admitted, rejected = apply_signal_filter_gate(prospects)
    # Fail-open: nothing dropped, original list returned.
    assert admitted == prospects
    assert rejected == 0


# ── per-prospect fail-open ──────────────────────────────────────────────────


def test_apply_gate_keeps_prospect_when_scoring_raises(monkeypatch):
    """A per-prospect scoring fault keeps that prospect (fail-open)."""
    import backend.prospecting.signal_gate as gate_mod

    def _boom(_p):
        raise RuntimeError("scoring blew up")

    monkeypatch.setattr(gate_mod, "admit_prospect", _boom)

    prospects = [_strong_prospect(), _weak_prospect()]
    admitted, rejected = apply_signal_filter_gate(prospects)
    assert admitted == prospects
    assert rejected == 0


# ── process_discovery Step 2c integration ───────────────────────────────────


def _fake_discover_factory(prospects_per_zip):
    def _fake(*, zipcode, industries, max_results_per_zip, must_have_website):
        return list(prospects_per_zip.get(zipcode, []))
    return _fake


def _isolate_idempotency(monkeypatch):
    from backend.common.idempotency import IdempotencyStore
    import backend.common.idempotency as idem_mod
    monkeypatch.setattr(idem_mod, "GLOBAL_IDEMPOTENCY_STORE", IdempotencyStore())
    import backend.prospecting.service as svc_mod
    monkeypatch.setattr(
        svc_mod, "GLOBAL_IDEMPOTENCY_STORE", idem_mod.GLOBAL_IDEMPOTENCY_STORE
    )


def test_process_discovery_gate_drops_weak_prospect(tmp_path, monkeypatch):
    monkeypatch.setenv("SAMUS_ARTIFACT_ROOT", str(tmp_path))
    monkeypatch.setenv("SAMUS_PROSPECTING_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    _isolate_idempotency(monkeypatch)

    prospects = {
        "95993": [
            ProspectRecord(
                prospect_id="pr_strong", company_name="Strong Co",
                phone="(530) 555-1000", website_url="https://strongco.example",
                website_status="live", seo_score=85,
                owner_email="owner@strongco.example",
                social_facebook="https://facebook.com/strongco",
                city="Yuba City", state="CA", zipcode="95993",
                industry="hvac", review_rating="4.8", review_count="120",
            ),
            ProspectRecord(
                prospect_id="pr_weak", company_name="Weak Co",
                website_url="https://weakco.example", website_status="parked",
                seo_score=0,
                city="Yuba City", state="CA", zipcode="95993",
                industry="hvac", review_rating="", review_count="0",
            ),
        ],
    }
    monkeypatch.setattr(
        "backend.prospecting.service.discover_for_zipcode",
        _fake_discover_factory(prospects),
    )

    from backend.prospecting.service import process_discovery
    req = DiscoveryRequest(
        campaign_name="gate_drop", zipcodes=["95993"], industries=["hvac"],
        max_results_per_zip=10, enable_seo_audit=False,
        enable_owner_enrichment=False, enable_full_audit_for_warm=False,
        enable_strategy_policy=False, enable_signal_filter_gate=True,
    )
    result = process_discovery(req, task_id="t-gate-drop")
    # The weak prospect is dropped before the call list is built.
    ids = {p.prospect_id for p in result.prospects}
    assert ids == {"pr_strong"}
    assert result.prospect_count == 1


def test_process_discovery_gate_disabled_keeps_all(tmp_path, monkeypatch):
    monkeypatch.setenv("SAMUS_ARTIFACT_ROOT", str(tmp_path))
    monkeypatch.setenv("SAMUS_PROSPECTING_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    _isolate_idempotency(monkeypatch)

    prospects = {
        "95993": [
            ProspectRecord(
                prospect_id="pr_strong", company_name="Strong Co",
                phone="(530) 555-1000", website_url="https://strongco.example",
                website_status="live", seo_score=85,
                city="Yuba City", state="CA", zipcode="95993",
                industry="hvac", review_rating="4.8", review_count="120",
            ),
            ProspectRecord(
                prospect_id="pr_weak", company_name="Weak Co",
                website_url="https://weakco.example", website_status="parked",
                seo_score=0,
                city="Yuba City", state="CA", zipcode="95993",
                industry="hvac", review_rating="", review_count="0",
            ),
        ],
    }
    monkeypatch.setattr(
        "backend.prospecting.service.discover_for_zipcode",
        _fake_discover_factory(prospects),
    )

    from backend.prospecting.service import process_discovery
    req = DiscoveryRequest(
        campaign_name="gate_off", zipcodes=["95993"], industries=["hvac"],
        max_results_per_zip=10, enable_seo_audit=False,
        enable_owner_enrichment=False, enable_full_audit_for_warm=False,
        enable_strategy_policy=False, enable_signal_filter_gate=False,
    )
    result = process_discovery(req, task_id="t-gate-off")
    # Gate disabled — both prospects survive to the call list.
    assert result.prospect_count == 2
