"""CODB investment reasoner — affordability, ROI ranking, bottleneck awareness,
recommend-only (no spend), guidance emission, and EOD integration.

All offline: financials are injected; the ledger is redirected to tmp via
SAMUS_STATE_ROOT so no network/Stripe/Firestore is touched.
"""
from __future__ import annotations

import pytest

from backend.cognitive import codb_reasoner as cr
from backend.cognitive.guidance import GuidanceLedger
from backend.finance.models import CodbInvestmentOption


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------
def _options():
    """A small deterministic catalog mirroring the real registry seed."""
    return [
        CodbInvestmentOption(
            id="apollo_basic", name="Apollo Basic", monthly_cost_usd=65,
            bottleneck="cold-email-reach", capability_gain=25.0,
            current_state="Apollo free = 403; ~10 emailable/day.",
        ),
        CodbInvestmentOption(
            id="apollo_professional", name="Apollo Professional", monthly_cost_usd=99,
            bottleneck="cold-email-reach", capability_gain=40.0,
        ),
        CodbInvestmentOption(
            id="vapi_topup", name="Vapi top-up", one_time_cost_usd=25,
            bottleneck="call-volume", capability_gain=2.0,
        ),
        CodbInvestmentOption(
            id="openai_cap_raise", name="OpenAI cap raise", monthly_cost_usd=20,
            bottleneck="llm-depth", capability_gain=3.0,
        ),
    ]


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("SAMUS_STATE_ROOT", str(tmp_path))
    monkeypatch.setenv("SAMUS_LEDGER_BACKEND", "jsonl")
    return GuidanceLedger()


# ---------------------------------------------------------------------------
# Affordability filter
# ---------------------------------------------------------------------------
def test_affordability_excludes_unaffordable():
    # headroom $30 -> only vapi_topup ($25) and openai_cap_raise ($20) fit;
    # apollo_basic ($65) and apollo_professional ($99) are excluded.
    fin = cr.Financials(available_cash_usd=40.0, mrr_usd=0.0, headroom_usd=30.0,
                        source="test")
    recs = cr.recommend_codb_investments(fin, options=_options())
    ids = {r.option_id for r in recs}
    assert "apollo_basic" not in ids
    assert "apollo_professional" not in ids
    assert ids == {"vapi_topup", "openai_cap_raise"}


def test_affordability_all_fit_with_large_headroom():
    fin = cr.Financials(available_cash_usd=1000.0, mrr_usd=0.0, headroom_usd=500.0,
                        source="test")
    recs = cr.recommend_codb_investments(fin, options=_options())
    assert {r.option_id for r in recs} == {
        "apollo_basic", "apollo_professional", "vapi_topup", "openai_cap_raise",
    }


def test_zero_headroom_recommends_nothing():
    fin = cr.Financials(available_cash_usd=0.0, mrr_usd=0.0, headroom_usd=0.0,
                        source="test")
    assert cr.recommend_codb_investments(fin, options=_options()) == []


# ---------------------------------------------------------------------------
# ROI ranking
# ---------------------------------------------------------------------------
def test_roi_ranking_order_without_hint():
    # No bottleneck hint -> pure capability_gain/cost ranking.
    #   openai_cap_raise: 3/20   = 0.15
    #   apollo_basic:     25/65  = 0.385
    #   apollo_professional: 40/99 = 0.404
    #   vapi_topup:       2/25   = 0.08
    fin = cr.Financials(available_cash_usd=1000.0, mrr_usd=0.0, headroom_usd=500.0,
                        source="test")
    recs = cr.recommend_codb_investments(fin, options=_options())
    order = [r.option_id for r in recs]
    assert order[0] == "apollo_professional"
    assert order[1] == "apollo_basic"
    assert order[-1] == "vapi_topup"
    # ROI is monotonically non-increasing in the returned order.
    rois = [r.roi_per_dollar for r in recs]
    assert rois == sorted(rois, reverse=True)


# ---------------------------------------------------------------------------
# Bottleneck awareness
# ---------------------------------------------------------------------------
def test_bottleneck_awareness_ranks_apollo_top():
    # With cold-email-reach as the binding constraint AND affordable, an Apollo
    # option must rank first (bottleneck-match bonus), ahead of a higher raw-ROI
    # non-matching option if any.
    fin = cr.Financials(available_cash_usd=1000.0, mrr_usd=0.0, headroom_usd=500.0,
                        source="test")
    recs = cr.recommend_codb_investments(
        fin, bottleneck_hint="cold-email-reach", options=_options()
    )
    assert recs[0].bottleneck == "cold-email-reach"
    assert recs[0].bottleneck_match is True
    assert recs[0].option_id in ("apollo_basic", "apollo_professional")
    # non-matching options come after the matching ones
    matching = [r for r in recs if r.bottleneck_match]
    non_matching = [r for r in recs if not r.bottleneck_match]
    assert recs[:len(matching)] == matching
    assert all(not r.bottleneck_match for r in non_matching)


def test_bottleneck_hint_when_match_unaffordable_falls_back():
    # Cold-email options don't fit ($65/$99 > $30 headroom) -> reasoner still
    # returns the affordable non-matching options ranked by ROI.
    fin = cr.Financials(available_cash_usd=40.0, mrr_usd=0.0, headroom_usd=30.0,
                        source="test")
    recs = cr.recommend_codb_investments(
        fin, bottleneck_hint="cold-email-reach", options=_options()
    )
    assert all(not r.bottleneck_match for r in recs)
    assert {r.option_id for r in recs} == {"vapi_topup", "openai_cap_raise"}


def test_top_recommendation_rationale_mentions_numbers():
    fin = cr.Financials(available_cash_usd=1000.0, mrr_usd=300.0, headroom_usd=750.0,
                        source="test")
    recs = cr.recommend_codb_investments(
        fin, bottleneck_hint="cold-email-reach", options=_options()
    )
    top = recs[0]
    assert "binding constraint" in top.rationale
    assert "$750" in top.rationale  # headroom appears
    assert "Recommend upgrade" in top.rationale
    assert top.numbers["headroom_usd"] == 750.0


# ---------------------------------------------------------------------------
# Recommend-only — NO purchase / spend side effects
# ---------------------------------------------------------------------------
def test_recommend_only_no_spend_calls(monkeypatch):
    """The reasoner must not touch any spend/purchase/budget-mutation path."""
    calls: list[str] = []

    # Poison the spend surfaces: any call is a failure.
    import backend.common.apollo_budget as ab

    def _boom(*a, **k):
        calls.append("spend")
        raise AssertionError("reasoner attempted a spend/record_spend call")

    monkeypatch.setattr(ab, "record_spend", _boom, raising=True)
    monkeypatch.setattr(ab.ApolloBudgetStore, "record_spend", _boom, raising=True)

    fin = cr.Financials(available_cash_usd=1000.0, mrr_usd=500.0, headroom_usd=750.0,
                        source="test")
    recs = cr.recommend_codb_investments(
        fin, bottleneck_hint="cold-email-reach", options=_options()
    )
    assert recs  # produced recommendations
    assert calls == []  # but spent nothing


def test_recommend_only_no_network(monkeypatch):
    """recommend_codb_investments with injected financials makes no HTTP call."""
    import backend.common.http_client as hc

    def _boom(*a, **k):
        raise AssertionError("reasoner attempted an HTTP call")

    if hasattr(hc, "signed_post_json_sync"):
        monkeypatch.setattr(hc, "signed_post_json_sync", _boom, raising=True)

    fin = cr.Financials(available_cash_usd=500.0, mrr_usd=0.0, headroom_usd=375.0,
                        source="test")
    recs = cr.recommend_codb_investments(fin, options=_options())
    assert recs


# ---------------------------------------------------------------------------
# Guidance-ledger emission
# ---------------------------------------------------------------------------
def test_emit_writes_guidance_records(ledger):
    fin = cr.Financials(available_cash_usd=1000.0, mrr_usd=500.0, headroom_usd=750.0,
                        source="test")
    recs = cr.recommend_codb_investments(
        fin, bottleneck_hint="cold-email-reach", options=_options()
    )
    ids = cr.emit_recommendations_to_guidance(recs, ledger=ledger, top_n=2)
    assert len(ids) == 2
    rows = ledger.all_latest()
    assert len(rows) == 2
    for row in rows:
        assert row.status == "proposed"           # never auto-accepted
        assert row.owner == "finance"
        assert "Scale CODB" in row.recommendation
        assert row.category == "resource_efficiency"


def test_emit_empty_is_noop(ledger):
    assert cr.emit_recommendations_to_guidance([], ledger=ledger) == []
    assert ledger.all_latest() == []


# ---------------------------------------------------------------------------
# EOD integration — flag ON includes the section; flag OFF omits it
# ---------------------------------------------------------------------------
def test_eod_includes_section_when_flag_on(ledger, monkeypatch):
    from backend.cognitive import intelligence_cycle as ic
    from backend.common import config as cfg

    # Force the flag ON and inject known financials + catalog via the reasoner.
    monkeypatch.setattr(cr, "read_financials",
                        lambda: cr.Financials(1000.0, 500.0, 750.0, "test"))
    monkeypatch.setattr(cr, "_load_options", _options)

    settings = cfg.get_settings()
    monkeypatch.setattr(settings, "codb_reasoner_enabled", True, raising=False)

    result = ic.run_end_of_day_review(
        llm=lambda s, p: '{"recommendations": []}',
        ledger=ledger,
        consult_openai=False,
    )
    assert "codb_investment_recommendations" in result
    section = result["codb_investment_recommendations"]
    assert section["count"] >= 1
    assert section["top"]["bottleneck"] == "cold-email-reach"
    assert section["emitted_recommendation_ids"]  # records were written


def test_eod_omits_section_when_flag_off(ledger, monkeypatch):
    from backend.cognitive import intelligence_cycle as ic
    from backend.common import config as cfg

    settings = cfg.get_settings()
    monkeypatch.setattr(settings, "codb_reasoner_enabled", False, raising=False)

    result = ic.run_end_of_day_review(
        llm=lambda s, p: '{"recommendations": []}',
        ledger=ledger,
        consult_openai=False,
    )
    assert "codb_investment_recommendations" not in result


# ---------------------------------------------------------------------------
# Deal-closed stimulus — no-op when disarmed, emits when armed
# ---------------------------------------------------------------------------
def test_on_deal_closed_noop_when_disarmed(ledger, monkeypatch):
    from backend.common import config as cfg

    settings = cfg.get_settings()
    monkeypatch.setattr(settings, "codb_reasoner_enabled", False, raising=False)

    out = cr.on_deal_closed(mrr_delta_usd=300.0, amount_usd=300.0, ledger=ledger)
    assert out["enabled"] is False
    assert out["emitted_recommendation_ids"] == []
    assert ledger.all_latest() == []


def test_on_deal_closed_emits_when_armed(ledger, monkeypatch):
    from backend.common import config as cfg

    monkeypatch.setattr(cr, "read_financials",
                        lambda: cr.Financials(1000.0, 500.0, 750.0, "test"))
    monkeypatch.setattr(cr, "_load_options", _options)

    settings = cfg.get_settings()
    monkeypatch.setattr(settings, "codb_reasoner_enabled", True, raising=False)

    out = cr.on_deal_closed(mrr_delta_usd=300.0, amount_usd=300.0, ledger=ledger)
    assert out["enabled"] is True
    assert out["top"]["bottleneck"] == "cold-email-reach"
    assert out["emitted_recommendation_ids"]
    assert len(ledger.all_latest()) == len(out["emitted_recommendation_ids"])


# ---------------------------------------------------------------------------
# Headroom math
# ---------------------------------------------------------------------------
def test_headroom_prefers_cash_minus_reserve(monkeypatch):
    monkeypatch.setattr(cr, "_SAFETY_RESERVE_PCT", 0.25)
    assert cr._compute_headroom(1000.0, 500.0) == 750.0


def test_headroom_falls_back_to_mrr_slice(monkeypatch):
    monkeypatch.setattr(cr, "_MRR_HEADROOM_PCT", 0.10)
    # no cash -> 10% of MRR
    assert cr._compute_headroom(0.0, 500.0) == 50.0


def test_registry_seed_loads_real_options():
    """The real codb_registry.yaml carries the seeded investment catalog."""
    opts = cr._load_options()
    ids = {o.id for o in opts}
    assert "apollo_basic" in ids
    assert "vapi_topup" in ids
    basic = next(o for o in opts if o.id == "apollo_basic")
    assert basic.monthly_cost_usd == 65
    assert basic.bottleneck == "cold-email-reach"
