"""Cut 2 — get_mrr_adds rollup tests.

Verifies the service helper filters the stripe_events JSONL correctly:
only subscription rows with a positive MRR contribute, the window
boundary works, and non-subscription events are excluded.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


def _isolate_log(monkeypatch, tmp_path):
    monkeypatch.setenv("SAMUS_STRIPE_EVENT_LOG", str(tmp_path / "stripe_events.jsonl"))


def _fresh_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _stale_ts(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
        "%Y-%m-%dT%H:%M:%SZ",
    )


# ---------------------------------------------------------------------------
# Empty / no-log behavior
# ---------------------------------------------------------------------------


def test_get_mrr_adds_empty_when_no_log(tmp_path, monkeypatch):
    monkeypatch.setenv("SAMUS_STRIPE_EVENT_LOG", str(tmp_path / "nope.jsonl"))
    from backend.finance.service import get_mrr_adds

    summary = get_mrr_adds(window_days=7)
    assert summary.window_days == 7
    assert summary.added_count == 0
    assert summary.total_mrr_usd == 0.0
    assert summary.recent_adds == []


def test_get_mrr_adds_empty_when_log_has_no_subscriptions(tmp_path, monkeypatch):
    _isolate_log(monkeypatch, tmp_path)
    from backend.finance.models import WebhookEventRecord
    from backend.finance.webhook import append_event_record

    # Only payment-mode events — no subscription_id.
    append_event_record(
        WebhookEventRecord(
            event_id="p1",
            event_type="checkout.session.completed",
            received_at=_fresh_ts(),
            livemode=True,
            customer_email="a@x.com",
            amount_total_usd=149.0,
            hf_offer_code="seo_audit",
            process_status="processed",
            session_mode="payment",
        )
    )
    from backend.finance.service import get_mrr_adds

    summary = get_mrr_adds(window_days=7)
    assert summary.added_count == 0
    assert summary.total_mrr_usd == 0.0


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def test_get_mrr_adds_counts_and_sums_subscriptions(tmp_path, monkeypatch):
    _isolate_log(monkeypatch, tmp_path)
    from backend.finance.models import WebhookEventRecord
    from backend.finance.webhook import append_event_record

    append_event_record(
        WebhookEventRecord(
            event_id="sub1",
            event_type="checkout.session.completed",
            received_at=_fresh_ts(),
            livemode=True,
            customer_email="a@x.com",
            amount_total_usd=300.0,
            process_status="processed",
            session_mode="subscription",
            subscription_id="sub_a",
            subscription_mrr_usd=300.0,
        )
    )
    append_event_record(
        WebhookEventRecord(
            event_id="sub2",
            event_type="checkout.session.completed",
            received_at=_fresh_ts(),
            livemode=True,
            customer_email="b@x.com",
            amount_total_usd=125.0,
            process_status="processed",
            session_mode="subscription",
            subscription_id="sub_b",
            subscription_mrr_usd=125.0,
        )
    )
    from backend.finance.service import get_mrr_adds

    summary = get_mrr_adds(window_days=7)
    assert summary.added_count == 2
    assert summary.total_mrr_usd == 425.0
    emails = {r.customer_email for r in summary.recent_adds}
    assert emails == {"a@x.com", "b@x.com"}
    # recent_adds sorted newest-first by ts (both fresh, so just verify present).
    assert all(r.mrr_usd > 0 for r in summary.recent_adds)


def test_get_mrr_adds_filters_stale_subscriptions(tmp_path, monkeypatch):
    _isolate_log(monkeypatch, tmp_path)
    from backend.finance.models import WebhookEventRecord
    from backend.finance.webhook import append_event_record

    append_event_record(
        WebhookEventRecord(
            event_id="sub_fresh",
            event_type="checkout.session.completed",
            received_at=_fresh_ts(),
            livemode=True,
            customer_email="fresh@x.com",
            amount_total_usd=300.0,
            process_status="processed",
            session_mode="subscription",
            subscription_id="sub_fresh_id",
            subscription_mrr_usd=300.0,
        )
    )
    append_event_record(
        WebhookEventRecord(
            event_id="sub_stale",
            event_type="checkout.session.completed",
            received_at=_stale_ts(30),
            livemode=True,
            customer_email="stale@x.com",
            amount_total_usd=300.0,
            process_status="processed",
            session_mode="subscription",
            subscription_id="sub_stale_id",
            subscription_mrr_usd=300.0,
        )
    )
    from backend.finance.service import get_mrr_adds

    summary = get_mrr_adds(window_days=7)
    assert summary.added_count == 1
    assert summary.total_mrr_usd == 300.0
    assert summary.recent_adds[0].customer_email == "fresh@x.com"


def test_get_mrr_adds_excludes_non_subscription_events(tmp_path, monkeypatch):
    _isolate_log(monkeypatch, tmp_path)
    from backend.finance.models import WebhookEventRecord
    from backend.finance.webhook import append_event_record

    # Mix of payment, ignored, and subscription rows — only the sub counts.
    append_event_record(
        WebhookEventRecord(
            event_id="p1",
            event_type="checkout.session.completed",
            received_at=_fresh_ts(),
            livemode=True,
            customer_email="p@x.com",
            amount_total_usd=149.0,
            process_status="processed",
            session_mode="payment",
        )
    )
    append_event_record(
        WebhookEventRecord(
            event_id="ign1",
            event_type="customer.subscription.updated",
            received_at=_fresh_ts(),
            livemode=True,
            process_status="ignored",
        )
    )
    append_event_record(
        WebhookEventRecord(
            event_id="sub_only",
            event_type="checkout.session.completed",
            received_at=_fresh_ts(),
            livemode=True,
            customer_email="s@x.com",
            amount_total_usd=300.0,
            process_status="processed",
            session_mode="subscription",
            subscription_id="sub_only_id",
            subscription_mrr_usd=300.0,
        )
    )
    from backend.finance.service import get_mrr_adds

    summary = get_mrr_adds(window_days=7)
    assert summary.added_count == 1
    assert summary.total_mrr_usd == 300.0
    assert summary.recent_adds[0].subscription_id == "sub_only_id"


def test_get_mrr_adds_excludes_zero_or_missing_mrr(tmp_path, monkeypatch):
    """A subscription_id alone isn't enough — MRR must be positive."""
    _isolate_log(monkeypatch, tmp_path)
    from backend.finance.models import WebhookEventRecord
    from backend.finance.webhook import append_event_record

    append_event_record(
        WebhookEventRecord(
            event_id="sub_zero",
            event_type="checkout.session.completed",
            received_at=_fresh_ts(),
            livemode=True,
            customer_email="z@x.com",
            process_status="processed",
            session_mode="subscription",
            subscription_id="sub_zero_id",
            subscription_mrr_usd=0.0,
        )
    )
    append_event_record(
        WebhookEventRecord(
            event_id="sub_none",
            event_type="checkout.session.completed",
            received_at=_fresh_ts(),
            livemode=True,
            customer_email="n@x.com",
            process_status="processed",
            session_mode="subscription",
            subscription_id="sub_none_id",
            subscription_mrr_usd=None,
        )
    )
    from backend.finance.service import get_mrr_adds

    summary = get_mrr_adds(window_days=7)
    assert summary.added_count == 0
    assert summary.total_mrr_usd == 0.0
