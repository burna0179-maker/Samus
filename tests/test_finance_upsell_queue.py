"""upsell_queue — JSONL ledger + fold-the-log state derivation + idempotency."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from backend.finance.upsell_queue import (
    DEFAULT_TOUCH_DELAYS_DAYS,
    UPSELL_TARGET_MAP,
    UpsellQueueRow,
    _current_state_by_key,
    _read_all_rows,
    due_upsells,
    enqueue_upsell,
    load_recent_rows,
    mark_converted,
    mark_failed,
    mark_sent,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolate_queue(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "SAMUS_UPSELL_QUEUE_PATH",
        str(tmp_path / "upsell_queue.jsonl"),
    )
    yield tmp_path


def _now() -> datetime:
    return datetime(2026, 5, 16, 12, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# enqueue_upsell
# ---------------------------------------------------------------------------

def test_enqueue_writes_three_queued_rows_with_correct_due_dates():
    delivered_at = _now()
    written = enqueue_upsell(
        customer_id="cust_a",
        customer_email="a@example.com",
        source_offer_code="seo_audit",
        delivered_at=delivered_at,
    )
    assert len(written) == 3
    rows = _read_all_rows()
    assert len(rows) == 3
    by_touch = {r.touch_num: r for r in rows}
    for touch_num in (1, 2, 3):
        r = by_touch[touch_num]
        assert r.kind == "queued"
        assert r.customer_email == "a@example.com"
        assert r.source_offer_code == "seo_audit"
        assert r.target_offer_code == "seo_implementation"
        assert r.target_price_id.startswith("price_")
        # due_at = delivered + N days
        expected_delay = DEFAULT_TOUCH_DELAYS_DAYS[touch_num - 1]
        expected_due = delivered_at + timedelta(days=expected_delay)
        assert r.due_at == expected_due.strftime("%Y-%m-%dT%H:%M:%SZ")


def test_enqueue_unknown_source_returns_empty_no_rows_written():
    written = enqueue_upsell(
        customer_id="cust_x",
        customer_email="x@example.com",
        source_offer_code="not_in_map",
        delivered_at=_now(),
    )
    assert written == []
    assert _read_all_rows() == []


def test_enqueue_idempotent_second_call_writes_skipped_dup_rows():
    """Running fulfill.py twice for the same customer must not double-queue."""
    delivered_at = _now()
    enqueue_upsell(
        customer_id="cust_a", customer_email="a@example.com",
        source_offer_code="seo_audit", delivered_at=delivered_at,
    )
    # Second enqueue
    enqueue_upsell(
        customer_id="cust_a", customer_email="a@example.com",
        source_offer_code="seo_audit", delivered_at=delivered_at,
    )
    rows = _read_all_rows()
    # 3 queued + 3 skipped_dup = 6 rows total
    assert len(rows) == 6
    queued = [r for r in rows if r.kind == "queued"]
    skipped = [r for r in rows if r.kind == "skipped_dup"]
    assert len(queued) == 3
    assert len(skipped) == 3


def test_enqueue_different_customers_share_no_idempotency_collision():
    enqueue_upsell(
        customer_id="cust_a", customer_email="a@example.com",
        source_offer_code="seo_audit", delivered_at=_now(),
    )
    enqueue_upsell(
        customer_id="cust_b", customer_email="b@example.com",
        source_offer_code="seo_audit", delivered_at=_now(),
    )
    rows = _read_all_rows()
    assert len(rows) == 6  # 3 + 3 fresh queues
    assert all(r.kind == "queued" for r in rows)


def test_enqueue_accepts_custom_cadence():
    written = enqueue_upsell(
        customer_id="c", customer_email="c@x.com",
        source_offer_code="seo_audit", delivered_at=_now(),
        touch_delays_days=(1, 2),  # custom: just two touches
    )
    assert len(written) == 2
    rows = _read_all_rows()
    assert {r.touch_num for r in rows} == {1, 2}


# ---------------------------------------------------------------------------
# due_upsells — fold the log
# ---------------------------------------------------------------------------

def test_due_upsells_returns_only_rows_past_due_at():
    delivered_at = _now()
    enqueue_upsell(
        customer_id="c", customer_email="c@x.com",
        source_offer_code="seo_audit", delivered_at=delivered_at,
    )
    # Today = delivered_at + 6 days -> touch 1 (D+5) is due, touch 2 (D+12) is not
    cutoff = delivered_at + timedelta(days=6)
    due = due_upsells(now=cutoff)
    assert len(due) == 1
    assert due[0].touch_num == 1


def test_due_upsells_returns_multiple_past_due_in_order():
    delivered_at = _now()
    enqueue_upsell(
        customer_id="c", customer_email="c@x.com",
        source_offer_code="seo_audit", delivered_at=delivered_at,
    )
    # Today = delivered + 40 days -> all 3 are due
    cutoff = delivered_at + timedelta(days=40)
    due = due_upsells(now=cutoff)
    assert len(due) == 3
    # earliest due first
    assert [r.touch_num for r in due] == [1, 2, 3]


def test_due_upsells_excludes_already_sent_rows():
    delivered_at = _now()
    enqueue_upsell(
        customer_id="c", customer_email="c@x.com",
        source_offer_code="seo_audit", delivered_at=delivered_at,
    )
    rows = _read_all_rows()
    queued_touch_1 = next(r for r in rows if r.touch_num == 1)
    mark_sent(queued_row=queued_touch_1, message_id="msg_x")

    # Now D+40 should only show touches 2 and 3 (1 already sent)
    due = due_upsells(now=delivered_at + timedelta(days=40))
    assert {r.touch_num for r in due} == {2, 3}


def test_due_upsells_excludes_failed_rows_so_loop_doesnt_retry():
    delivered_at = _now()
    enqueue_upsell(
        customer_id="c", customer_email="c@x.com",
        source_offer_code="seo_audit", delivered_at=delivered_at,
    )
    rows = _read_all_rows()
    queued_touch_1 = next(r for r in rows if r.touch_num == 1)
    mark_failed(queued_row=queued_touch_1, error="smtp 550")

    due = due_upsells(now=delivered_at + timedelta(days=40))
    assert {r.touch_num for r in due} == {2, 3}


# ---------------------------------------------------------------------------
# mark_sent / mark_failed / mark_converted
# ---------------------------------------------------------------------------

def test_mark_sent_appends_transition_row():
    enqueue_upsell(
        customer_id="c", customer_email="c@x.com",
        source_offer_code="seo_audit", delivered_at=_now(),
    )
    rows = _read_all_rows()
    queued_touch_2 = next(r for r in rows if r.touch_num == 2)
    mark_sent(queued_row=queued_touch_2, message_id="msg_xyz")

    rows = _read_all_rows()
    sent_rows = [r for r in rows if r.kind == "sent"]
    assert len(sent_rows) == 1
    s = sent_rows[0]
    assert s.touch_num == 2
    assert s.sent_message_id == "msg_xyz"
    assert s.customer_id == "c"


def test_mark_failed_truncates_error_to_200_chars():
    enqueue_upsell(
        customer_id="c", customer_email="c@x.com",
        source_offer_code="seo_audit", delivered_at=_now(),
    )
    rows = _read_all_rows()
    queued = next(r for r in rows if r.touch_num == 1)
    huge_error = "x" * 1000
    mark_failed(queued_row=queued, error=huge_error)

    failed_row = next(r for r in _read_all_rows() if r.kind == "failed")
    assert len(failed_row.error) == 200


def test_mark_converted_requires_prior_sent_row_for_match():
    """mark_converted is a no-op if no matching 'sent' row exists yet."""
    enqueue_upsell(
        customer_id="c", customer_email="c@x.com",
        source_offer_code="seo_audit", delivered_at=_now(),
    )
    # Convert without sending first → no row written
    mark_converted(
        customer_id="c", source_offer_code="seo_audit",
        touch_num=1, subscription_id="sub_x",
    )
    # The 3 queued rows are still the only rows
    rows = _read_all_rows()
    assert len(rows) == 3
    assert not any(r.kind == "converted" for r in rows)


def test_mark_converted_after_sent_writes_conversion_row():
    enqueue_upsell(
        customer_id="c", customer_email="c@x.com",
        source_offer_code="seo_audit", delivered_at=_now(),
    )
    rows = _read_all_rows()
    queued_touch_1 = next(r for r in rows if r.touch_num == 1)
    mark_sent(queued_row=queued_touch_1, message_id="msg_x")
    mark_converted(
        customer_id="c", source_offer_code="seo_audit",
        touch_num=1, subscription_id="sub_42",
    )
    rows = _read_all_rows()
    conv = [r for r in rows if r.kind == "converted"]
    assert len(conv) == 1
    assert conv[0].subscription_id == "sub_42"
    # subsequent due_upsells must NOT include this touch any more
    due = due_upsells(now=_now() + timedelta(days=40))
    assert {r.touch_num for r in due} == {2, 3}  # touch 1 done


# ---------------------------------------------------------------------------
# Fold-the-log helper
# ---------------------------------------------------------------------------

def test_current_state_by_key_uses_latest_row():
    rows = [
        UpsellQueueRow(
            event_id="e1", ts="2026-05-10T00:00:00Z", kind="queued",
            touch_num=1, customer_id="c", customer_email="c@x.com",
            source_offer_code="seo_audit", target_offer_code="seo_optimization",
            target_price_id="price_x", due_at="2026-05-15T00:00:00Z",
        ),
        UpsellQueueRow(
            event_id="e2", ts="2026-05-15T01:00:00Z", kind="sent",
            touch_num=1, customer_id="c", customer_email="c@x.com",
            source_offer_code="seo_audit", target_offer_code="seo_optimization",
            target_price_id="price_x", sent_message_id="msg_x",
        ),
    ]
    latest = _current_state_by_key(rows)
    key = ("c", "seo_audit", 1)
    assert latest[key].kind == "sent"


# ---------------------------------------------------------------------------
# load_recent_rows window
# ---------------------------------------------------------------------------

def test_load_recent_rows_filters_by_window(monkeypatch, tmp_path):
    # Write stale + fresh rows directly to the ledger
    path = tmp_path / "upsell_queue.jsonl"
    monkeypatch.setenv("SAMUS_UPSELL_QUEUE_PATH", str(path))
    now = datetime.now(timezone.utc)
    fresh = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    stale = (now - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in [
            {"event_id": "f", "ts": fresh, "kind": "sent", "touch_num": 1,
             "customer_id": "c", "customer_email": "c@x.com",
             "source_offer_code": "seo_audit", "target_offer_code": "seo_optimization",
             "target_price_id": "price_x", "sent_message_id": "msg_x"},
            {"event_id": "s", "ts": stale, "kind": "sent", "touch_num": 1,
             "customer_id": "c2", "customer_email": "c2@x.com",
             "source_offer_code": "seo_audit", "target_offer_code": "seo_optimization",
             "target_price_id": "price_x", "sent_message_id": "msg_old"},
        ]:
            fh.write(json.dumps(row) + "\n")
    rolled = load_recent_rows(window_days=14)
    assert {r.event_id for r in rolled} == {"f"}


def test_upsell_target_map_has_seo_audit_entry():
    """Sanity check: the wired upsell targets stay mapped (corrected funnel)."""
    assert "seo_audit" in UPSELL_TARGET_MAP
    audit_target = UPSELL_TARGET_MAP["seo_audit"]
    assert audit_target["target_offer_code"] == "seo_implementation"
    assert audit_target["target_price_id"].startswith("price_")

    assert "seo_implementation" in UPSELL_TARGET_MAP
    impl_target = UPSELL_TARGET_MAP["seo_implementation"]
    assert impl_target["target_offer_code"] == "seo_optimization"
    assert impl_target["target_price_id"].startswith("price_")


# ---------------------------------------------------------------------------
# Stripe coupon plumbing — credit application per (customer × source)
# ---------------------------------------------------------------------------

from backend.finance.upsell_queue import (
    UPSELL_CREDIT_CENTS_BY_SOURCE,
    _CouponBundle,
)


def _fake_coupon_fn(calls: list):
    """Build a fake create_coupon_fn that records each invocation."""
    def _fn(*, customer_id, customer_email, source_offer_code,
            target_offer_code, credit_cents):
        calls.append({
            "customer_id": customer_id,
            "customer_email": customer_email,
            "source": source_offer_code,
            "target": target_offer_code,
            "credit_cents": credit_cents,
        })
        return _CouponBundle(
            coupon_id=f"coupon_{len(calls):03d}",
            promotion_code_id=f"promo_{len(calls):03d}",
            promotion_code=f"AUDIT-CREDIT-FAKE{len(calls):03d}",
            credit_usd_cents=credit_cents,
        )
    return _fn


def test_enqueue_creates_one_coupon_shared_across_three_touches():
    calls: list = []
    enqueue_upsell(
        customer_id="cust_alice",
        customer_email="alice@example.com",
        source_offer_code="seo_audit",
        delivered_at=_now(),
        create_coupon_fn=_fake_coupon_fn(calls),
    )
    assert len(calls) == 1
    assert calls[0]["credit_cents"] == 14900  # $149 audit credit
    assert calls[0]["target"] == "seo_implementation"

    rows = _read_all_rows()
    assert len(rows) == 3
    coupon_ids = {r.coupon_id for r in rows}
    promo_codes = {r.promotion_code for r in rows}
    assert coupon_ids == {"coupon_001"}            # shared across touches
    assert promo_codes == {"AUDIT-CREDIT-FAKE001"}
    for r in rows:
        assert r.credit_usd_cents == 14900
        assert r.promotion_code_id == "promo_001"


def test_enqueue_reuses_prior_coupon_on_skipped_dup():
    """Re-enqueueing the same customer×source must not burn a second coupon."""
    calls: list = []
    fn = _fake_coupon_fn(calls)
    enqueue_upsell(
        customer_id="cust_bob",
        customer_email="bob@example.com",
        source_offer_code="seo_audit",
        delivered_at=_now(),
        create_coupon_fn=fn,
    )
    enqueue_upsell(
        customer_id="cust_bob",
        customer_email="bob@example.com",
        source_offer_code="seo_audit",
        delivered_at=_now(),
        create_coupon_fn=fn,
    )
    assert len(calls) == 1   # second call did NOT hit Stripe again

    rows = _read_all_rows()
    # 3 queued from first enqueue + 3 skipped_dup from second
    assert len(rows) == 6
    # All rows must carry the same coupon bundle.
    assert {r.coupon_id for r in rows} == {"coupon_001"}
    assert {r.promotion_code for r in rows} == {"AUDIT-CREDIT-FAKE001"}


def test_enqueue_different_source_gets_different_coupon():
    """audit→impl and impl→opt are separate funnel hops; each gets its own coupon."""
    calls: list = []
    fn = _fake_coupon_fn(calls)
    enqueue_upsell(
        customer_id="cust_carol", customer_email="carol@example.com",
        source_offer_code="seo_audit", delivered_at=_now(),
        create_coupon_fn=fn,
    )
    enqueue_upsell(
        customer_id="cust_carol", customer_email="carol@example.com",
        source_offer_code="seo_implementation", delivered_at=_now(),
        create_coupon_fn=fn,
    )
    assert len(calls) == 2
    assert calls[0]["credit_cents"] == 14900   # audit credit
    assert calls[1]["credit_cents"] == 20000   # implementation credit
    rows = _read_all_rows()
    by_source = {}
    for r in rows:
        by_source.setdefault(r.source_offer_code, set()).add(r.coupon_id)
    assert by_source["seo_audit"] == {"coupon_001"}
    assert by_source["seo_implementation"] == {"coupon_002"}


def test_enqueue_persists_empty_coupon_bundle_when_stripe_fails():
    """A failing coupon fn returns an empty _CouponBundle; rows still get written."""
    def _failing_coupon_fn(**_kw):
        return _CouponBundle(credit_usd_cents=14900)

    written = enqueue_upsell(
        customer_id="cust_dave", customer_email="dave@example.com",
        source_offer_code="seo_audit", delivered_at=_now(),
        create_coupon_fn=_failing_coupon_fn,
    )
    assert len(written) == 3
    rows = _read_all_rows()
    for r in rows:
        assert r.coupon_id == ""
        assert r.promotion_code == ""
        # credit_usd_cents is preserved (the credit is real even without a coupon)
        assert r.credit_usd_cents == 14900


def test_upsell_credit_map_covers_all_targeted_sources():
    """Every source in UPSELL_TARGET_MAP must have a credit amount mapped."""
    for source in UPSELL_TARGET_MAP:
        assert source in UPSELL_CREDIT_CENTS_BY_SOURCE, \
            f"{source!r} has an upsell target but no credit amount"
        assert UPSELL_CREDIT_CENTS_BY_SOURCE[source] > 0


# ---------------------------------------------------------------------------
# Quote-based hops (Workflow Rescue → Buildout; Buildout → AI Ops Build)
# Coupon creation must be SKIPPED — operator generates a custom Stripe
# invoice per-customer with the credit applied as a line-item discount.
# ---------------------------------------------------------------------------

def test_enqueue_skips_coupon_for_quote_based_target():
    """workflow_rescue → buildout is quote-based; no coupon should be created."""
    calls: list = []

    def _coupon_fn(**kw):
        calls.append(kw)
        return _CouponBundle(
            coupon_id="should_not_be_used",
            promotion_code_id="should_not_be_used",
            promotion_code="SHOULD-NOT-BE-USED",
            credit_usd_cents=kw["credit_cents"],
        )

    enqueue_upsell(
        customer_id="cust_q1",
        customer_email="q1@example.com",
        source_offer_code="service_workflow_rescue",
        delivered_at=_now(),
        create_coupon_fn=_coupon_fn,
    )
    assert calls == []  # coupon fn must NOT be invoked for quote-based

    rows = _read_all_rows()
    assert len(rows) == 3
    for r in rows:
        assert r.coupon_id == ""
        assert r.promotion_code == ""
        # But the credit amount IS recorded so the composer can describe it
        assert r.credit_usd_cents == 50000     # $500 rescue credit


def test_enqueue_skips_coupon_for_buildout_to_aiops_quote_based():
    """workflow_buildout → ai_ops_partner_build is also quote-based."""
    calls: list = []

    def _coupon_fn(**kw):
        calls.append(kw)
        return _CouponBundle(credit_usd_cents=kw["credit_cents"])

    enqueue_upsell(
        customer_id="cust_q2",
        customer_email="q2@example.com",
        source_offer_code="service_workflow_buildout",
        delivered_at=_now(),
        create_coupon_fn=_coupon_fn,
    )
    assert calls == []

    rows = _read_all_rows()
    assert len(rows) == 3
    for r in rows:
        assert r.coupon_id == ""
        assert r.credit_usd_cents == 250000     # $2,500 buildout credit
        assert r.target_offer_code == "service_ai_ops_partner_build"


def test_target_map_marks_automation_hops_as_quote_based():
    assert UPSELL_TARGET_MAP["service_workflow_rescue"]["quote_based"] is True
    assert UPSELL_TARGET_MAP["service_workflow_buildout"]["quote_based"] is True
    assert UPSELL_TARGET_MAP["seo_audit"]["quote_based"] is False
    assert UPSELL_TARGET_MAP["seo_implementation"]["quote_based"] is False
