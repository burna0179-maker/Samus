"""Stripe webhook handler tests — signature verify + event processing."""

from __future__ import annotations

import hashlib
import hmac
import json
import time

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sign(payload_bytes: bytes, secret: str, *, timestamp: int | None = None) -> str:
    ts = timestamp if timestamp is not None else int(time.time())
    signed_payload = f"{ts}.".encode("utf-8") + payload_bytes
    sig = hmac.new(secret.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()
    return f"t={ts},v1={sig}"


def _checkout_session_event(
    *,
    event_id: str = "evt_test_1",
    email: str = "alice@example.com",
    offer: str = "seo_audit",
    amount_cents: int = 14900,
    website_url: str | None = None,
    payment_status: str = "paid",
    client_reference_id: str | None = None,
) -> dict:
    obj = {
        "object": "checkout.session",
        "customer_details": {"email": email},
        "amount_total": amount_cents,
        "currency": "usd",
        "payment_status": payment_status,
        "metadata": {"hf_offer_code": offer},
    }
    if client_reference_id is not None:
        obj["client_reference_id"] = client_reference_id
    # Win #2: synthesize the Stripe custom_fields shape only when the test
    # explicitly opts in, so legacy tests stay on the operator-manual path.
    if website_url is not None:
        obj["custom_fields"] = [
            {
                "key": "website_url",
                "type": "text",
                "label": {"type": "custom", "custom": "Your website URL"},
                "text": {"value": website_url},
            },
        ]
    return {
        "id": event_id,
        "type": "checkout.session.completed",
        "livemode": True,
        "created": int(time.time()),
        "data": {"object": obj},
    }


def _override_settings(
    monkeypatch,
    *,
    stripe_webhook_secret: str = "whsec_test",
    auto_fulfill_offer_codes: list[str] | None = None,
):
    """Make finance.webhook.get_settings() return our test secret."""

    class _S:
        pass

    s = _S()
    s.stripe_webhook_secret = stripe_webhook_secret
    s.auto_fulfill_offer_codes = list(auto_fulfill_offer_codes or [])
    import backend.finance.webhook as web

    monkeypatch.setattr(web, "get_settings", lambda: s)


def _isolate_log(monkeypatch, tmp_path):
    """Point the JSONL event log at a tmp path so tests don't collide."""
    monkeypatch.setenv("SAMUS_STRIPE_EVENT_LOG", str(tmp_path / "stripe_events.jsonl"))


# ---------------------------------------------------------------------------
# In-memory CustomerStore stub (mirrors backend.memory.customers.CustomerStore)
# ---------------------------------------------------------------------------


class _FakeCustomer:
    def __init__(self, id_: str, email: str, current_state: str = "prospect"):
        self.id = id_
        self.email = email
        self.name = ""
        self.company = ""
        self.source = "stripe_webhook"
        self.created_at = time.time()
        self.current_state = current_state
        self.current_state_since = time.time()
        self.metadata: dict = {}


class _FakeEvent:
    def __init__(self, from_state, to_state):
        self.event_id = "evt_x"
        self.customer_id = "cust_x"
        self.from_state = from_state
        self.to_state = to_state
        self.date = time.time()
        self.reason = ""
        self.metadata: dict = {}


class _FakeStore:
    def __init__(self):
        self.customers: dict[str, _FakeCustomer] = {}
        self.calls: list[tuple] = []
        self.fail_on_create = False
        self.fail_on_advance = False

    def get_by_email(self, email):
        self.calls.append(("get_by_email", email))
        return self.customers.get(email.lower())

    def create_customer(self, *, email, name="", company="", source="manual", metadata=None):
        self.calls.append(("create_customer", email, source))
        if self.fail_on_create:
            raise RuntimeError("simulated neo4j down")
        c = _FakeCustomer(id_=f"cust_{email.replace('@', '_')}", email=email)
        self.customers[email.lower()] = c
        return c

    def advance_state(self, *, customer_id, to_state, reason="", metadata=None):
        self.calls.append(("advance_state", customer_id, to_state))
        if self.fail_on_advance:
            raise RuntimeError("simulated neo4j down")
        for c in self.customers.values():
            if c.id == customer_id:
                prev = c.current_state
                c.current_state = to_state
                return _FakeEvent(from_state=prev, to_state=to_state)
        raise ValueError(f"unknown_id: {customer_id}")


# ---------------------------------------------------------------------------
# Signature verification
# ---------------------------------------------------------------------------


def test_verify_valid_signature_succeeds():
    from backend.finance.webhook import verify_stripe_signature

    payload = b'{"id":"evt_x"}'
    secret = "whsec_unit_test"
    header = _sign(payload, secret)
    # Should not raise.
    verify_stripe_signature(payload, header, secret)


def test_verify_rejects_mismatched_signature():
    from backend.finance.webhook import WebhookSignatureError, verify_stripe_signature

    payload = b'{"id":"evt_x"}'
    header = _sign(payload, "right_secret")
    with pytest.raises(WebhookSignatureError) as ei:
        verify_stripe_signature(payload, header, "wrong_secret")
    assert "no_matching_v1_signature" in str(ei.value)


def test_verify_rejects_expired_signature():
    from backend.finance.webhook import WebhookSignatureError, verify_stripe_signature

    payload = b'{"id":"evt_x"}'
    secret = "whsec_test"
    old_ts = int(time.time()) - 9999  # well past 5-minute tolerance
    header = _sign(payload, secret, timestamp=old_ts)
    with pytest.raises(WebhookSignatureError) as ei:
        verify_stripe_signature(payload, header, secret)
    assert "signature_expired" in str(ei.value)


def test_verify_rejects_future_dated_signature():
    # RT FIN-07: the old abs() check accepted future timestamps up to a full
    # window ahead; a far-future ts must now be rejected.
    from backend.finance.webhook import WebhookSignatureError, verify_stripe_signature

    payload = b'{"id":"evt_future"}'
    secret = "whsec_test"
    future_ts = int(time.time()) + 600  # well beyond the 5s skew allowance
    header = _sign(payload, secret, timestamp=future_ts)
    with pytest.raises(WebhookSignatureError) as ei:
        verify_stripe_signature(payload, header, secret)
    assert "signature_future" in str(ei.value)


def test_verify_rejects_empty_secret():
    from backend.finance.webhook import WebhookSignatureError, verify_stripe_signature

    with pytest.raises(WebhookSignatureError) as ei:
        verify_stripe_signature(b"{}", "t=1,v1=abc", "")
    assert "webhook_secret_unset" in str(ei.value)


def test_verify_rejects_empty_header():
    from backend.finance.webhook import WebhookSignatureError, verify_stripe_signature

    with pytest.raises(WebhookSignatureError) as ei:
        verify_stripe_signature(b"{}", "", "whsec_x")
    assert "missing_signature_header" in str(ei.value)


def test_verify_rejects_header_missing_timestamp():
    from backend.finance.webhook import WebhookSignatureError, verify_stripe_signature

    with pytest.raises(WebhookSignatureError) as ei:
        verify_stripe_signature(b"{}", "v1=abc", "whsec_x")
    assert "missing_timestamp" in str(ei.value)


def test_verify_rejects_header_missing_v1():
    from backend.finance.webhook import WebhookSignatureError, verify_stripe_signature

    with pytest.raises(WebhookSignatureError) as ei:
        verify_stripe_signature(b"{}", f"t={int(time.time())}", "whsec_x")
    assert "no_v1_signatures" in str(ei.value)


def test_verify_accepts_one_matching_among_multiple_v1():
    from backend.finance.webhook import verify_stripe_signature

    payload = b'{"id":"evt_x"}'
    secret = "whsec_test"
    ts = int(time.time())
    signed = f"{ts}.".encode("utf-8") + payload
    good = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
    header = f"t={ts},v1=deadbeef,v1={good}"
    verify_stripe_signature(payload, header, secret)


# ---------------------------------------------------------------------------
# Event log
# ---------------------------------------------------------------------------


def test_event_log_path_respects_env(tmp_path, monkeypatch):
    custom = tmp_path / "x.jsonl"
    monkeypatch.setenv("SAMUS_STRIPE_EVENT_LOG", str(custom))
    from backend.finance.webhook import event_log_path

    assert event_log_path() == custom


def test_load_seen_event_ids_empty_when_no_log(tmp_path, monkeypatch):
    monkeypatch.setenv("SAMUS_STRIPE_EVENT_LOG", str(tmp_path / "nope.jsonl"))
    from backend.finance.webhook import _load_seen_event_ids

    assert _load_seen_event_ids() == set()


def test_append_then_load_seen_event_ids(tmp_path, monkeypatch):
    _isolate_log(monkeypatch, tmp_path)
    from datetime import datetime, timezone

    from backend.finance.models import WebhookEventRecord
    from backend.finance.webhook import _load_seen_event_ids, append_event_record

    # _load_seen_event_ids only returns ids within the _SEEN_WINDOW_HOURS (96h)
    # retention window, so the timestamps must be current — a hardcoded past date
    # would rot out of the window and the roundtrip would (correctly) return {}.
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    append_event_record(
        WebhookEventRecord(
            event_id="evt_a",
            event_type="x",
            received_at=now_iso,
            livemode=False,
            process_status="processed",
        )
    )
    append_event_record(
        WebhookEventRecord(
            event_id="evt_b",
            event_type="y",
            received_at=now_iso,
            livemode=False,
            process_status="ignored",
        )
    )
    assert _load_seen_event_ids() == {"evt_a", "evt_b"}


def test_load_recent_events_filters_by_window(tmp_path, monkeypatch):
    _isolate_log(monkeypatch, tmp_path)
    from backend.finance.models import WebhookEventRecord
    from backend.finance.webhook import append_event_record, load_recent_events
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    old_ts = (now - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    new_ts = (now - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    append_event_record(
        WebhookEventRecord(
            event_id="old",
            event_type="checkout.session.completed",
            received_at=old_ts,
            livemode=False,
            process_status="processed",
        )
    )
    append_event_record(
        WebhookEventRecord(
            event_id="new",
            event_type="checkout.session.completed",
            received_at=new_ts,
            livemode=False,
            process_status="processed",
        )
    )
    recent = load_recent_events(window_days=7)
    ids = [e.event_id for e in recent]
    assert "new" in ids
    assert "old" not in ids


def test_event_ledger_rotation_drops_aged_rows(tmp_path, monkeypatch):
    """Regression: append_event_record's opportunistic rotate must actually
    drop aged rows from the active idempotency ledger.

    The rotate call passes ts_field='received_at'. When a duplicate
    rotate_by_age definition shadowed the original and rejected that kwarg, the
    call raised TypeError, which append_event_record swallows -> rotation was a
    silent no-op and the ledger grew unbounded. This asserts the aged row is
    gone after the rotate fires (so a swallowed TypeError would fail here).
    """
    _isolate_log(monkeypatch, tmp_path)
    # Rotate on every append instead of every 200th, so two writes exercise it.
    monkeypatch.setattr("backend.finance.webhook._ROTATE_EVERY_N", 1)
    from datetime import datetime, timedelta, timezone

    from backend.finance.models import WebhookEventRecord
    from backend.finance.webhook import _event_ledger, append_event_record

    aged = (datetime.now(timezone.utc) - timedelta(days=45)).strftime("%Y-%m-%dT%H:%M:%SZ")
    fresh = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    append_event_record(
        WebhookEventRecord(
            event_id="evt_aged",
            event_type="x",
            received_at=aged,
            livemode=False,
            process_status="processed",
        )
    )
    append_event_record(
        WebhookEventRecord(
            event_id="evt_fresh",
            event_type="y",
            received_at=fresh,
            livemode=False,
            process_status="processed",
        )
    )

    active_ids = {r["event_id"] for r in _event_ledger().scan()}
    assert "evt_fresh" in active_ids
    assert "evt_aged" not in active_ids  # aged row rotated out of the active log


# ---------------------------------------------------------------------------
# Handler end-to-end
# ---------------------------------------------------------------------------


def test_handle_checkout_session_completed_creates_and_advances(tmp_path, monkeypatch):
    _isolate_log(monkeypatch, tmp_path)
    _override_settings(monkeypatch, stripe_webhook_secret="whsec_test")
    store = _FakeStore()

    event = _checkout_session_event()
    body = json.dumps(event).encode("utf-8")
    header = _sign(body, "whsec_test")

    from backend.finance.webhook import handle_stripe_webhook

    result = handle_stripe_webhook(body, header, customer_store=store)

    assert result.received is True
    assert result.process_status == "processed"
    assert result.customer_email == "alice@example.com"
    assert result.customer_advanced_to == "paid"
    # Store calls: get_by_email -> create_customer -> advance_state
    kinds = [c[0] for c in store.calls]
    assert kinds == ["get_by_email", "create_customer", "advance_state"]
    advances = [c for c in store.calls if c[0] == "advance_state"]
    assert advances[0][2] == "paid"


def test_handle_existing_paid_customer_no_op_transition(tmp_path, monkeypatch):
    _isolate_log(monkeypatch, tmp_path)
    _override_settings(monkeypatch)
    store = _FakeStore()
    # Pre-existing customer already in 'paid' (or further state)
    cust = _FakeCustomer(id_="cust_bob", email="bob@example.com", current_state="paid")
    store.customers["bob@example.com"] = cust

    event = _checkout_session_event(event_id="evt_b", email="bob@example.com")
    body = json.dumps(event).encode("utf-8")
    header = _sign(body, "whsec_test")

    from backend.finance.webhook import handle_stripe_webhook

    result = handle_stripe_webhook(body, header, customer_store=store)

    assert result.process_status == "processed"
    assert result.customer_advanced_to == "paid"
    # advance_state NOT called — already past the threshold
    assert not any(c[0] == "advance_state" for c in store.calls)


def test_handle_advance_skipped_for_delivered_customer(tmp_path, monkeypatch):
    _isolate_log(monkeypatch, tmp_path)
    _override_settings(monkeypatch)
    store = _FakeStore()
    store.customers["c@example.com"] = _FakeCustomer(
        id_="cust_c",
        email="c@example.com",
        current_state="delivered",
    )
    event = _checkout_session_event(event_id="evt_c", email="c@example.com")
    body = json.dumps(event).encode("utf-8")
    header = _sign(body, "whsec_test")

    from backend.finance.webhook import handle_stripe_webhook

    result = handle_stripe_webhook(body, header, customer_store=store)

    assert result.process_status == "processed"
    # Don't roll a delivered customer back to paid.
    assert result.customer_advanced_to == "delivered"


# ---------------------------------------------------------------------------
# RT FIN-01 — state-advance gated on cleared payment_status
# ---------------------------------------------------------------------------


def test_handle_unpaid_does_not_advance_but_records_event(tmp_path, monkeypatch):
    """An 'unpaid' (ACH-pending/failed) session must NOT advance to paid, but
    the event is still recorded for visibility."""
    _isolate_log(monkeypatch, tmp_path)
    _override_settings(monkeypatch)
    store = _FakeStore()

    event = _checkout_session_event(
        event_id="evt_unpaid",
        email="ach@example.com",
        payment_status="unpaid",
    )
    body = json.dumps(event).encode("utf-8")
    header = _sign(body, "whsec_test")

    from backend.finance.webhook import handle_stripe_webhook, load_recent_events

    result = handle_stripe_webhook(body, header, customer_store=store)

    assert result.process_status == "processed"
    # No advance to 'paid' — fulfillment withheld pending payment.
    assert result.customer_advanced_to == "prospect"
    assert not any(c[0] == "advance_state" for c in store.calls)
    # The event is still persisted for visibility.
    rows = [e for e in load_recent_events(window_days=7) if e.event_id == "evt_unpaid"]
    assert len(rows) == 1
    assert rows[0].payment_status == "unpaid"


def test_handle_paid_advances_to_paid(tmp_path, monkeypatch):
    """The realistic card-checkout value ('paid') advances as before."""
    _isolate_log(monkeypatch, tmp_path)
    _override_settings(monkeypatch)
    store = _FakeStore()

    event = _checkout_session_event(
        event_id="evt_paid",
        email="card@example.com",
        payment_status="paid",
    )
    body = json.dumps(event).encode("utf-8")
    header = _sign(body, "whsec_test")

    from backend.finance.webhook import handle_stripe_webhook

    result = handle_stripe_webhook(body, header, customer_store=store)

    assert result.process_status == "processed"
    assert result.customer_advanced_to == "paid"
    advances = [c for c in store.calls if c[0] == "advance_state"]
    assert len(advances) == 1
    assert advances[0][2] == "paid"


def test_handle_no_payment_required_advances_to_paid(tmp_path, monkeypatch):
    """A $0 / 100%-coupon / free-trial session ('no_payment_required') is
    treated as cleared and advances."""
    _isolate_log(monkeypatch, tmp_path)
    _override_settings(monkeypatch)
    store = _FakeStore()

    event = _checkout_session_event(
        event_id="evt_free",
        email="free@example.com",
        payment_status="no_payment_required",
    )
    body = json.dumps(event).encode("utf-8")
    header = _sign(body, "whsec_test")

    from backend.finance.webhook import handle_stripe_webhook

    result = handle_stripe_webhook(body, header, customer_store=store)

    assert result.process_status == "processed"
    assert result.customer_advanced_to == "paid"
    advances = [c for c in store.calls if c[0] == "advance_state"]
    assert len(advances) == 1
    assert advances[0][2] == "paid"


def test_handle_idempotency_on_duplicate_event_id(tmp_path, monkeypatch):
    _isolate_log(monkeypatch, tmp_path)
    _override_settings(monkeypatch)
    store = _FakeStore()
    event = _checkout_session_event(event_id="evt_dup")
    body = json.dumps(event).encode("utf-8")
    header = _sign(body, "whsec_test")
    from backend.finance.webhook import handle_stripe_webhook

    first = handle_stripe_webhook(body, header, customer_store=store)
    assert first.process_status == "processed"
    # Second time round — Stripe retry. Should be a no-op duplicate.
    second = handle_stripe_webhook(body, header, customer_store=store)
    assert second.process_status == "duplicate"
    # No second create/advance call.
    creates = [c for c in store.calls if c[0] == "create_customer"]
    advances = [c for c in store.calls if c[0] == "advance_state"]
    assert len(creates) == 1
    assert len(advances) == 1


def test_claim_event_id_is_atomic_first_wins(tmp_path, monkeypatch):
    """L1 — claim_event_id grants the id to exactly one caller; a second claim
    of the same id loses (treated as a duplicate by the handler)."""
    _isolate_log(monkeypatch, tmp_path)
    from backend.finance.webhook import claim_event_id

    assert claim_event_id("evt_claim_1") is True  # first caller wins
    assert claim_event_id("evt_claim_1") is False  # concurrent retry loses
    # A different id is unaffected.
    assert claim_event_id("evt_claim_2") is True


def test_claim_event_id_empty_id_proceeds(tmp_path, monkeypatch):
    """An id-less event can't be keyed on — claim_event_id lets it proceed."""
    _isolate_log(monkeypatch, tmp_path)
    from backend.finance.webhook import claim_event_id

    assert claim_event_id("") is True


def test_handle_treats_preclaimed_event_as_duplicate(tmp_path, monkeypatch):
    """L1 — if the event_id is already claimed (a concurrent delivery grabbed
    it) the handler returns 'duplicate' BEFORE any customer-store side effect,
    even though the JSONL log has no record for it yet."""
    _isolate_log(monkeypatch, tmp_path)
    _override_settings(monkeypatch)
    store = _FakeStore()

    event = _checkout_session_event(event_id="evt_raced")
    body = json.dumps(event).encode("utf-8")
    header = _sign(body, "whsec_test")

    from backend.finance.webhook import claim_event_id, handle_stripe_webhook

    # Simulate the racing concurrent delivery winning the claim first.
    assert claim_event_id("evt_raced") is True

    result = handle_stripe_webhook(body, header, customer_store=store)
    assert result.process_status == "duplicate"
    assert "already claimed" in result.note
    # No customer create / advance — the side effects were never reached.
    assert store.calls == []


# ---------------------------------------------------------------------------
# PATCH-RACE-11 — reservation + terminal-marker: claim-then-crash reprocesses
# ---------------------------------------------------------------------------


def test_race11_normal_event_processes_once(tmp_path, monkeypatch):
    """(a) A normal first delivery processes exactly once and writes a terminal
    record; an immediate retry (fresh reservation, but now a terminal record
    exists) is a real duplicate. Baseline that the reservation scheme does not
    regress the happy path or genuine completed-event dedup."""
    _isolate_log(monkeypatch, tmp_path)
    _override_settings(monkeypatch)
    store = _FakeStore()
    event = _checkout_session_event(event_id="evt_race11_normal")
    body = json.dumps(event).encode("utf-8")
    header = _sign(body, "whsec_test")
    from backend.finance.webhook import handle_stripe_webhook

    first = handle_stripe_webhook(body, header, customer_store=store)
    assert first.process_status == "processed"
    advances = [c for c in store.calls if c[0] == "advance_state"]
    assert len(advances) == 1


def test_race11_completed_event_retry_is_duplicate(tmp_path, monkeypatch):
    """(b) A retry of a COMPLETED event (terminal WebhookEventRecord present) is
    rejected as a duplicate even if its reservation has somehow expired — the
    terminal marker, not the reservation age, is the completion authority."""
    _isolate_log(monkeypatch, tmp_path)
    _override_settings(monkeypatch)
    store = _FakeStore()
    event = _checkout_session_event(event_id="evt_race11_done")
    body = json.dumps(event).encode("utf-8")
    header = _sign(body, "whsec_test")
    import backend.finance.webhook as web

    # First delivery completes (writes the terminal record + the claim marker).
    first = web.handle_stripe_webhook(body, header, customer_store=store)
    assert first.process_status == "processed"
    # Force the reservation TTL to 0 so the reservation counts as "expired".
    monkeypatch.setattr(web, "_CLAIM_TTL_SECONDS", 0.0)
    # Retry: terminal record exists -> must be a duplicate, NOT reclaimed.
    second = web.handle_stripe_webhook(body, header, customer_store=store)
    assert second.process_status == "duplicate"
    # No second side effect.
    advances = [c for c in store.calls if c[0] == "advance_state"]
    assert len(advances) == 1


def test_race11_crashed_owner_reservation_is_reclaimed_and_reprocesses_once(
    tmp_path,
    monkeypatch,
):
    """(c) THE BUG: an owner that claimed the reservation but CRASHED before
    writing the terminal record (no WebhookEventRecord) must be reclaimable so
    the payment is reprocessed — not permanently dropped. A truly-completed
    event is still rejected. We simulate the crash by claiming the id directly
    (the reservation marker) and never appending a terminal record.

    Three sub-assertions encode 'reprocesses exactly once':
      1. While the reservation is FRESH, a delivery is still a duplicate (the
         live owner is presumed processing) — no premature steal.
      2. Once the reservation is EXPIRED (no terminal record), the next
         delivery reclaims it and PROCESSES the payment.
      3. A subsequent retry, now that a terminal record exists, is a duplicate.
    """
    _isolate_log(monkeypatch, tmp_path)
    _override_settings(monkeypatch)
    import backend.finance.webhook as web

    event = _checkout_session_event(event_id="evt_race11_crash")
    body = json.dumps(event).encode("utf-8")
    header = _sign(body, "whsec_test")

    # Simulate the crashed owner: it won the reservation but never completed.
    assert web.claim_event_id("evt_race11_crash") is True

    # (1) Fresh reservation -> a new delivery must NOT steal it yet.
    monkeypatch.setattr(web, "_CLAIM_TTL_SECONDS", 10_000.0)
    store_fresh = _FakeStore()
    fresh = web.handle_stripe_webhook(body, header, customer_store=store_fresh)
    assert fresh.process_status == "duplicate"
    assert store_fresh.calls == []  # no side effects on a live owner

    # (2) Reservation now expired (no terminal record) -> reclaim + reprocess.
    monkeypatch.setattr(web, "_CLAIM_TTL_SECONDS", 0.0)
    store_reclaim = _FakeStore()
    reclaimed = web.handle_stripe_webhook(body, header, customer_store=store_reclaim)
    assert reclaimed.process_status == "processed"
    advances = [c for c in store_reclaim.calls if c[0] == "advance_state"]
    assert len(advances) == 1  # processed EXACTLY once on reclaim

    # (3) Now a terminal record exists -> a further retry is a real duplicate.
    store_again = _FakeStore()
    again = web.handle_stripe_webhook(body, header, customer_store=store_again)
    assert again.process_status == "duplicate"
    assert store_again.calls == []


def test_race11_reclaim_primitives_jsonl(tmp_path, monkeypatch):
    """Unit-level: the JsonlLedger reservation primitives behave correctly —
    claim_age_seconds reports None when unclaimed and a non-negative age once
    claimed; reclaim_expired refreshes an existing marker and refuses a missing
    one."""
    from backend.common.persistence import JsonlLedger

    ledger = JsonlLedger(str(tmp_path / "evt.jsonl"))
    assert ledger.claim_age_seconds("k1") is None  # unclaimed
    assert ledger.claim("k1") is True
    age = ledger.claim_age_seconds("k1")
    assert age is not None and age >= 0.0
    # Reclaim an existing marker refreshes it (returns True).
    assert ledger.reclaim_expired("k1") is True
    # Reclaiming a key that was never claimed returns False (nothing to take).
    assert ledger.reclaim_expired("never_claimed") is False


def test_handle_unknown_event_type_returns_ignored(tmp_path, monkeypatch):
    _isolate_log(monkeypatch, tmp_path)
    _override_settings(monkeypatch)
    store = _FakeStore()
    event = {
        "id": "evt_unh",
        "type": "customer.subscription.updated",
        "livemode": True,
        "data": {"object": {}},
    }
    body = json.dumps(event).encode("utf-8")
    header = _sign(body, "whsec_test")
    from backend.finance.webhook import handle_stripe_webhook

    result = handle_stripe_webhook(body, header, customer_store=store)
    assert result.process_status == "ignored"
    # No customer-store interactions.
    assert store.calls == []


def test_handle_invoice_paid_records_renewal_and_emits_payment(tmp_path, monkeypatch):
    """A subscription renewal (invoice.paid) records a ``renewal`` event with the
    dollar amount AND emits payment.received — instead of the valueless
    ``ignored`` catch-all that made the Kerry Brown $300/mo renewals invisible."""
    _isolate_log(monkeypatch, tmp_path)
    _override_settings(monkeypatch)
    # Capture the business-event emission. The handler does a local
    # ``from backend.common.business_events import emit_business_event`` at call
    # time, so patching the source-module attribute intercepts it.
    emitted: list[tuple] = []
    import backend.common.business_events as be

    monkeypatch.setattr(be, "emit_business_event", lambda *a, **k: emitted.append((a, k)) or {})
    store = _FakeStore()
    event = {
        "id": "evt_renew1",
        "type": "invoice.paid",
        "livemode": True,
        "data": {
            "object": {
                "object": "invoice",
                "customer_email": "kerry@example.com",
                "amount_paid": 30000,  # cents -> $300.00
                "currency": "usd",
                "subscription": "sub_kerry",
                "billing_reason": "subscription_cycle",
            }
        },
    }
    body = json.dumps(event).encode("utf-8")
    header = _sign(body, "whsec_test")
    from backend.finance.webhook import handle_stripe_webhook

    result = handle_stripe_webhook(body, header, customer_store=store)
    assert result.process_status == "renewal"
    assert result.subscription_id == "sub_kerry"
    assert result.customer_email == "kerry@example.com"
    # Renewal advances no NEW customer state (the subscription already exists).
    assert store.calls == []
    # The ledger row now carries the dollar amount (previously dropped).
    lines = (tmp_path / "stripe_events.jsonl").read_text(encoding="utf-8").splitlines()
    rec = json.loads(lines[-1])
    assert rec["process_status"] == "renewal"
    assert rec["amount_total_usd"] == 300.0
    assert rec["subscription_id"] == "sub_kerry"
    # payment.received emitted once, carrying the revenue.
    assert len(emitted) == 1
    _, kw = emitted[0]
    assert kw.get("revenue_usd") == 300.0
    assert kw.get("workcell") == "finance"


def test_handle_invoice_payment_succeeded_also_renews(tmp_path, monkeypatch):
    """invoice.payment_succeeded is handled identically to invoice.paid."""
    _isolate_log(monkeypatch, tmp_path)
    _override_settings(monkeypatch)
    import backend.common.business_events as be

    monkeypatch.setattr(be, "emit_business_event", lambda *a, **k: {})
    store = _FakeStore()
    event = {
        "id": "evt_renew2",
        "type": "invoice.payment_succeeded",
        "livemode": True,
        "data": {
            "object": {
                "object": "invoice",
                "customer_email": "k@x.com",
                "amount_paid": 30000,
                "currency": "usd",
                "subscription": "sub_2",
                "billing_reason": "subscription_cycle",
            }
        },
    }
    body = json.dumps(event).encode("utf-8")
    header = _sign(body, "whsec_test")
    from backend.finance.webhook import handle_stripe_webhook

    result = handle_stripe_webhook(body, header, customer_store=store)
    assert result.process_status == "renewal"
    assert result.subscription_id == "sub_2"
    assert store.calls == []


def test_handle_checkout_session_expired_records_funnel_signal(tmp_path, monkeypatch):
    """Abandoned checkouts land as funnel_signal with full attribution (email,
    offer, client_reference_id) and NO customer-store mutation."""
    _isolate_log(monkeypatch, tmp_path)
    _override_settings(monkeypatch)
    store = _FakeStore()
    event = _checkout_session_event(
        event_id="evt_exp1",
        email="alice@example.com",
        offer="seo_audit",
        client_reference_id="out_prospect_abc",
    )
    event["type"] = "checkout.session.expired"
    # An expired session is unpaid; reflect that so the test is realistic.
    event["data"]["object"]["payment_status"] = "unpaid"
    body = json.dumps(event).encode("utf-8")
    header = _sign(body, "whsec_test")
    from backend.finance.webhook import handle_stripe_webhook

    result = handle_stripe_webhook(body, header, customer_store=store)
    assert result.process_status == "funnel_signal"
    assert result.event_type == "checkout.session.expired"
    assert result.customer_email == "alice@example.com"
    # Attribution surfaces so morning briefing can credit abandon to source
    assert result.note.startswith("checkout.session.expired")
    # No customer mutation on a funnel signal.
    assert store.calls == []


def test_handle_checkout_session_created_records_funnel_signal(tmp_path, monkeypatch):
    """Checkout starts land as funnel_signal — proves the buy link is live."""
    _isolate_log(monkeypatch, tmp_path)
    _override_settings(monkeypatch)
    store = _FakeStore()
    event = _checkout_session_event(
        event_id="evt_crd1",
        email="",
        offer="seo_audit",
        client_reference_id="out_xyz",
    )
    event["type"] = "checkout.session.created"
    # created fires before payment; payment_status starts unpaid.
    event["data"]["object"]["payment_status"] = "unpaid"
    body = json.dumps(event).encode("utf-8")
    header = _sign(body, "whsec_test")
    from backend.finance.webhook import handle_stripe_webhook

    result = handle_stripe_webhook(body, header, customer_store=store)
    assert result.process_status == "funnel_signal"
    assert result.event_type == "checkout.session.created"
    assert store.calls == []


def test_handle_missing_email_returns_failed(tmp_path, monkeypatch):
    _isolate_log(monkeypatch, tmp_path)
    _override_settings(monkeypatch)
    store = _FakeStore()
    event = _checkout_session_event(event_id="evt_noemail")
    # Wipe out customer email
    event["data"]["object"].pop("customer_details", None)
    body = json.dumps(event).encode("utf-8")
    header = _sign(body, "whsec_test")
    from backend.finance.webhook import handle_stripe_webhook

    result = handle_stripe_webhook(body, header, customer_store=store)
    assert result.process_status == "failed"
    assert "no customer_email" in result.note


def test_handle_customer_store_failure_returns_failed(tmp_path, monkeypatch):
    _isolate_log(monkeypatch, tmp_path)
    _override_settings(monkeypatch)
    store = _FakeStore()
    store.fail_on_create = True
    body = json.dumps(_checkout_session_event(event_id="evt_err")).encode("utf-8")
    header = _sign(body, "whsec_test")
    from backend.finance.webhook import handle_stripe_webhook

    result = handle_stripe_webhook(body, header, customer_store=store)
    assert result.process_status == "failed"
    assert "customer_store_failure" in result.note


def test_handle_bad_json_returns_bad_payload(tmp_path, monkeypatch):
    _isolate_log(monkeypatch, tmp_path)
    _override_settings(monkeypatch)
    body = b'{"not": json'  # malformed
    header = _sign(body, "whsec_test")
    from backend.finance.webhook import handle_stripe_webhook

    result = handle_stripe_webhook(body, header, customer_store=_FakeStore())
    assert result.process_status == "bad_payload"


def test_handle_bad_signature_raises_signature_error(tmp_path, monkeypatch):
    _isolate_log(monkeypatch, tmp_path)
    _override_settings(monkeypatch)
    body = json.dumps(_checkout_session_event()).encode("utf-8")
    bad_header = _sign(body, "different_secret")
    from backend.finance.webhook import WebhookSignatureError, handle_stripe_webhook

    with pytest.raises(WebhookSignatureError):
        handle_stripe_webhook(body, bad_header, customer_store=_FakeStore())


# ---------------------------------------------------------------------------
# get_recent_payments service helper
# ---------------------------------------------------------------------------


def test_get_recent_payments_empty_when_no_log(tmp_path, monkeypatch):
    monkeypatch.setenv("SAMUS_STRIPE_EVENT_LOG", str(tmp_path / "no.jsonl"))
    from backend.finance.service import get_recent_payments

    rs = get_recent_payments(window_days=7)
    assert rs.log_loaded is False
    assert rs.total_count == 0


def test_get_recent_payments_rolls_up_counts(tmp_path, monkeypatch):
    _isolate_log(monkeypatch, tmp_path)
    from backend.finance.models import WebhookEventRecord
    from backend.finance.webhook import append_event_record
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    fresh = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    stale = (now - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    append_event_record(
        WebhookEventRecord(
            event_id="p1",
            event_type="checkout.session.completed",
            received_at=fresh,
            livemode=True,
            customer_email="a@x.com",
            amount_total_usd=149.0,
            hf_offer_code="seo_audit",
            process_status="processed",
        )
    )
    append_event_record(
        WebhookEventRecord(
            event_id="p2",
            event_type="customer.subscription.updated",
            received_at=fresh,
            livemode=True,
            process_status="ignored",
        )
    )
    append_event_record(
        WebhookEventRecord(
            event_id="p3",
            event_type="checkout.session.completed",
            received_at=fresh,
            livemode=True,
            process_status="failed",
            process_note="no email",
        )
    )
    append_event_record(
        WebhookEventRecord(
            event_id="p_old",
            event_type="checkout.session.completed",
            received_at=stale,
            livemode=True,
            process_status="processed",
        )
    )
    from backend.finance.service import get_recent_payments

    rs = get_recent_payments(window_days=7)
    assert rs.log_loaded is True
    # 3 within window, 1 stale
    assert rs.total_count == 3
    assert rs.processed_count == 1
    assert rs.ignored_count == 1
    assert rs.failed_count == 1
    assert rs.awaiting_fulfillment_count == 1


# ---------------------------------------------------------------------------
# FastAPI endpoint
# ---------------------------------------------------------------------------


def test_endpoint_400_on_bad_signature(tmp_path, monkeypatch):
    _isolate_log(monkeypatch, tmp_path)
    _override_settings(monkeypatch)
    from fastapi.testclient import TestClient
    from backend.finance.app import app

    client = TestClient(app)
    r = client.post(
        "/stripe_webhook",
        content=json.dumps(_checkout_session_event()).encode(),
        headers={"stripe-signature": "t=1,v1=deadbeef"},
    )
    assert r.status_code == 400
    assert "signature_error" in r.text


def test_endpoint_200_on_unknown_event_type(tmp_path, monkeypatch):
    _isolate_log(monkeypatch, tmp_path)
    _override_settings(monkeypatch)
    from fastapi.testclient import TestClient
    from backend.finance.app import app

    client = TestClient(app)
    body = json.dumps(
        {"id": "evt_x", "type": "customer.created", "livemode": True, "data": {"object": {}}}
    ).encode()
    header = _sign(body, "whsec_test")
    r = client.post("/stripe_webhook", content=body, headers={"stripe-signature": header})
    assert r.status_code == 200
    assert r.json()["process_status"] == "ignored"


def test_recent_payments_endpoint(tmp_path, monkeypatch):
    monkeypatch.setenv("SAMUS_STRIPE_EVENT_LOG", str(tmp_path / "empty.jsonl"))
    from fastapi.testclient import TestClient
    from backend.finance.app import app

    client = TestClient(app)
    r = client.get("/recent_payments?window_days=14")
    assert r.status_code == 200
    body = r.json()
    assert body["window_days"] == 14
    assert body["log_loaded"] is False


# ---------------------------------------------------------------------------
# Win #2 — custom_fields URL extraction + auto-fulfill scheduling
# ---------------------------------------------------------------------------


def test_extract_session_fields_pulls_custom_field_url():
    """_extract_session_fields surfaces website_url from the custom_fields array."""
    from backend.finance.webhook import _extract_session_fields

    event = _checkout_session_event(website_url="https://acme.example.com")
    email, amount, currency, offer, status, url = _extract_session_fields(
        event["data"],
    )
    assert email == "alice@example.com"
    assert offer == "seo_audit"
    assert url == "https://acme.example.com"


def test_extract_session_fields_ignores_non_text_custom_fields():
    """Only text-type custom_fields populate website_url."""
    from backend.finance.webhook import _extract_session_fields

    event = _checkout_session_event()
    event["data"]["object"]["custom_fields"] = [
        {  # right key but wrong type
            "key": "website_url",
            "type": "dropdown",
            "dropdown": {"value": "https://nope.example.com"},
        },
        {  # right type, wrong key
            "key": "favorite_color",
            "type": "text",
            "text": {"value": "https://yes.example.com"},
        },
    ]
    _, _, _, _, _, url = _extract_session_fields(event["data"])
    assert url == ""


def test_extract_session_fields_no_custom_fields_returns_empty_url():
    from backend.finance.webhook import _extract_session_fields

    event = _checkout_session_event()  # no website_url
    _, _, _, _, _, url = _extract_session_fields(event["data"])
    assert url == ""


def test_handle_schedules_auto_fulfill_when_whitelisted_and_url_present(
    tmp_path,
    monkeypatch,
):
    _isolate_log(monkeypatch, tmp_path)
    _override_settings(
        monkeypatch,
        auto_fulfill_offer_codes=["seo_audit"],
    )
    store = _FakeStore()

    scheduled: list[dict] = []
    import backend.finance.webhook as web

    monkeypatch.setattr(
        web,
        "_schedule_auto_fulfill",
        lambda *, email, audit_url, event_id: scheduled.append(
            {"email": email, "audit_url": audit_url, "event_id": event_id},
        ),
    )

    event = _checkout_session_event(
        event_id="evt_auto",
        email="auto@example.com",
        website_url="https://auto.example.com",
    )
    body = json.dumps(event).encode("utf-8")
    header = _sign(body, "whsec_test")

    from backend.finance.webhook import handle_stripe_webhook

    result = handle_stripe_webhook(body, header, customer_store=store)

    assert result.process_status == "processed"
    assert result.customer_website_url == "https://auto.example.com"
    assert result.auto_fulfill_scheduled is True
    assert "auto_fulfill scheduled" in result.note
    assert len(scheduled) == 1
    assert scheduled[0]["audit_url"] == "https://auto.example.com"
    assert scheduled[0]["event_id"] == "evt_auto"


def test_handle_skips_auto_fulfill_when_offer_not_whitelisted(
    tmp_path,
    monkeypatch,
):
    _isolate_log(monkeypatch, tmp_path)
    _override_settings(
        monkeypatch,
        auto_fulfill_offer_codes=["different_offer"],
    )
    store = _FakeStore()
    scheduled: list = []
    import backend.finance.webhook as web

    monkeypatch.setattr(
        web,
        "_schedule_auto_fulfill",
        lambda **kw: scheduled.append(kw),
    )
    event = _checkout_session_event(
        event_id="evt_no_wl",
        website_url="https://present.example.com",
    )
    body = json.dumps(event).encode("utf-8")
    header = _sign(body, "whsec_test")

    from backend.finance.webhook import handle_stripe_webhook

    result = handle_stripe_webhook(body, header, customer_store=store)

    assert result.process_status == "processed"
    # URL is still captured (operator may need to copy it into Run-Fulfill).
    assert result.customer_website_url == "https://present.example.com"
    assert result.auto_fulfill_scheduled is False
    assert scheduled == []


def test_handle_skips_auto_fulfill_when_url_missing(tmp_path, monkeypatch):
    """Even a whitelisted offer needs the customer-supplied URL to auto-fulfill."""
    _isolate_log(monkeypatch, tmp_path)
    _override_settings(
        monkeypatch,
        auto_fulfill_offer_codes=["seo_audit"],
    )
    store = _FakeStore()
    scheduled: list = []
    import backend.finance.webhook as web

    monkeypatch.setattr(
        web,
        "_schedule_auto_fulfill",
        lambda **kw: scheduled.append(kw),
    )
    # No website_url -> no custom_fields entry.
    event = _checkout_session_event(event_id="evt_no_url")
    body = json.dumps(event).encode("utf-8")
    header = _sign(body, "whsec_test")

    from backend.finance.webhook import handle_stripe_webhook

    result = handle_stripe_webhook(body, header, customer_store=store)

    assert result.process_status == "processed"
    assert result.auto_fulfill_scheduled is False
    assert scheduled == []


def test_run_auto_fulfill_sync_appends_success_followup(tmp_path, monkeypatch):
    """Background thread target appends a 'auto_fulfill' row when fulfill ok."""
    _isolate_log(monkeypatch, tmp_path)

    class _Step:
        def __init__(self, name, status, detail=""):
            self.name = name
            self.status = status
            self.detail = detail

    class _OkResult:
        ok = True
        email_message_id = "msgid_ok_42"
        steps = [_Step("render", "ok"), _Step("send", "ok")]

    from backend.finance.webhook import (
        _run_auto_fulfill_sync,
        load_recent_events,
    )

    _run_auto_fulfill_sync(
        email="x@example.com",
        audit_url="https://x.example.com",
        event_id="evt_followup_ok",
        fulfill_fn=lambda **kw: _OkResult(),
    )

    rows = [e for e in load_recent_events(window_days=7) if e.event_id == "evt_followup_ok"]
    assert len(rows) == 1
    row = rows[0]
    assert row.event_type == "auto_fulfill"
    assert row.process_status == "processed"
    assert row.auto_fulfill_ok is True
    assert row.auto_fulfill_message_id == "msgid_ok_42"
    assert row.customer_website_url == "https://x.example.com"


def test_run_auto_fulfill_sync_appends_failure_followup(tmp_path, monkeypatch):
    _isolate_log(monkeypatch, tmp_path)

    class _Step:
        def __init__(self, name, status, detail=""):
            self.name = name
            self.status = status
            self.detail = detail

    class _FailResult:
        ok = False
        email_message_id = ""
        steps = [
            _Step("render", "ok"),
            _Step("send", "failed", "smtp 550 bounce"),
        ]

    from backend.finance.webhook import (
        _run_auto_fulfill_sync,
        load_recent_events,
    )

    _run_auto_fulfill_sync(
        email="y@example.com",
        audit_url="https://y.example.com",
        event_id="evt_followup_fail",
        fulfill_fn=lambda **kw: _FailResult(),
    )
    rows = [e for e in load_recent_events(window_days=7) if e.event_id == "evt_followup_fail"]
    assert len(rows) == 1
    row = rows[0]
    assert row.event_type == "auto_fulfill"
    assert row.process_status == "failed"
    assert row.auto_fulfill_ok is False
    assert "send: smtp 550 bounce" in row.auto_fulfill_error


def test_run_auto_fulfill_sync_handles_crash(tmp_path, monkeypatch):
    """An exception in fulfill_fn must still produce a failure follow-up row."""
    _isolate_log(monkeypatch, tmp_path)

    def _boom(**_kw):
        raise RuntimeError("boom from neo4j")

    from backend.finance.webhook import (
        _run_auto_fulfill_sync,
        load_recent_events,
    )

    _run_auto_fulfill_sync(
        email="z@example.com",
        audit_url="https://z.example.com",
        event_id="evt_followup_crash",
        fulfill_fn=_boom,
    )
    rows = [e for e in load_recent_events(window_days=7) if e.event_id == "evt_followup_crash"]
    assert len(rows) == 1
    assert rows[0].auto_fulfill_ok is False
    assert "boom from neo4j" in rows[0].auto_fulfill_error


# ---------------------------------------------------------------------------
# Phase 5 — close-the-loop CRM dispatch on successful payment
# ---------------------------------------------------------------------------


def test_handle_fires_close_the_loop_to_crm_on_success(tmp_path, monkeypatch):
    """Successful checkout.session.completed -> close-the-loop dispatch fires."""
    _isolate_log(monkeypatch, tmp_path)
    _override_settings(monkeypatch)
    store = _FakeStore()

    captured: list[dict] = []
    import backend.finance.webhook as web

    monkeypatch.setattr(
        web,
        "_dispatch_close_the_loop_to_crm",
        lambda **kw: captured.append(kw),
    )

    event = _checkout_session_event(
        event_id="evt_loop",
        email="customer@example.com",
        amount_cents=14900,
    )
    body = json.dumps(event).encode("utf-8")
    header = _sign(body, "whsec_test")

    from backend.finance.webhook import handle_stripe_webhook

    result = handle_stripe_webhook(body, header, customer_store=store)
    assert result.process_status == "processed"
    assert len(captured) == 1
    assert captured[0]["email"] == "customer@example.com"
    assert captured[0]["amount_usd"] == 149.0
    assert captured[0]["event_id"] == "evt_loop"


def test_handle_skips_close_the_loop_when_customer_store_failed(
    tmp_path,
    monkeypatch,
):
    """If the customer-store advance failed (no customer_id), don't dispatch."""
    _isolate_log(monkeypatch, tmp_path)
    _override_settings(monkeypatch)
    store = _FakeStore()
    store.fail_on_create = True

    captured: list = []
    import backend.finance.webhook as web

    monkeypatch.setattr(
        web,
        "_dispatch_close_the_loop_to_crm",
        lambda **kw: captured.append(kw),
    )

    event = _checkout_session_event(event_id="evt_no_cust")
    body = json.dumps(event).encode("utf-8")
    header = _sign(body, "whsec_test")

    from backend.finance.webhook import handle_stripe_webhook

    result = handle_stripe_webhook(body, header, customer_store=store)
    assert result.process_status == "failed"
    assert captured == []


def test_dispatch_helper_skips_when_gateway_url_unset(tmp_path, monkeypatch):
    """Helper early-returns cleanly when the gateway URL is missing.
    Producers dispatch via gateway -> samus-crm-jobs SQS queue, so without
    a GATEWAY_URL there's nowhere to enqueue."""
    _isolate_log(monkeypatch, tmp_path)

    class _S:
        gateway_urls: dict = {}
        shared_hmac_key = "secret"
        stripe_webhook_secret = "whsec_test"
        auto_fulfill_offer_codes: list = []

    import backend.finance.webhook as web

    monkeypatch.setattr(web, "get_settings", lambda: _S())

    fired: list = []
    monkeypatch.setattr(web, "signed_post_json_sync", lambda *a, **kw: fired.append((a, kw)))

    from backend.finance.webhook import _dispatch_close_the_loop_to_crm

    _dispatch_close_the_loop_to_crm(
        email="x@x.com",
        amount_usd=100.0,
        event_id="evt_drop",
    )
    assert fired == []


def test_dispatch_helper_skips_when_hmac_key_unset(tmp_path, monkeypatch):
    _isolate_log(monkeypatch, tmp_path)

    class _S:
        gateway_urls = {"gateway": "http://gateway.local"}
        shared_hmac_key = ""
        stripe_webhook_secret = "whsec_test"
        auto_fulfill_offer_codes: list = []

    import backend.finance.webhook as web

    monkeypatch.setattr(web, "get_settings", lambda: _S())

    fired: list = []
    monkeypatch.setattr(web, "signed_post_json_sync", lambda *a, **kw: fired.append((a, kw)))

    from backend.finance.webhook import _dispatch_close_the_loop_to_crm

    _dispatch_close_the_loop_to_crm(
        email="x@x.com",
        amount_usd=50.0,
        event_id="evt_drop_hmac",
    )
    assert fired == []


def test_dispatch_helper_failure_does_not_propagate(tmp_path, monkeypatch):
    """signed_post_json_sync blowing up must not bubble out — Stripe must
    still see a clean response so it doesn't trigger retry storms."""
    _isolate_log(monkeypatch, tmp_path)

    class _S:
        gateway_urls = {"gateway": "http://gateway.local"}
        shared_hmac_key = "secret"
        stripe_webhook_secret = "whsec_test"
        auto_fulfill_offer_codes: list = []

    import backend.finance.webhook as web

    monkeypatch.setattr(web, "get_settings", lambda: _S())

    def _raising(*args, **kwargs):
        raise RuntimeError("simulated gateway outage")

    monkeypatch.setattr(web, "signed_post_json_sync", _raising)

    from backend.finance.webhook import _dispatch_close_the_loop_to_crm

    _dispatch_close_the_loop_to_crm(
        email="x@x.com",
        amount_usd=99.0,
        event_id="evt_thread_boom",
    )


def test_dispatch_helper_enqueues_close_payment_to_opportunity(
    tmp_path,
    monkeypatch,
):
    """Verify the new collapsed contract: single sync POST to the gateway's
    /dispatch/crm with the close_payment_to_opportunity action — replaces
    the previous 2-call lookup-then-advance flow."""
    _isolate_log(monkeypatch, tmp_path)

    class _S:
        gateway_urls = {"gateway": "http://gateway.local"}
        shared_hmac_key = "secret"
        stripe_webhook_secret = "whsec_test"
        auto_fulfill_offer_codes: list = []

    import backend.finance.webhook as web

    monkeypatch.setattr(web, "get_settings", lambda: _S())

    captured: list[dict] = []

    def _capture(base_url, path, payload, **kwargs):
        captured.append({"base_url": base_url, "path": path, "payload": payload})

        class _R:
            status_code = 200

        return _R()

    monkeypatch.setattr(web, "signed_post_json_sync", _capture)

    from backend.finance.webhook import _dispatch_close_the_loop_to_crm

    _dispatch_close_the_loop_to_crm(
        email="buyer@x.com",
        amount_usd=1500.0,
        event_id="evt_test_001",
    )

    assert len(captured) == 1  # single enqueue, not two
    call = captured[0]
    assert call["base_url"] == "http://gateway.local"
    assert call["path"] == "/dispatch/crm"
    envelope = call["payload"]
    assert envelope["task_id"] == "evt_test_001"
    assert envelope["metadata"]["action"] == "close_payment_to_opportunity"
    assert envelope["payload"]["email"] == "buyer@x.com"
    assert envelope["payload"]["amount_usd"] == 1500.0
    assert envelope["payload"]["payment_ref"] == "evt_test_001"


# ---------------------------------------------------------------------------
# Exact-attribution close: client_reference_id=op_<id> on the buy link
# ---------------------------------------------------------------------------


def test_handle_fires_exact_opportunity_close_on_op_client_reference(
    tmp_path,
    monkeypatch,
):
    """checkout.session with client_reference_id=op_<id> -> the exact-Opportunity
    close fires; the email-based close-the-loop does NOT."""
    _isolate_log(monkeypatch, tmp_path)
    _override_settings(monkeypatch)
    store = _FakeStore()

    exact: list[dict] = []
    email_loop: list[dict] = []
    import backend.finance.webhook as web

    monkeypatch.setattr(web, "_dispatch_close_opportunity_by_id", lambda **kw: exact.append(kw))
    monkeypatch.setattr(web, "_dispatch_close_the_loop_to_crm", lambda **kw: email_loop.append(kw))

    event = _checkout_session_event(
        event_id="evt_op_ref",
        email="customer@example.com",
        amount_cents=14900,
        client_reference_id="op_kelly0001",
    )
    body = json.dumps(event).encode("utf-8")
    header = _sign(body, "whsec_test")

    from backend.finance.webhook import handle_stripe_webhook

    result = handle_stripe_webhook(body, header, customer_store=store)
    assert result.process_status == "processed"
    assert len(exact) == 1
    assert exact[0]["opportunity_id"] == "op_kelly0001"
    assert exact[0]["amount_usd"] == 149.0
    assert exact[0]["event_id"] == "evt_op_ref"
    # An op_ ref short-circuits the fuzzy email lookup entirely.
    assert email_loop == []


def test_handle_uses_email_path_when_client_reference_not_an_op_ref(
    tmp_path,
    monkeypatch,
):
    """A non-op_ client_reference_id (or none) leaves the email path in charge."""
    _isolate_log(monkeypatch, tmp_path)
    _override_settings(monkeypatch)
    store = _FakeStore()

    exact: list[dict] = []
    email_loop: list[dict] = []
    import backend.finance.webhook as web

    monkeypatch.setattr(web, "_dispatch_close_opportunity_by_id", lambda **kw: exact.append(kw))
    monkeypatch.setattr(web, "_dispatch_close_the_loop_to_crm", lambda **kw: email_loop.append(kw))

    event = _checkout_session_event(
        event_id="evt_upsell_ref",
        email="customer@example.com",
        client_reference_id="upsell_abc123",
    )
    body = json.dumps(event).encode("utf-8")
    header = _sign(body, "whsec_test")

    from backend.finance.webhook import handle_stripe_webhook

    handle_stripe_webhook(body, header, customer_store=store)
    assert exact == []
    assert len(email_loop) == 1


def test_dispatch_close_opportunity_by_id_enqueues_correct_envelope(
    tmp_path,
    monkeypatch,
):
    """_dispatch_close_opportunity_by_id enqueues close_opportunity_from_payment
    with the explicit opportunity_id."""
    _isolate_log(monkeypatch, tmp_path)

    class _S:
        gateway_urls = {"gateway": "http://gateway.local"}
        shared_hmac_key = "secret"
        stripe_webhook_secret = "whsec_test"
        auto_fulfill_offer_codes: list = []

    import backend.finance.webhook as web

    monkeypatch.setattr(web, "get_settings", lambda: _S())

    captured: list[dict] = []

    def _capture(base_url, path, payload, **kwargs):
        captured.append({"base_url": base_url, "path": path, "payload": payload})

        class _R:
            status_code = 200

        return _R()

    monkeypatch.setattr(web, "signed_post_json_sync", _capture)

    from backend.finance.webhook import _dispatch_close_opportunity_by_id

    _dispatch_close_opportunity_by_id(
        opportunity_id="op_abc123",
        amount_usd=149.0,
        event_id="evt_x1",
    )

    assert len(captured) == 1
    envelope = captured[0]["payload"]
    assert captured[0]["path"] == "/dispatch/crm"
    assert envelope["task_id"] == "evt_x1"
    assert envelope["metadata"]["action"] == "close_opportunity_from_payment"
    assert envelope["payload"]["opportunity_id"] == "op_abc123"
    assert envelope["payload"]["won_amount_usd"] == 149.0
    assert envelope["payload"]["payment_ref"] == "evt_x1"


def test_dispatch_close_opportunity_by_id_skips_when_id_empty(
    tmp_path,
    monkeypatch,
):
    """An empty opportunity_id is a clean no-op — no dispatch."""
    _isolate_log(monkeypatch, tmp_path)

    class _S:
        gateway_urls = {"gateway": "http://gateway.local"}
        shared_hmac_key = "secret"
        stripe_webhook_secret = "whsec_test"
        auto_fulfill_offer_codes: list = []

    import backend.finance.webhook as web

    monkeypatch.setattr(web, "get_settings", lambda: _S())

    fired: list = []
    monkeypatch.setattr(web, "signed_post_json_sync", lambda *a, **kw: fired.append((a, kw)))

    from backend.finance.webhook import _dispatch_close_opportunity_by_id

    _dispatch_close_opportunity_by_id(
        opportunity_id="",
        amount_usd=10.0,
        event_id="evt_empty",
    )
    assert fired == []


# ---------------------------------------------------------------------------
# Finding 4 — production live-mode gate (reject test-mode events in prod)
# ---------------------------------------------------------------------------


def _override_settings_with_env(
    monkeypatch,
    *,
    env: str,
    reject_test_mode: bool = True,
    stripe_webhook_secret: str = "whsec_test",
):
    """Settings stub carrying the env + the live-mode gate flag.

    Mirrors the production Settings.is_production property: env 'production'
    or 'prod' -> True. Used to exercise the Finding-4 webhook gate.
    """

    class _S:
        pass

    s = _S()
    s.stripe_webhook_secret = stripe_webhook_secret
    s.auto_fulfill_offer_codes = []
    s.env = env
    s.is_production = env.strip().lower() in ("production", "prod")
    s.stripe_reject_test_mode_in_production = reject_test_mode
    import backend.finance.webhook as web

    monkeypatch.setattr(web, "get_settings", lambda: s)


def test_webhook_rejects_test_mode_event_in_production(tmp_path, monkeypatch):
    """In a production env a livemode=false event is rejected — no state change."""
    _isolate_log(monkeypatch, tmp_path)
    _override_settings_with_env(monkeypatch, env="production")
    store = _FakeStore()

    event = _checkout_session_event(event_id="evt_testmode")
    event["livemode"] = False  # test-mode event
    body = json.dumps(event).encode("utf-8")
    header = _sign(body, "whsec_test")

    from backend.finance.webhook import handle_stripe_webhook

    result = handle_stripe_webhook(body, header, customer_store=store)

    assert result.process_status == "ignored"
    assert "test-mode event rejected" in result.note
    # No customer create / advance — the customer was NOT moved to 'paid'.
    assert store.calls == []


def test_webhook_processes_live_event_in_production(tmp_path, monkeypatch):
    """A genuine livemode=true event still processes normally in production."""
    _isolate_log(monkeypatch, tmp_path)
    _override_settings_with_env(monkeypatch, env="production")
    store = _FakeStore()

    event = _checkout_session_event(event_id="evt_livemode")
    event["livemode"] = True
    body = json.dumps(event).encode("utf-8")
    header = _sign(body, "whsec_test")

    from backend.finance.webhook import handle_stripe_webhook

    result = handle_stripe_webhook(body, header, customer_store=store)

    assert result.process_status == "processed"
    assert result.customer_advanced_to == "paid"


def test_webhook_processes_test_mode_event_in_non_production(tmp_path, monkeypatch):
    """In a non-production env a test-mode event is still processable.

    This is what keeps the rest of the suite's livemode=False fixtures green.
    """
    _isolate_log(monkeypatch, tmp_path)
    _override_settings_with_env(monkeypatch, env="development")
    store = _FakeStore()

    event = _checkout_session_event(event_id="evt_dev_testmode")
    event["livemode"] = False
    body = json.dumps(event).encode("utf-8")
    header = _sign(body, "whsec_test")

    from backend.finance.webhook import handle_stripe_webhook

    result = handle_stripe_webhook(body, header, customer_store=store)

    assert result.process_status == "processed"
    assert result.customer_advanced_to == "paid"


def test_webhook_gate_inert_when_flag_disabled_in_production(tmp_path, monkeypatch):
    """Operator can disable the gate; a test-mode event then processes in prod."""
    _isolate_log(monkeypatch, tmp_path)
    _override_settings_with_env(monkeypatch, env="production", reject_test_mode=False)
    store = _FakeStore()

    event = _checkout_session_event(event_id="evt_gate_off")
    event["livemode"] = False
    body = json.dumps(event).encode("utf-8")
    header = _sign(body, "whsec_test")

    from backend.finance.webhook import handle_stripe_webhook

    result = handle_stripe_webhook(body, header, customer_store=store)

    assert result.process_status == "processed"


def test_get_recent_payments_correlates_auto_fulfill_followups(
    tmp_path,
    monkeypatch,
):
    """The service helper:
    - excludes auto_fulfill rows from the operator-facing total/processed/recent
    - counts auto_fulfilled_count + auto_fulfill_failed_count separately
    - decrements awaiting_fulfillment_count when ok=True
    - keeps the awaiting count when ok=False (operator must re-run)
    """
    _isolate_log(monkeypatch, tmp_path)
    from datetime import datetime, timezone
    from backend.finance.models import WebhookEventRecord
    from backend.finance.webhook import append_event_record

    fresh = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Original payments
    for ev_id, email in [
        ("p_ok", "ok@x.com"),
        ("p_fail", "fail@x.com"),
        ("p_manual", "manual@x.com"),
    ]:
        append_event_record(
            WebhookEventRecord(
                event_id=ev_id,
                event_type="checkout.session.completed",
                received_at=fresh,
                livemode=True,
                customer_email=email,
                amount_total_usd=149.0,
                hf_offer_code="seo_audit",
                process_status="processed",
                auto_fulfill_attempted=(ev_id != "p_manual"),
            )
        )
    # Auto-fulfill follow-ups: one ok, one failed, p_manual has none
    append_event_record(
        WebhookEventRecord(
            event_id="p_ok",
            event_type="auto_fulfill",
            received_at=fresh,
            livemode=False,
            process_status="processed",
            auto_fulfill_attempted=True,
            auto_fulfill_ok=True,
            auto_fulfill_message_id="msg_ok",
        )
    )
    append_event_record(
        WebhookEventRecord(
            event_id="p_fail",
            event_type="auto_fulfill",
            received_at=fresh,
            livemode=False,
            process_status="failed",
            auto_fulfill_attempted=True,
            auto_fulfill_ok=False,
            auto_fulfill_error="send: 550 bounce",
        )
    )

    from backend.finance.service import get_recent_payments

    rs = get_recent_payments(window_days=7)
    # Only the 3 Stripe-side events are operator-visible; auto rows are internal.
    assert rs.total_count == 3
    assert rs.processed_count == 3
    assert rs.auto_fulfilled_count == 1
    assert rs.auto_fulfill_failed_count == 1
    # awaiting = processed - auto_ok = 3 - 1 = 2 (the failed one + the manual one)
    assert rs.awaiting_fulfillment_count == 2
    # recent_events list should contain only stripe events, not auto rows.
    types = {e.event_type for e in rs.recent_events}
    assert "auto_fulfill" not in types


# ---------------------------------------------------------------------------
# Outreach conversion attribution (out_<prospect_id> client_reference_id)
# ---------------------------------------------------------------------------


def test_out_ref_credits_outreach_prospect(monkeypatch, tmp_path):
    """A flyer buy-now purchase (client_reference_id=out_<prospect_id>) writes a
    conversion record crediting that exact prospect."""
    _override_settings(monkeypatch)
    _isolate_log(monkeypatch, tmp_path)
    conv = tmp_path / "outreach_conversions.jsonl"
    monkeypatch.setenv("SAMUS_OUTREACH_CONVERSIONS_LOG", str(conv))

    from backend.finance.webhook import handle_stripe_webhook

    event = _checkout_session_event(
        event_id="evt_flyer_1",
        email="buyer@acme.com",
        offer="workflow_rescue",
        amount_cents=50000,
        client_reference_id="out_pr_flyer42",
    )
    body = json.dumps(event).encode("utf-8")
    header = _sign(body, "whsec_test")
    result = handle_stripe_webhook(body, header, customer_store=_FakeStore())
    assert result.process_status == "processed"

    lines = [l for l in conv.read_text().splitlines() if l.strip()]
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["prospect_id"] == "pr_flyer42"
    assert rec["amount_usd"] == 500.0
    assert rec["event_id"] == "evt_flyer_1"
