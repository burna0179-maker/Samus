"""Smoke tests for the morning briefing CLI.

Targets the formatter — Stripe call is stubbed out via env-var unset +
service helpers return real (empty) registries from the gitignored yaml
fallback path.
"""

from __future__ import annotations


def _isolate_phase3_empty(monkeypatch, tmp_path):
    """Point all finance yaml loaders at nonexistent paths -> empty registries."""
    monkeypatch.setenv("SAMUS_DEBTS_PATH", str(tmp_path / "_d.yaml"))
    monkeypatch.setenv("SAMUS_ACTIONS_PATH", str(tmp_path / "_a.yaml"))
    monkeypatch.setenv("SAMUS_INFO_GAPS_PATH", str(tmp_path / "_g.yaml"))
    monkeypatch.setenv("SAMUS_HARDSHIP_PATH", str(tmp_path / "_h.yaml"))
    monkeypatch.setenv("SAMUS_CODB_REGISTRY_PATH", str(tmp_path / "_codb.yaml"))
    (tmp_path / "_codb.yaml").write_text(
        "costs: []\nrevenue_targets: {monthly_minimum_usd: 0, runway_alert_days: 60}\n",
        encoding="utf-8",
    )


def _stub_stripe_cash(monkeypatch, *, available=0.0, mrr=0.0, subs=0, err=None):
    """Replace _fetch_stripe_cash so tests don't hit the live API."""
    import backend.morning as morning_mod

    monkeypatch.setattr(
        morning_mod,
        "_fetch_stripe_cash",
        lambda: (available, mrr, subs, err),
    )


def _stub_payment_links(
    monkeypatch, *, stripe_reachable=False, stripe_error="stripe_api_key_unset", links=None
):
    """Replace get_payment_links so tests don't hit the live Stripe API."""
    import backend.morning as morning_mod
    from backend.finance.models import PaymentLinksRollup

    rollup = PaymentLinksRollup(
        links=links or [],
        count_total=len(links or []),
        count_subscription=sum(1 for s in (links or []) if s.is_subscription),
        count_one_time=sum(1 for s in (links or []) if not s.is_subscription),
        livemode_count=sum(1 for s in (links or []) if s.livemode),
        stripe_reachable=stripe_reachable,
        stripe_error=stripe_error,
        ts="2026-05-15T00:00:00Z",
    )
    monkeypatch.setattr(morning_mod, "get_payment_links", lambda *_, **__: rollup)


def test_briefing_renders_with_no_data(tmp_path, monkeypatch):
    _isolate_phase3_empty(monkeypatch, tmp_path)
    _stub_stripe_cash(monkeypatch, err="stripe_api_key_unset")
    _stub_payment_links(monkeypatch)
    monkeypatch.setenv("SAMUS_MORNING_NO_COLOR", "1")
    from backend.morning import render_briefing

    out = render_briefing()
    # Sectioning is present even on empty data.
    assert "SAMUS MORNING BRIEFING" in out
    assert "CRITICAL" in out
    assert "CASH" in out
    assert "DEBT PORTFOLIO" in out
    assert "SALES" in out
    assert "OPEN INFO GAPS" in out
    # Empty state messaging.
    assert "(no actions due today)" in out
    assert "(debts.yaml not populated)" in out
    assert "stripe_api_key_unset" in out


def test_briefing_surfaces_live_stripe_mrr(tmp_path, monkeypatch):
    _isolate_phase3_empty(monkeypatch, tmp_path)
    _stub_stripe_cash(monkeypatch, available=269.53, mrr=300.00, subs=1)
    _stub_payment_links(monkeypatch)
    monkeypatch.setenv("SAMUS_MORNING_NO_COLOR", "1")
    from backend.morning import render_briefing

    out = render_briefing()
    assert "$269.53" in out
    assert "$300.00" in out
    assert "1 active subscription" in out


def test_briefing_renders_payment_links_section(tmp_path, monkeypatch):
    _isolate_phase3_empty(monkeypatch, tmp_path)
    _stub_stripe_cash(monkeypatch, err="stripe_api_key_unset")
    from backend.finance.models import PaymentLinkSummary

    _stub_payment_links(
        monkeypatch,
        stripe_reachable=True,
        stripe_error=None,
        links=[
            PaymentLinkSummary(
                id="p1",
                offer_code="seo_audit",
                url="https://buy.stripe.com/9B6fZgcoW7yWbSm6Hy8so0h",
                is_subscription=False,
                livemode=True,
                samus_managed=True,
            ),
            PaymentLinkSummary(
                id="p2",
                offer_code="seo_optimization",
                url="https://buy.stripe.com/6oU5kCagO7yW9Keea08so0i",
                is_subscription=True,
                livemode=True,
                samus_managed=True,
            ),
        ],
    )
    monkeypatch.setenv("SAMUS_MORNING_NO_COLOR", "1")
    from backend.morning import render_briefing

    out = render_briefing()
    assert "Live payment links: 2 active" in out
    assert "1 one-time" in out
    assert "1 subscription" in out
    assert "2 livemode" in out
    # Summary line only — the per-link URL inventory is intentionally
    # omitted as operator noise (count + type breakdown is sufficient).
    assert "https://buy.stripe.com/" not in out
    assert "seo_audit" not in out


def test_briefing_renders_payment_links_unavailable(tmp_path, monkeypatch):
    _isolate_phase3_empty(monkeypatch, tmp_path)
    _stub_stripe_cash(monkeypatch, err="stripe_api_key_unset")
    _stub_payment_links(
        monkeypatch, stripe_reachable=False, stripe_error="stripe_http_403: forbidden"
    )
    monkeypatch.setenv("SAMUS_MORNING_NO_COLOR", "1")
    from backend.morning import render_briefing

    out = render_briefing()
    assert "Live payment links: unavailable" in out
    assert "stripe_http_403" in out


def test_briefing_surfaces_critical_warrant_gap(tmp_path, monkeypatch):
    _isolate_phase3_empty(monkeypatch, tmp_path)
    _stub_stripe_cash(monkeypatch, err="stripe_api_key_unset")
    _stub_payment_links(monkeypatch)
    # Seed a critical info gap to match the real-world Berg-warrant case.
    (tmp_path / "_g.yaml").write_text(
        "gaps:\n"
        "  - {id: G1, priority: critical, related_debt_id: DBT-006,\n"
        "     gap: 'Status of probation-violation warrant',\n"
        "     how_to_close: 'Call Berg Law office', status: open}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SAMUS_INFO_GAPS_PATH", str(tmp_path / "_g.yaml"))
    monkeypatch.setenv("SAMUS_MORNING_NO_COLOR", "1")
    from backend.morning import render_briefing

    out = render_briefing()
    assert "Status of probation-violation warrant" in out
    assert "DBT-006" in out
    assert "Call Berg Law office" in out


def test_briefing_renders_overdue_actions_when_present(tmp_path, monkeypatch):
    _isolate_phase3_empty(monkeypatch, tmp_path)
    _stub_stripe_cash(monkeypatch, err="stripe_api_key_unset")
    _stub_payment_links(monkeypatch)
    # Pin an action in the past so it shows as overdue regardless of today.
    (tmp_path / "_a.yaml").write_text(
        "actions:\n"
        "  - {id: A1, when: TODAY, due_date: 2020-01-01,\n"
        "     action: 'Ancient overdue thing', where: 'somewhere', status: open}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SAMUS_ACTIONS_PATH", str(tmp_path / "_a.yaml"))
    monkeypatch.setenv("SAMUS_MORNING_NO_COLOR", "1")
    from backend.morning import render_briefing

    out = render_briefing()
    assert "OVERDUE" in out
    assert "Ancient overdue thing" in out


def test_no_color_flag_strips_ansi(tmp_path, monkeypatch):
    _isolate_phase3_empty(monkeypatch, tmp_path)
    _stub_stripe_cash(monkeypatch, err="stripe_api_key_unset")
    _stub_payment_links(monkeypatch)
    monkeypatch.setenv("SAMUS_MORNING_NO_COLOR", "1")
    from backend.morning import render_briefing

    out = render_briefing()
    assert "\033[" not in out


def test_main_returns_zero_on_success(tmp_path, monkeypatch, capsys):
    _isolate_phase3_empty(monkeypatch, tmp_path)
    _stub_stripe_cash(monkeypatch, err="stripe_api_key_unset")
    from backend.morning import main

    code = main(["--no-color"])
    assert code == 0
    captured = capsys.readouterr()
    assert "SAMUS MORNING BRIEFING" in captured.out


def test_stripe_subscription_mrr_math():
    """monthly_recurring_revenue_usd math against synthetic fixtures."""
    from backend.finance.models import (
        StripeRecurring,
        StripeSubscription,
        StripeSubscriptionItem,
        StripeSubscriptionItemPrice,
    )
    from backend.finance.stripe_client import monthly_recurring_revenue_usd

    # 1x $300/mo + 1x $1200/yr + 2x $50/week + 1x EUR(skipped)
    monthly = StripeSubscription(
        id="s1",
        status="active",
        items=[
            StripeSubscriptionItem(
                id="i1",
                quantity=1,
                price=StripeSubscriptionItemPrice(
                    id="p1",
                    unit_amount=30000,
                    currency="usd",
                    recurring=StripeRecurring(interval="month", interval_count=1),
                ),
            ),
        ],
    )
    yearly = StripeSubscription(
        id="s2",
        status="active",
        items=[
            StripeSubscriptionItem(
                id="i2",
                quantity=1,
                price=StripeSubscriptionItemPrice(
                    id="p2",
                    unit_amount=120000,
                    currency="usd",
                    recurring=StripeRecurring(interval="year", interval_count=1),
                ),
            ),
        ],
    )
    weekly = StripeSubscription(
        id="s3",
        status="active",
        items=[
            StripeSubscriptionItem(
                id="i3",
                quantity=2,
                price=StripeSubscriptionItemPrice(
                    id="p3",
                    unit_amount=5000,
                    currency="usd",
                    recurring=StripeRecurring(interval="week", interval_count=1),
                ),
            ),
        ],
    )
    eur = StripeSubscription(
        id="s4",
        status="active",
        items=[
            StripeSubscriptionItem(
                id="i4",
                quantity=1,
                price=StripeSubscriptionItemPrice(
                    id="p4",
                    unit_amount=10000,
                    currency="eur",
                    recurring=StripeRecurring(interval="month", interval_count=1),
                ),
            ),
        ],
    )
    mrr = monthly_recurring_revenue_usd([monthly, yearly, weekly, eur])
    # 300 + (1200/12=100) + (2*50*4.33=433) + 0 = 833
    assert abs(mrr - 833.0) < 0.01


def test_subscription_envelope_flattening():
    """Stripe wraps items as {items: {data: [...]}}; verify we flatten."""
    from backend.finance.models import StripeSubscription

    raw = {
        "id": "sub_x",
        "status": "active",
        "items": {
            "data": [
                {
                    "id": "si_a",
                    "quantity": 1,
                    "price": {
                        "id": "price_a",
                        "unit_amount": 30000,
                        "currency": "usd",
                        "recurring": {"interval": "month", "interval_count": 1},
                    },
                }
            ]
        },
    }
    sub = StripeSubscription.model_validate_envelope(raw)
    assert sub.id == "sub_x"
    assert len(sub.items) == 1
    assert sub.items[0].price.unit_amount == 30000


def test_briefing_sales_lists_call_list_prospects(tmp_path, monkeypatch):
    """SALES section renders today's prospects from the call-list CSV."""
    import datetime as _dt

    _isolate_phase3_empty(monkeypatch, tmp_path)
    _stub_stripe_cash(monkeypatch, err="stripe_api_key_unset")
    _stub_payment_links(monkeypatch)
    monkeypatch.setenv("SAMUS_MORNING_NO_COLOR", "1")
    monkeypatch.setenv("SAMUS_ARTIFACT_ROOT", str(tmp_path))

    day = _dt.date(2026, 5, 20)
    calls = tmp_path / "daily_calls"
    calls.mkdir(parents=True, exist_ok=True)
    (calls / f"call_list_{day.isoformat()}.csv").write_text(
        "call_priority,company_name,phone,city,industry,lead_score,seo_score,owner_name\n"
        "hot,Acme HVAC,(555) 111-2222,Yuba City,hvac contractor,78,30,Dana Reed\n"
        "low,Stale Shells LLC,(555) 333-4444,Marysville,dentist,40,90,\n",
        encoding="utf-8",
    )
    from backend.morning import render_briefing

    out = render_briefing(today=day)
    assert "2 prospects ready" in out
    assert "1 hot, 0 warm, 1 low" in out
    assert "Acme HVAC" in out
    assert "(555) 111-2222" in out
    assert "ask for Dana Reed" in out  # owner enrichment surfaced
    assert f"morning_call_list_{day.isoformat()}.txt" in out  # attachment pointer


def test_briefing_sales_no_call_list_message(tmp_path, monkeypatch):
    """SALES section degrades gracefully when no call-list CSV exists."""
    import datetime as _dt

    _isolate_phase3_empty(monkeypatch, tmp_path)
    _stub_stripe_cash(monkeypatch, err="stripe_api_key_unset")
    _stub_payment_links(monkeypatch)
    monkeypatch.setenv("SAMUS_MORNING_NO_COLOR", "1")
    monkeypatch.setenv("SAMUS_ARTIFACT_ROOT", str(tmp_path))  # no daily_calls/ csv
    from backend.morning import render_briefing

    out = render_briefing(today=_dt.date(2026, 5, 20))
    assert "no call list for today" in out


def test_briefing_sales_shows_yesterdays_logged_calls(tmp_path, monkeypatch):
    """SALES section surfaces yesterday's hand-logged calls from the journal."""
    import datetime as _dt
    import json as _json

    _isolate_phase3_empty(monkeypatch, tmp_path)
    _stub_stripe_cash(monkeypatch, err="stripe_api_key_unset")
    _stub_payment_links(monkeypatch)
    monkeypatch.setenv("SAMUS_MORNING_NO_COLOR", "1")
    monkeypatch.setenv("SAMUS_ARTIFACT_ROOT", str(tmp_path))

    day = _dt.date(2026, 5, 21)
    yesterday = day - _dt.timedelta(days=1)
    calls = tmp_path / "daily_calls"
    calls.mkdir(parents=True, exist_ok=True)
    (calls / f"call_outcomes_{yesterday.isoformat()}.jsonl").write_text(
        _json.dumps(
            {"outcome": "booked", "company": "Acme HVAC", "notes": "owner booked an audit Thu 2pm"}
        )
        + "\n"
        + _json.dumps({"outcome": "no_answer", "company": "Quiet Co", "notes": "answering machine"})
        + "\n",
        encoding="utf-8",
    )
    from backend.morning import render_briefing

    out = render_briefing(today=day)
    assert "Calls logged yesterday: 2" in out
    assert "1 booked" in out and "1 no_answer" in out
    assert "Acme HVAC" in out  # booked one is listed
    assert "owner booked an audit Thu 2pm" in out  # with its notes
    assert "Quiet Co" not in out  # no_answer not individually listed
