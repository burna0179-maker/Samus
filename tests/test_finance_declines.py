"""Declines loader + filter_recent + cash-distress heuristic."""

from __future__ import annotations

from datetime import date


def test_load_default_registry_validates():
    from backend.finance.declines import load_registry

    reg = load_registry()
    # PDF seed: 17 events Feb-May 2026
    assert len(reg.events) == 17
    # Spot-check: Microsoft 365 cancellation is severity=critical.
    crit = [e for e in reg.events if e.severity == "critical"]
    assert len(crit) == 1
    assert "Microsoft" in crit[0].vendor


def test_filter_recent_30_day_window():
    from backend.finance.declines import filter_recent
    from backend.finance.models import DeclineEvent

    events = [
        DeclineEvent(date=date(2026, 5, 10), vendor="X", severity="medium"),
        DeclineEvent(date=date(2026, 4, 15), vendor="Y", severity="high"),
        DeclineEvent(date=date(2026, 2, 20), vendor="Z", severity="low"),
    ]
    out = filter_recent(events, window_days=30, today=date(2026, 5, 15))
    ids = [e.vendor for e in out]
    assert "X" in ids
    assert "Y" in ids  # April 15 is within 30 days of May 15
    assert "Z" not in ids  # Feb 20 is far outside


def test_evaluate_distress_ok_when_empty():
    from backend.finance.declines import evaluate_distress

    status, reasons = evaluate_distress([])
    assert status == "ok"
    assert reasons == []


def test_evaluate_distress_critical_on_service_canceled():
    from backend.finance.declines import evaluate_distress
    from backend.finance.models import DeclineEvent

    events = [DeclineEvent(date=date(2026, 5, 9), vendor="MS", severity="critical")]
    status, reasons = evaluate_distress(events)
    assert status == "critical"
    assert any("critical" in r for r in reasons)


def test_evaluate_distress_degraded_on_three_high():
    from backend.finance.declines import evaluate_distress
    from backend.finance.models import DeclineEvent

    events = [
        DeclineEvent(date=date(2026, 5, 1), vendor="A", severity="high"),
        DeclineEvent(date=date(2026, 5, 2), vendor="B", severity="high"),
        DeclineEvent(date=date(2026, 5, 3), vendor="C", severity="high"),
    ]
    status, reasons = evaluate_distress(events)
    assert status == "degraded"
    assert any("high" in r for r in reasons)


def test_evaluate_distress_degraded_on_six_total():
    from backend.finance.declines import evaluate_distress
    from backend.finance.models import DeclineEvent

    events = [
        DeclineEvent(date=date(2026, 5, i + 1), vendor=f"v{i}", severity="low") for i in range(6)
    ]
    status, reasons = evaluate_distress(events)
    assert status == "degraded"
    assert any("6" in r for r in reasons)


def test_evaluate_distress_critical_when_high_count_5plus():
    from backend.finance.declines import evaluate_distress
    from backend.finance.models import DeclineEvent

    events = [
        DeclineEvent(date=date(2026, 5, i + 1), vendor=f"v{i}", severity="high") for i in range(5)
    ]
    status, reasons = evaluate_distress(events)
    assert status == "critical"


def test_evaluate_distress_critical_when_total_10plus():
    from backend.finance.declines import evaluate_distress
    from backend.finance.models import DeclineEvent

    events = [
        DeclineEvent(date=date(2026, 5, i + 1), vendor=f"v{i}", severity="low") for i in range(10)
    ]
    status, reasons = evaluate_distress(events)
    assert status == "critical"


def test_summarize_with_pinned_today():
    from backend.finance.declines import load_registry, summarize

    reg = load_registry()
    # Pin to 2026-05-15: 30-day window catches Apr 15 onward.
    summary = summarize(reg, window_days=30, ts="2026-05-15T00:00:00Z", today=date(2026, 5, 15))
    # All events from April 15 onward (and May) — should include the
    # critical Microsoft cancellation (5/9) so cash_distress=critical.
    assert summary.cash_distress == "critical"
    assert summary.recent_total_count > 0
    # Recent events sorted desc by date.
    if len(summary.recent_events) > 1:
        dates = [e.date for e in summary.recent_events]
        assert dates == sorted(dates, reverse=True)


def test_summarize_distant_window_returns_ok():
    from backend.finance.declines import load_registry, summarize

    reg = load_registry()
    # Pin today to 2030 so no events fall in the window.
    summary = summarize(reg, window_days=7, ts="2030-01-01T00:00:00Z", today=date(2030, 1, 1))
    assert summary.recent_total_count == 0
    assert summary.cash_distress == "ok"
    assert summary.distress_reasons == []
