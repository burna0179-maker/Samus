"""get_upsell_summary — briefing rollup over the upsell_queue.jsonl."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from backend.finance.service import get_upsell_summary
from backend.finance.upsell_queue import (
    _read_all_rows,
    enqueue_upsell,
    mark_converted,
    mark_failed,
    mark_sent,
)


@pytest.fixture(autouse=True)
def _isolate_queue(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "SAMUS_UPSELL_QUEUE_PATH",
        str(tmp_path / "upsell_queue.jsonl"),
    )
    yield tmp_path


def _now() -> datetime:
    return datetime.now(timezone.utc)


def test_get_upsell_summary_empty_when_log_missing():
    s = get_upsell_summary(window_days=14)
    assert s.log_loaded is False
    assert s.queued_count == 0
    assert s.sent_count == 0


def test_get_upsell_summary_counts_queued_due_now():
    """Three touches enqueued long ago; all due → due_now_count = 3."""
    delivered = _now() - timedelta(days=40)
    enqueue_upsell(
        customer_id="c", customer_email="c@x.com",
        source_offer_code="seo_audit", delivered_at=delivered,
    )
    s = get_upsell_summary(window_days=60)
    assert s.log_loaded is True
    assert s.queued_count == 3
    assert s.due_now_count == 3
    assert s.sent_count == 0


def test_get_upsell_summary_counts_sent_failed_converted():
    delivered = _now() - timedelta(days=40)
    enqueue_upsell(
        customer_id="c", customer_email="c@x.com",
        source_offer_code="seo_audit", delivered_at=delivered,
    )
    rows = _read_all_rows()
    by_touch = {r.touch_num: r for r in rows}
    # Touch 1 → sent → converted
    mark_sent(queued_row=by_touch[1], message_id="msg_1")
    mark_converted(
        customer_id="c", source_offer_code="seo_audit",
        touch_num=1, subscription_id="sub_42",
    )
    # Touch 2 → sent
    mark_sent(queued_row=by_touch[2], message_id="msg_2")
    # Touch 3 → failed
    mark_failed(queued_row=by_touch[3], error="smtp 550 bounce")

    s = get_upsell_summary(window_days=60)
    # touch 1 latest = converted, touch 2 latest = sent, touch 3 latest = failed
    assert s.queued_count == 0
    assert s.sent_count == 1
    assert s.failed_count == 1
    assert s.converted_count == 1


def test_get_upsell_summary_skipped_dup_counted_separately():
    delivered = _now() - timedelta(days=40)
    enqueue_upsell(
        customer_id="c", customer_email="c@x.com",
        source_offer_code="seo_audit", delivered_at=delivered,
    )
    # Second enqueue → 3 skipped_dup rows; latest state per (c, seo_audit, N)
    # becomes skipped_dup
    enqueue_upsell(
        customer_id="c", customer_email="c@x.com",
        source_offer_code="seo_audit", delivered_at=delivered,
    )
    s = get_upsell_summary(window_days=60)
    assert s.skipped_dup_count == 3
    assert s.queued_count == 0  # latest state is skipped_dup, not queued


def test_get_upsell_summary_recent_sent_capped_at_5():
    """If more than 5 sent in window, recent_sent must cap to the latest 5."""
    for i in range(8):
        enqueue_upsell(
            customer_id=f"c_{i}", customer_email=f"c{i}@x.com",
            source_offer_code="seo_audit",
            delivered_at=_now() - timedelta(days=40),
        )
    # Mark all touch_1 rows as sent
    rows = _read_all_rows()
    for r in rows:
        if r.kind == "queued" and r.touch_num == 1:
            mark_sent(queued_row=r, message_id=f"msg_{r.customer_id}")
    s = get_upsell_summary(window_days=60)
    assert s.sent_count == 8
    assert len(s.recent_sent) == 5  # capped


def test_get_upsell_summary_filters_recent_sent_to_window():
    """Old sent rows show in counts only if within window."""
    # Use a fixed-time row in the past beyond the window
    from backend.finance.upsell_queue import UpsellQueueRow, _append
    stale_ts = (_now() - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _append(UpsellQueueRow(
        event_id="stale_q", ts=stale_ts, kind="queued", touch_num=1,
        customer_id="c", customer_email="c@x.com",
        source_offer_code="seo_audit", target_offer_code="seo_optimization",
        target_price_id="price_x",
        due_at=stale_ts,
    ))
    _append(UpsellQueueRow(
        event_id="stale_s", ts=stale_ts, kind="sent", touch_num=1,
        customer_id="c", customer_email="c@x.com",
        source_offer_code="seo_audit", target_offer_code="seo_optimization",
        target_price_id="price_x", sent_message_id="msg_stale",
    ))
    # 7-day window: stale sent row is OUTSIDE, so sent_count = 0
    s = get_upsell_summary(window_days=7)
    assert s.sent_count == 0
    # 60-day window: stale sent row is INSIDE, so sent_count = 1
    s = get_upsell_summary(window_days=60)
    assert s.sent_count == 1
