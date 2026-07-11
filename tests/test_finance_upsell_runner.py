"""upsell_runner — drain queue, send via fake backend, transition rows."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from backend.finance.upsell_queue import (
    _read_all_rows,
    enqueue_upsell,
)
from backend.finance.upsell_runner import process_upsell_queue


@pytest.fixture(autouse=True)
def _isolate_queue(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "SAMUS_UPSELL_QUEUE_PATH",
        str(tmp_path / "upsell_queue.jsonl"),
    )
    yield tmp_path


def _now() -> datetime:
    return datetime(2026, 5, 16, 12, 0, tzinfo=timezone.utc)


def _fake_send_ok():
    sent_calls: list[dict] = []

    def _send(*, to, subject, body, html_body=None):
        sent_calls.append(
            {
                "to": to,
                "subject": subject,
                "body_len": len(body),
                "has_html": html_body is not None,
            }
        )
        return {"message_id": f"msg_{len(sent_calls)}", "channel": "fake"}

    return _send, sent_calls


def _fake_send_raises(exc: Exception):
    def _send(**kw):
        raise exc

    return _send


# ---------------------------------------------------------------------------
# Empty queue / no-ops
# ---------------------------------------------------------------------------


def test_process_returns_zero_when_no_rows():
    fake_send, _ = _fake_send_ok()
    result = process_upsell_queue(now=_now(), send_email_fn=fake_send)
    assert result.due == 0
    assert result.sent == 0
    assert result.failed == 0


def test_process_returns_zero_when_no_rows_are_due():
    """Queue has rows but all are future-dated."""
    delivered_at = _now()
    enqueue_upsell(
        customer_id="c",
        customer_email="c@x.com",
        source_offer_code="seo_audit",
        delivered_at=delivered_at,
    )
    fake_send, sent_calls = _fake_send_ok()
    # 1 day past delivery — no touch is due (earliest = D+5)
    result = process_upsell_queue(
        now=delivered_at + timedelta(days=1),
        send_email_fn=fake_send,
    )
    assert result.due == 0
    assert sent_calls == []


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_process_sends_one_email_per_due_row_and_marks_sent():
    delivered_at = _now()
    enqueue_upsell(
        customer_id="c",
        customer_email="c@x.com",
        source_offer_code="seo_audit",
        delivered_at=delivered_at,
    )
    fake_send, sent_calls = _fake_send_ok()
    # D+40 → all 3 touches due
    result = process_upsell_queue(
        now=delivered_at + timedelta(days=40),
        send_email_fn=fake_send,
    )
    assert result.due == 3
    assert result.sent == 3
    assert result.failed == 0
    assert len(sent_calls) == 3
    # Every send must include the html body
    assert all(c["has_html"] for c in sent_calls)
    # Ledger now has 3 queued + 3 sent rows
    rows = _read_all_rows()
    kinds = sorted(r.kind for r in rows)
    assert kinds == ["queued", "queued", "queued", "sent", "sent", "sent"]


def test_process_records_message_id_from_send_response():
    delivered_at = _now()
    enqueue_upsell(
        customer_id="c",
        customer_email="c@x.com",
        source_offer_code="seo_audit",
        delivered_at=delivered_at,
    )
    fake_send, _ = _fake_send_ok()
    process_upsell_queue(
        now=delivered_at + timedelta(days=40),
        send_email_fn=fake_send,
    )
    sent_rows = [r for r in _read_all_rows() if r.kind == "sent"]
    assert len(sent_rows) == 3
    # message_id format: msg_1, msg_2, msg_3
    assert {r.sent_message_id for r in sent_rows} == {"msg_1", "msg_2", "msg_3"}


# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------


def test_process_marks_failed_when_send_raises():
    delivered_at = _now()
    enqueue_upsell(
        customer_id="c",
        customer_email="c@x.com",
        source_offer_code="seo_audit",
        delivered_at=delivered_at,
    )
    fake_send = _fake_send_raises(RuntimeError("smtp 550 bounce"))
    result = process_upsell_queue(
        now=delivered_at + timedelta(days=40),
        send_email_fn=fake_send,
    )
    assert result.due == 3
    assert result.sent == 0
    assert result.failed == 3
    failed_rows = [r for r in _read_all_rows() if r.kind == "failed"]
    assert len(failed_rows) == 3
    assert all("smtp 550" in r.error for r in failed_rows)


def test_process_failed_rows_do_not_replay_on_second_pass():
    """A failed touch must NOT keep retrying every hour."""
    delivered_at = _now()
    enqueue_upsell(
        customer_id="c",
        customer_email="c@x.com",
        source_offer_code="seo_audit",
        delivered_at=delivered_at,
    )
    fake_send = _fake_send_raises(RuntimeError("flaky"))
    first = process_upsell_queue(
        now=delivered_at + timedelta(days=40),
        send_email_fn=fake_send,
    )
    assert first.failed == 3

    # Second pass with a working backend — should NOT re-attempt the failed ones
    fake_send_ok, sent_calls = _fake_send_ok()
    second = process_upsell_queue(
        now=delivered_at + timedelta(days=41),
        send_email_fn=fake_send_ok,
    )
    assert second.due == 0
    assert second.sent == 0
    assert sent_calls == []


def test_process_falls_back_to_failed_on_no_composer():
    """An unmapped source offer should mark failed without raising."""
    delivered_at = _now()
    # Manually write a queued row for an unmapped source
    from backend.finance.upsell_queue import UpsellQueueRow, _append

    _append(
        UpsellQueueRow(
            event_id="e1",
            ts="2026-05-15T00:00:00Z",
            kind="queued",
            touch_num=1,
            customer_id="c",
            customer_email="c@x.com",
            source_offer_code="unmapped_offer",
            target_offer_code="not_a_target",
            target_price_id="price_x",
            due_at="2026-05-15T00:00:00Z",
        )
    )
    fake_send, calls = _fake_send_ok()
    result = process_upsell_queue(
        now=delivered_at + timedelta(days=40),
        send_email_fn=fake_send,
    )
    assert result.due == 1
    assert result.failed == 1
    assert result.sent == 0
    assert calls == []
    failed = next(r for r in _read_all_rows() if r.kind == "failed")
    assert failed.error == "no_composer"


# ---------------------------------------------------------------------------
# Partial: some succeed, some fail
# ---------------------------------------------------------------------------


def test_process_passes_queue_event_id_to_template():
    """Cut 3: sent email body must include the row's event_id as
    ?client_reference_id=upsell_<id> so Stripe webhook can attribute."""
    delivered_at = _now()
    enqueue_upsell(
        customer_id="c",
        customer_email="c@x.com",
        source_offer_code="seo_audit",
        delivered_at=delivered_at,
    )
    # Find the queued row to grab its event_id
    from backend.finance.upsell_queue import _read_all_rows

    queued = [r for r in _read_all_rows() if r.kind == "queued" and r.touch_num == 1][0]
    expected_param = f"client_reference_id=upsell_{queued.event_id}"

    captured_bodies: list[str] = []

    def _capture_send(*, to, subject, body, html_body=None):
        captured_bodies.append(body)
        return {"message_id": "msg_x"}

    process_upsell_queue(
        now=delivered_at + timedelta(days=6),  # D+6 -> touch 1 due
        send_email_fn=_capture_send,
    )
    assert len(captured_bodies) == 1
    assert expected_param in captured_bodies[0]


def test_process_partial_pass_records_both_outcomes():
    delivered_at = _now()
    enqueue_upsell(
        customer_id="c",
        customer_email="c@x.com",
        source_offer_code="seo_audit",
        delivered_at=delivered_at,
    )

    seen_calls: list[int] = []

    def _intermittent_send(*, to, subject, body, html_body=None):
        seen_calls.append(len(seen_calls) + 1)
        # Fail the second call only
        if len(seen_calls) == 2:
            raise ConnectionError("transient")
        return {"message_id": f"msg_{len(seen_calls)}"}

    result = process_upsell_queue(
        now=delivered_at + timedelta(days=40),
        send_email_fn=_intermittent_send,
    )
    assert result.due == 3
    assert result.sent == 2
    assert result.failed == 1


# ---------------------------------------------------------------------------
# Coupon-applied checkout link in sent body
# ---------------------------------------------------------------------------


def test_runner_embeds_promotion_code_from_queue_row_in_sent_email():
    """When the queued row carries a promo code, the sent email must include
    prefilled_promotion_code in every buy-link in the body."""
    from backend.finance.upsell_queue import _CouponBundle

    def _coupon_fn(**_kw):
        return _CouponBundle(
            coupon_id="coupon_test",
            promotion_code_id="promo_test",
            promotion_code="AUDIT-CREDIT-RUNNER1",
            credit_usd_cents=14900,
        )

    delivered_at = _now()
    enqueue_upsell(
        customer_id="cust_runner_promo",
        customer_email="runner_promo@example.com",
        source_offer_code="seo_audit",
        delivered_at=delivered_at,
        create_coupon_fn=_coupon_fn,
    )

    captured: list[dict] = []

    def _capture_send(*, to, subject, body, html_body=None):
        captured.append(
            {
                "to": to,
                "subject": subject,
                "body": body,
                "html": html_body or "",
            }
        )
        return {"message_id": f"m{len(captured)}", "channel": "fake"}

    process_upsell_queue(
        now=delivered_at + timedelta(days=40),  # forces all 3 touches due
        send_email_fn=_capture_send,
    )
    assert len(captured) == 3
    for msg in captured:
        assert "prefilled_promotion_code=AUDIT-CREDIT-RUNNER1" in msg["body"], (
            f"Touch missing promo code in body: {msg['subject']!r}"
        )
        assert "prefilled_promotion_code=AUDIT-CREDIT-RUNNER1" in msg["html"], (
            f"Touch missing promo code in HTML: {msg['subject']!r}"
        )
