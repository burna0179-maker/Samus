"""Pluggable append-ledger backend (W-1).

Covers JsonlLedger's new scan/claim, the open_ledger factory + SAMUS_LEDGER_BACKEND
selection, and the FirestoreLedger backend exercised against an in-memory fake
Firestore client (google-cloud-firestore is not installed locally — and must
not need to be for the default jsonl path). Two integration tests confirm the
finance webhook + upsell queue route correctly through the firestore backend.
"""

from __future__ import annotations

import sys
import uuid

import pytest

from backend.common import persistence
from backend.common.firestore_ledger import FirestoreLedger


# ---------------------------------------------------------------------------
# In-memory fake Firestore client
# ---------------------------------------------------------------------------
# Mirrors the slice of google.cloud.firestore the FirestoreLedger uses:
#   client.collection(name).add(dict)
#   client.collection(name).document(id).create(dict)   -> raises AlreadyExists
#   client.collection(name).order_by(field).stream()    -> snapshots
# The exception class is named "AlreadyExists" on purpose — firestore_ledger
# ._is_already_exists() matches it structurally by class name, exactly as it
# would match google.api_core.exceptions.AlreadyExists.


class AlreadyExists(Exception):
    """Stand-in for google.api_core.exceptions.AlreadyExists."""


class _FakeSnapshot:
    def __init__(self, data: dict, *, exists: bool = True) -> None:
        self._data = data
        self.exists = exists

    def to_dict(self) -> dict:
        return dict(self._data)


class _FakeQuery:
    def __init__(self, docs: dict, order_field: str) -> None:
        self._docs = docs
        self._order = order_field

    def stream(self):
        rows = sorted(self._docs.values(), key=lambda d: d.get(self._order, 0))
        return [_FakeSnapshot(d) for d in rows]


class _FakeDocRef:
    def __init__(self, docs: dict, doc_id: str) -> None:
        self._docs = docs
        self._id = doc_id

    def create(self, data: dict) -> None:
        if self._id in self._docs:
            raise AlreadyExists(self._id)
        self._docs[self._id] = dict(data)

    def get(self) -> _FakeSnapshot:
        if self._id in self._docs:
            return _FakeSnapshot(self._docs[self._id], exists=True)
        return _FakeSnapshot({}, exists=False)

    def update(self, data: dict) -> None:
        if self._id not in self._docs:
            raise AlreadyExists(self._id)  # any non-AlreadyExists error would do
        self._docs[self._id].update(data)


class _FakeCollection:
    def __init__(self, docs: dict) -> None:
        self._docs = docs

    def add(self, data: dict) -> None:
        self._docs[uuid.uuid4().hex] = dict(data)

    def document(self, doc_id: str) -> _FakeDocRef:
        return _FakeDocRef(self._docs, doc_id)

    def order_by(self, field: str) -> _FakeQuery:
        return _FakeQuery(self._docs, field)


class FakeFirestoreClient:
    """In-memory Firestore stand-in. Collections persist for the client's life."""

    def __init__(self) -> None:
        self._collections: dict[str, dict] = {}

    def collection(self, name: str) -> _FakeCollection:
        return _FakeCollection(self._collections.setdefault(name, {}))


# ---------------------------------------------------------------------------
# JsonlLedger — new scan() / claim()
# ---------------------------------------------------------------------------


def test_jsonl_ledger_scan_returns_all_rows(tmp_path):
    ledger = persistence.JsonlLedger(tmp_path / "log.jsonl")
    for i in range(5):
        ledger.append({"n": i})
    rows = ledger.scan()
    assert [r["n"] for r in rows] == [0, 1, 2, 3, 4]


def test_jsonl_ledger_scan_empty_when_no_file(tmp_path):
    assert persistence.JsonlLedger(tmp_path / "nope.jsonl").scan() == []


def test_jsonl_ledger_scan_skips_malformed_lines(tmp_path):
    path = tmp_path / "log.jsonl"
    ledger = persistence.JsonlLedger(path)
    ledger.append({"ok": 1})
    with path.open("a", encoding="utf-8") as fh:
        fh.write("{not json\n\n")
    ledger.append({"ok": 2})
    assert [r["ok"] for r in ledger.scan()] == [1, 2]


def test_jsonl_ledger_claim_first_wins_second_loses(tmp_path):
    ledger = persistence.JsonlLedger(tmp_path / "log.jsonl")
    assert ledger.claim("evt_1") is True
    assert ledger.claim("evt_1") is False
    # A fresh instance over the same path still sees the prior claim.
    assert persistence.JsonlLedger(tmp_path / "log.jsonl").claim("evt_1") is False


def test_jsonl_ledger_claim_empty_key_always_wins(tmp_path):
    ledger = persistence.JsonlLedger(tmp_path / "log.jsonl")
    assert ledger.claim("") is True
    assert ledger.claim("") is True


def test_jsonl_ledger_claim_distinct_keys_are_independent(tmp_path):
    ledger = persistence.JsonlLedger(tmp_path / "log.jsonl")
    assert ledger.claim("a") is True
    assert ledger.claim("b") is True
    assert ledger.claim("a") is False


# ---------------------------------------------------------------------------
# open_ledger factory + SAMUS_LEDGER_BACKEND selection
# ---------------------------------------------------------------------------


def test_ledger_backend_defaults_to_jsonl(monkeypatch):
    monkeypatch.delenv("SAMUS_LEDGER_BACKEND", raising=False)
    assert persistence.ledger_backend() == "jsonl"


def test_ledger_backend_reads_env(monkeypatch):
    monkeypatch.setenv("SAMUS_LEDGER_BACKEND", "  FireStore ")
    assert persistence.ledger_backend() == "firestore"


def test_open_ledger_defaults_to_jsonl(tmp_path, monkeypatch):
    monkeypatch.delenv("SAMUS_LEDGER_BACKEND", raising=False)
    led = persistence.open_ledger(jsonl_path=tmp_path / "x.jsonl", collection="x")
    assert isinstance(led, persistence.JsonlLedger)


def test_open_ledger_firestore_returns_firestore_ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("SAMUS_LEDGER_BACKEND", "firestore")
    monkeypatch.setattr("backend.common.firestore_ledger._default_client", FakeFirestoreClient)
    led = persistence.open_ledger(jsonl_path=tmp_path / "x.jsonl", collection="x")
    assert isinstance(led, FirestoreLedger)


def test_open_ledger_unknown_backend_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("SAMUS_LEDGER_BACKEND", "redis")
    with pytest.raises(ValueError, match="unknown SAMUS_LEDGER_BACKEND"):
        persistence.open_ledger(jsonl_path=tmp_path / "x.jsonl", collection="x")


def test_open_ledger_satisfies_ledger_protocol(tmp_path, monkeypatch):
    monkeypatch.delenv("SAMUS_LEDGER_BACKEND", raising=False)
    led = persistence.open_ledger(jsonl_path=tmp_path / "x.jsonl", collection="x")
    assert isinstance(led, persistence.Ledger)


# ---------------------------------------------------------------------------
# FirestoreLedger — against the fake client
# ---------------------------------------------------------------------------


def test_firestore_ledger_append_scan_roundtrip():
    led = FirestoreLedger("stripe_events", client=FakeFirestoreClient())
    led.append({"event_id": "e1", "msg": "first"})
    led.append({"event_id": "e2", "msg": "second"})
    rows = led.scan()
    assert [r["event_id"] for r in rows] == ["e1", "e2"]
    assert [r["msg"] for r in rows] == ["first", "second"]


def test_firestore_ledger_scan_strips_internal_fields():
    led = FirestoreLedger("c", client=FakeFirestoreClient())
    led.append({"event_id": "e1"})
    [row] = led.scan()
    # The append-order timestamp the backend stamps is not visible to callers.
    assert set(row.keys()) == {"event_id"}


def test_firestore_ledger_tail_returns_last_n():
    led = FirestoreLedger("c", client=FakeFirestoreClient())
    for i in range(10):
        led.append({"n": i})
    assert [r["n"] for r in led.tail(3)] == [7, 8, 9]


def test_firestore_ledger_claim_first_wins_second_loses():
    client = FakeFirestoreClient()
    led = FirestoreLedger("stripe_events", client=client)
    assert led.claim("evt_1") is True
    assert led.claim("evt_1") is False
    # A second ledger instance over the same client sees the prior claim —
    # this is the cross-instance guarantee a file claim cannot give.
    assert FirestoreLedger("stripe_events", client=client).claim("evt_1") is False


def test_firestore_ledger_claim_empty_key_always_wins():
    led = FirestoreLedger("c", client=FakeFirestoreClient())
    assert led.claim("") is True
    assert led.claim("") is True


def test_firestore_ledger_claims_do_not_pollute_scan():
    led = FirestoreLedger("c", client=FakeFirestoreClient())
    led.append({"event_id": "e1"})
    led.claim("e1")
    # claim() writes to the sibling <collection>_claims collection.
    assert [r["event_id"] for r in led.scan()] == ["e1"]


def test_firestore_ledger_claim_fails_open_on_transient_error():
    class _BoomClient:
        def collection(self, name):
            raise RuntimeError("firestore unreachable")

    led = FirestoreLedger("c", client=_BoomClient())
    # A non-AlreadyExists error must fail OPEN — never drop a genuine event.
    assert led.claim("evt_1") is True


# --- reservation / reclaim (PATCH-RACE-11) ---------------------------------


def test_firestore_ledger_claim_age_none_when_unclaimed():
    led = FirestoreLedger("c", client=FakeFirestoreClient())
    assert led.claim_age_seconds("never_claimed") is None


def test_firestore_ledger_claim_age_empty_key_is_none():
    led = FirestoreLedger("c", client=FakeFirestoreClient())
    assert led.claim_age_seconds("") is None


def test_firestore_ledger_claim_age_is_small_right_after_claim():
    led = FirestoreLedger("c", client=FakeFirestoreClient())
    led.claim("evt_1")
    age = led.claim_age_seconds("evt_1")
    assert age is not None and 0.0 <= age < 60.0


def test_firestore_ledger_claim_age_stampless_is_infinite():
    """A claim document with no claimed_at stamp is treated as reclaimable."""
    client = FakeFirestoreClient()
    led = FirestoreLedger("c", client=client)
    # Write a stampless claim doc directly into the sibling claims collection.
    from backend.common.firestore_ledger import _claim_doc_id

    client.collection("c_claims").document(_claim_doc_id("evt_1")).create({"key": "evt_1"})
    assert led.claim_age_seconds("evt_1") == float("inf")


def test_firestore_ledger_reclaim_missing_is_false():
    led = FirestoreLedger("c", client=FakeFirestoreClient())
    assert led.reclaim_expired("never_claimed") is False


def test_firestore_ledger_reclaim_refreshes_existing_claim():
    client = FakeFirestoreClient()
    led = FirestoreLedger("c", client=client)
    led.claim("evt_1")
    assert led.reclaim_expired("evt_1") is True
    # Still claimed (a subsequent claim() loses) and age reset to ~now.
    assert led.claim("evt_1") is False
    age = led.claim_age_seconds("evt_1")
    assert age is not None and age < 60.0


def test_firestore_ledger_default_client_errors_without_package(monkeypatch):
    # The lazy import path: with the package absent, construction fails loud
    # rather than silently — the jsonl default is the documented fix. The
    # package may legitimately be installed (requirements.txt ships it for the
    # Cloud Run posture), so absence is simulated: a None entry in sys.modules
    # makes ``from google.cloud import firestore`` raise ImportError.
    monkeypatch.delenv("SAMUS_LEDGER_BACKEND", raising=False)
    monkeypatch.setitem(sys.modules, "google.cloud.firestore", None)
    with pytest.raises(RuntimeError, match="google-cloud-firestore"):
        FirestoreLedger("c")


# ---------------------------------------------------------------------------
# Integration — finance webhook + upsell queue on the firestore backend
# ---------------------------------------------------------------------------


def test_webhook_idempotency_routes_through_firestore(monkeypatch):
    """The finance webhook's ledger calls work end-to-end on the firestore
    backend: append is visible to a later scan, and claim dedups across the
    separate FirestoreLedger instances each helper call constructs."""
    from backend.common.dates import iso_now
    from backend.finance import webhook
    from backend.finance.models import WebhookEventRecord

    shared = FakeFirestoreClient()
    monkeypatch.setenv("SAMUS_LEDGER_BACKEND", "firestore")
    monkeypatch.setattr("backend.common.firestore_ledger._default_client", lambda: shared)

    rec = WebhookEventRecord(
        event_id="evt_fs_1",
        event_type="checkout.session.completed",
        received_at=iso_now(),
        livemode=True,
        process_status="processed",
    )
    webhook.append_event_record(rec)
    assert "evt_fs_1" in webhook._load_seen_event_ids()

    # The atomic claim: first delivery wins, a concurrent retry loses.
    assert webhook.claim_event_id("evt_fs_2") is True
    assert webhook.claim_event_id("evt_fs_2") is False


def test_upsell_queue_routes_through_firestore(monkeypatch):
    from backend.finance import upsell_queue

    shared = FakeFirestoreClient()
    monkeypatch.setenv("SAMUS_LEDGER_BACKEND", "firestore")
    monkeypatch.setattr("backend.common.firestore_ledger._default_client", lambda: shared)

    # service_workflow_rescue is a quote-based hop — no Stripe coupon call.
    written = upsell_queue.enqueue_upsell(
        customer_id="cust_1",
        customer_email="owner@example.com",
        source_offer_code="service_workflow_rescue",
    )
    assert len(written) == 3  # one row per touch

    rows = upsell_queue._read_all_rows()
    assert len(rows) == 3
    assert {r.kind for r in rows} == {"queued"}
    assert {r.touch_num for r in rows} == {1, 2, 3}
