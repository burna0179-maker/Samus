"""ROI roll-ups — daily revenue-minus-cost aggregation + persistence.

Fixtures write the real concrete ledgers (outreach conversions JSONL, Stripe
webhook event ledger, voice events, llm-budget JSON) so the roll-up totals
are reconciled against hand-computed sums, pre-merge (empty event stream).
"""

from __future__ import annotations

import datetime as _dt
import json
import sys
import types

import pytest

from backend.finance import roi
from backend.finance.roi import (
    ROW_KIND,
    RoiRollup,
    compute_daily_rollup,
    get_rollup,
    load_rollup,
    run_nightly_rollup,
    save_rollup,
)

TODAY = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")


@pytest.fixture()
def roi_env(monkeypatch, tmp_path):
    """Redirect every data source the roll-up reads to tmp fixtures."""
    conv = tmp_path / "outreach_conversions.jsonl"
    stripe = tmp_path / "stripe_events.jsonl"
    voice = tmp_path / "voice_events.jsonl"
    budgets = tmp_path / "llm_budgets.json"
    store = tmp_path / "roi_rollups.json"
    monkeypatch.setenv("SAMUS_OUTREACH_CONVERSIONS_LOG", str(conv))
    monkeypatch.setenv("SAMUS_STRIPE_EVENT_LOG", str(stripe))
    monkeypatch.setenv("SAMUS_VOICE_EVENTS_PATH", str(voice))
    monkeypatch.setenv("SAMUS_LLM_BUDGET_PATH", str(budgets))
    monkeypatch.setenv("SAMUS_ROI_ROLLUP_PATH", str(store))
    monkeypatch.setenv("DDB_PORTFOLIO_SNAPSHOTS_TABLE", "")  # skip DDB mirror
    return types.SimpleNamespace(
        conv=conv,
        stripe=stripe,
        voice=voice,
        budgets=budgets,
        store=store,
        tmp=tmp_path,
    )


def _write_jsonl(path, rows):
    path.write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n",
        encoding="utf-8",
    )


def _stripe_row(event_id: str, amount: float, *, day: str = TODAY, status="paid"):
    return {
        "event_id": event_id,
        "event_type": "checkout.session.completed",
        "received_at": f"{day}T10:00:00Z",
        "livemode": True,
        "amount_total_usd": amount,
        "payment_status": status,
        "process_status": "processed",
        "hf_offer_code": "seo_audit",
    }


def _conv_row(event_id: str, prospect_id: str, amount: float, *, day: str = TODAY):
    return {
        "prospect_id": prospect_id,
        "email": "x@y.z",
        "amount_usd": amount,
        "currency": "usd",
        "offer_code": "flyer",
        "event_id": event_id,
        "attributed_at": f"{day}T09:00:00Z",
    }


# ---------------------------------------------------------------------------
# compute_daily_rollup — totals reconcile with the concrete ledgers
# ---------------------------------------------------------------------------


def test_empty_sources_yield_zero_rollup(roi_env):
    rollup = compute_daily_rollup(TODAY)
    assert isinstance(rollup, RoiRollup)
    assert rollup.revenue_usd == 0.0
    assert rollup.cost_usd == 0.0
    assert rollup.net_usd == 0.0
    assert rollup.conversion_count == 0
    # Post HOTL-T1 merge the unified stream is always importable — the flag
    # reflects module presence, not whether any events happen to be persisted.
    assert rollup.events_stream_available is True


def test_revenue_from_both_concrete_ledgers_with_event_id_dedup(roi_env):
    # Conversion evt_1 ($150) also appears in the webhook ledger — counted ONCE.
    _write_jsonl(roi_env.conv, [_conv_row("evt_1", "p_1", 150.0)])
    _write_jsonl(
        roi_env.stripe,
        [
            _stripe_row("evt_1", 150.0),  # dup of the conversion — skipped
            _stripe_row("evt_2", 500.0),  # direct sale — counted
            _stripe_row("evt_3", 75.0, day="2020-01-01"),  # wrong day — skipped
            _stripe_row("evt_4", 20.0, status="unpaid"),  # unpaid — skipped
        ],
    )
    rollup = compute_daily_rollup(TODAY)
    assert rollup.revenue_usd == pytest.approx(650.0)
    assert rollup.conversion_count == 2
    assert rollup.sources == {
        "outreach_conversions": 1,
        "webhook_ledger": 1,
        "event_stream": 0,
    }
    # Dimension grouping: conversion is email/outreach, webhook is direct.
    assert rollup.by_channel["email"]["revenue_usd"] == pytest.approx(150.0)
    assert rollup.by_channel["direct"]["revenue_usd"] == pytest.approx(500.0)
    assert rollup.by_lead_source["outreach"]["revenue_usd"] == pytest.approx(150.0)
    assert rollup.by_lead_source["seo_audit"]["revenue_usd"] == pytest.approx(500.0)
    # No campaign_id pre-merge -> "(none)" bucket carries everything.
    assert rollup.by_campaign["(none)"]["revenue_usd"] == pytest.approx(650.0)


def test_multitouch_falls_back_to_recording_workcell_when_no_journey(roi_env):
    """When the unified stream has no events for a prospect, revenue is 100%
    attributed to the recording workcell (outreach for conversions, finance
    for direct Stripe events). Post-merge this is the "empty journey" fallback
    inside :func:`_journey_workcells`; pre-merge it was the only code path.
    """
    _write_jsonl(roi_env.conv, [_conv_row("evt_1", "p_1", 100.0)])
    _write_jsonl(roi_env.stripe, [_stripe_row("evt_2", 300.0)])
    rollup = compute_daily_rollup(TODAY)
    # No prospect journey events -> 100% to each item's recording workcell.
    assert rollup.by_workcell["outreach"]["revenue_usd"] == pytest.approx(100.0)
    assert rollup.by_workcell["finance"]["revenue_usd"] == pytest.approx(300.0)


def test_costs_from_llm_budget_and_voice_ledger(roi_env):
    # llm budget: today's bucket for two workcells; a stale bucket ignored.
    roi_env.budgets.write_text(
        json.dumps(
            {
                "strategy": {
                    "bucket_day": TODAY,
                    "input_tokens_today": 1_000_000,
                    "output_tokens_today": 0,
                },
                "seo": {
                    "bucket_day": "2020-01-01",
                    "input_tokens_today": 9_999_999,
                    "output_tokens_today": 9_999_999,
                },
            }
        ),
        encoding="utf-8",
    )
    # voice: one call today ($0.42), one on another day.
    _write_jsonl(
        roi_env.voice,
        [
            {"kind": "end_of_call", "ts": f"{TODAY}T11:00:00Z", "vapi_cost": 0.42},
            {"kind": "end_of_call", "ts": "2020-01-01T11:00:00Z", "vapi_cost": 5.0},
            {"kind": "status_update", "ts": f"{TODAY}T11:00:00Z", "vapi_cost": 9.0},
        ],
    )
    rollup = compute_daily_rollup(TODAY)
    from backend.common.llm_pricing import cost_from_usage

    expected_llm = float(
        cost_from_usage(
            "claude-haiku-4",
            {"input_tokens": 1_000_000, "output_tokens": 0},
        )
    )
    assert expected_llm > 0
    assert rollup.cost_usd == pytest.approx(round(expected_llm, 4) + 0.42, abs=1e-6)
    assert rollup.by_workcell["strategy"]["cost_usd"] == pytest.approx(round(expected_llm, 4))
    assert rollup.by_workcell["voice"]["cost_usd"] == pytest.approx(0.42)
    assert rollup.by_channel["call"]["cost_usd"] == pytest.approx(0.42)
    assert "seo" not in rollup.by_workcell  # stale bucket_day excluded


def test_net_is_revenue_minus_cost(roi_env):
    _write_jsonl(roi_env.stripe, [_stripe_row("evt_1", 100.0)])
    _write_jsonl(
        roi_env.voice,
        [
            {"kind": "end_of_call", "ts": f"{TODAY}T11:00:00Z", "vapi_cost": 10.0},
        ],
    )
    rollup = compute_daily_rollup(TODAY)
    assert rollup.revenue_usd == pytest.approx(100.0)
    assert rollup.cost_usd == pytest.approx(10.0)
    assert rollup.net_usd == pytest.approx(90.0)


# ---------------------------------------------------------------------------
# Unified event stream (post-merge behaviour, via a fake business_events)
# ---------------------------------------------------------------------------


@pytest.fixture()
def fake_events_module(monkeypatch):
    """Install a fake backend.common.business_events the shim delegates to."""
    mod = types.ModuleType("backend.common.business_events")
    mod._events = []  # type: ignore[attr-defined]

    def emit_business_event(event_type, **kwargs):
        ev = {"event_type": event_type, **kwargs}
        mod._events.append(ev)  # type: ignore[attr-defined]
        return ev

    def read_events(
        prospect_id=None, opportunity_id=None, since=None, event_types=None, limit=1000
    ):
        out = []
        for ev in mod._events:  # type: ignore[attr-defined]
            if prospect_id and ev.get("prospect_id") != prospect_id:
                continue
            if event_types and ev["event_type"] not in event_types:
                continue
            out.append(dict(ev))
        return out[:limit]

    mod.emit_business_event = emit_business_event  # type: ignore[attr-defined]
    mod.read_events = read_events  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "backend.common.business_events", mod)
    return mod


def test_multitouch_splits_across_journey_workcells(roi_env, fake_events_module):
    # Journey: prospecting -> outreach -> voice touched p_1.
    for wc in ("prospecting", "outreach", "voice"):
        fake_events_module._events.append(
            {
                "event_type": "email.sent",
                "workcell": wc,
                "prospect_id": "p_1",
                "ts": f"{TODAY}T08:00:00Z",
            }
        )
    _write_jsonl(roi_env.conv, [_conv_row("evt_1", "p_1", 300.0)])
    rollup = compute_daily_rollup(TODAY)
    assert rollup.events_stream_available is True
    for wc in ("prospecting", "outreach", "voice"):
        assert rollup.by_workcell[wc]["revenue_usd"] == pytest.approx(100.0)
    assert rollup.revenue_usd == pytest.approx(300.0)


def test_event_stream_payment_and_costs_counted_with_dims(roi_env, fake_events_module):
    fake_events_module._events.extend(
        [
            {
                "event_type": "payment.received",
                "workcell": "finance",
                "prospect_id": "p_9",
                "ts": f"{TODAY}T12:00:00Z",
                "revenue_usd": 250.0,
                "campaign_id": "camp_1",
                "variant_arm_id": "subject::v3",
                "metadata": {"channel": "email", "lead_source": "campaign"},
            },
            {
                "event_type": "email.sent",
                "workcell": "outreach",
                "prospect_id": "p_9",
                "ts": f"{TODAY}T07:00:00Z",
                "cost_usd": 0.10,
                "campaign_id": "camp_1",
                "metadata": {"channel": "email"},
            },
        ]
    )
    rollup = compute_daily_rollup(TODAY)
    assert rollup.revenue_usd == pytest.approx(250.0)
    assert rollup.by_campaign["camp_1"]["revenue_usd"] == pytest.approx(250.0)
    assert rollup.by_campaign["camp_1"]["cost_usd"] == pytest.approx(0.10)
    assert rollup.by_variant_arm["subject::v3"]["revenue_usd"] == pytest.approx(250.0)
    assert rollup.by_channel["email"]["net_usd"] == pytest.approx(250.0 - 0.10)


# ---------------------------------------------------------------------------
# Persistence + nightly entry point
# ---------------------------------------------------------------------------


def test_save_and_load_rollup_round_trip(roi_env):
    rollup = compute_daily_rollup(TODAY)
    assert save_rollup(rollup) is True
    stored = load_rollup(TODAY)
    assert stored is not None
    assert stored["row_kind"] == ROW_KIND
    assert stored["day"] == TODAY


def test_get_rollup_serves_stored_historical_day(roi_env):
    old_day = "2026-01-02"
    _write_jsonl(roi_env.stripe, [_stripe_row("evt_h", 40.0, day=old_day)])
    first = get_rollup(old_day)
    assert first["revenue_usd"] == pytest.approx(40.0)
    # Remove the source ledger — the stored roll-up still serves.
    roi_env.stripe.unlink()
    second = get_rollup(old_day)
    assert second["revenue_usd"] == pytest.approx(40.0)
    # recompute=True re-reads the (now empty) sources.
    third = get_rollup(old_day, recompute=True)
    assert third["revenue_usd"] == 0.0


def test_run_nightly_rollup_defaults_to_yesterday_and_persists(roi_env):
    yesterday = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=1)).strftime("%Y-%m-%d")
    _write_jsonl(roi_env.stripe, [_stripe_row("evt_y", 99.0, day=yesterday)])
    rollup = run_nightly_rollup()
    assert rollup.day == yesterday
    assert rollup.revenue_usd == pytest.approx(99.0)
    assert load_rollup(yesterday) is not None


def test_run_nightly_rollup_emits_business_event(roi_env, fake_events_module):
    run_nightly_rollup(day=TODAY)
    decisions = [e for e in fake_events_module._events if e["event_type"] == "decision.made"]
    assert len(decisions) == 1
    assert decisions[0]["metadata"]["decision_kind"] == "roi_nightly_rollup"


# ---------------------------------------------------------------------------
# Gateway route registration (one line in gateway/app.py binds these)
# ---------------------------------------------------------------------------


def test_admin_routes_serve_roi_and_arbitration(roi_env, monkeypatch, tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    monkeypatch.setenv("SAMUS_ARBITRATION_LOG", str(tmp_path / "arb.jsonl"))
    monkeypatch.setenv("SAMUS_STATE_ROOT", str(tmp_path))
    _write_jsonl(roi_env.stripe, [_stripe_row("evt_r", 60.0)])

    app = FastAPI()
    roi.register_economics_admin_routes(app)
    client = TestClient(app)

    resp = client.get("/admin/roi")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["rollup"]["revenue_usd"] == pytest.approx(60.0)

    # No arbitration history yet -> ok with None payload.
    resp = client.get("/admin/arbitration")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "arbitration": None}

    # refresh=true runs a fresh (empty-source) arbitration and persists it.
    resp = client.get("/admin/arbitration", params={"refresh": "true"})
    assert resp.status_code == 200
    assert resp.json()["arbitration"]["bid_count"] == 0
    resp = client.get("/admin/arbitration")
    assert resp.json()["arbitration"] is not None
