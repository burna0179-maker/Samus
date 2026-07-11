"""Tests for `backend.strategy.credit_ledger`.

Covers the append-only earn/spend ledger, thread-safety, the
"correction via new EARN" pattern, and the JSON-serialisable snapshot.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime

import pytest

from backend.strategy.credit_ledger import (
    AgentBalance,
    CreditLedger,
    CreditTransaction,
    InsufficientCreditsError,
    InvalidAmountError,
    TxnType,
)


# ---------------------------------------------------------------------------
# Happy-path basics
# ---------------------------------------------------------------------------
def test_earn_happy_path_records_transaction_and_balance() -> None:
    ledger = CreditLedger()
    txn = ledger.earn("agent_a", 100, "report_generation")

    assert isinstance(txn, CreditTransaction)
    assert txn.agent_id == "agent_a"
    assert txn.amount == 100
    assert txn.txn_type is TxnType.EARN
    assert txn.category == "report_generation"
    assert ledger.balance("agent_a").credits == 100


def test_spend_happy_path_records_transaction_and_decrements_balance() -> None:
    ledger = CreditLedger()
    ledger.earn("agent_a", 100, "report_generation")
    txn = ledger.spend("agent_a", 40, "compute_resources")

    assert txn.txn_type is TxnType.SPEND
    assert txn.amount == 40
    bal = ledger.balance("agent_a")
    assert bal.credits == 60
    assert bal.earned_categories == {"report_generation": 100}
    assert bal.spent_categories == {"compute_resources": 40}


# ---------------------------------------------------------------------------
# Validation + error semantics
# ---------------------------------------------------------------------------
def test_spend_more_than_balance_raises_and_does_not_mutate() -> None:
    ledger = CreditLedger()
    ledger.earn("agent_a", 50, "report_generation")

    with pytest.raises(InsufficientCreditsError):
        ledger.spend("agent_a", 75, "compute_resources")

    # Ledger must not be mutated on failure.
    assert ledger.balance("agent_a").credits == 50
    assert len(ledger.transactions("agent_a")) == 1  # only the earn
    assert ledger.balance("agent_a").spent_categories == {}


def test_spend_exactly_equal_to_balance_zeroes_out() -> None:
    ledger = CreditLedger()
    ledger.earn("agent_a", 250, "report_generation")
    ledger.spend("agent_a", 250, "compute_resources")
    assert ledger.balance("agent_a").credits == 0


def test_earn_with_zero_amount_raises_invalid_amount() -> None:
    ledger = CreditLedger()
    with pytest.raises(InvalidAmountError):
        ledger.earn("agent_a", 0, "report_generation")


def test_earn_with_negative_amount_raises_invalid_amount() -> None:
    ledger = CreditLedger()
    with pytest.raises(InvalidAmountError):
        ledger.earn("agent_a", -5, "report_generation")


def test_spend_with_zero_amount_raises_invalid_amount() -> None:
    ledger = CreditLedger()
    ledger.earn("agent_a", 100, "report_generation")
    with pytest.raises(InvalidAmountError):
        ledger.spend("agent_a", 0, "compute_resources")


# ---------------------------------------------------------------------------
# Read-side surface
# ---------------------------------------------------------------------------
def test_balance_for_unknown_agent_returns_zero_balance() -> None:
    ledger = CreditLedger()
    bal = ledger.balance("never_seen")

    assert isinstance(bal, AgentBalance)
    assert bal.agent_id == "never_seen"
    assert bal.credits == 0
    assert bal.earned_categories == {}
    assert bal.spent_categories == {}


def test_balance_tracks_multiple_categories_across_earns_and_spends() -> None:
    ledger = CreditLedger()
    ledger.earn("agent_a", 100, "report_generation")
    ledger.earn("agent_a", 200, "data_analysis")
    ledger.earn("agent_a", 50, "report_generation")
    ledger.spend("agent_a", 80, "compute_resources")
    ledger.spend("agent_a", 30, "training_data")
    ledger.spend("agent_a", 20, "compute_resources")

    bal = ledger.balance("agent_a")
    assert bal.credits == 100 + 200 + 50 - 80 - 30 - 20
    assert bal.earned_categories == {"report_generation": 150, "data_analysis": 200}
    assert bal.spent_categories == {"compute_resources": 100, "training_data": 30}


def test_transactions_returns_chronological_list() -> None:
    ledger = CreditLedger()
    a = ledger.earn("agent_a", 10, "x")
    b = ledger.earn("agent_b", 20, "y")
    c = ledger.spend("agent_a", 5, "z")

    all_txns = ledger.transactions()
    assert [t.txn_id for t in all_txns] == [a.txn_id, b.txn_id, c.txn_id]


def test_transactions_filtered_by_agent_id() -> None:
    ledger = CreditLedger()
    ledger.earn("agent_a", 10, "x")
    ledger.earn("agent_b", 20, "y")
    ledger.spend("agent_a", 5, "z")

    a_only = ledger.transactions(agent_id="agent_a")
    assert {t.agent_id for t in a_only} == {"agent_a"}
    assert len(a_only) == 2


def test_transactions_returns_fresh_list_caller_cannot_corrupt_ledger() -> None:
    ledger = CreditLedger()
    ledger.earn("agent_a", 10, "x")
    snapshot = ledger.transactions()
    snapshot.clear()
    # Ledger itself still has the txn.
    assert len(ledger.transactions()) == 1


# ---------------------------------------------------------------------------
# Append-only / correction pattern
# ---------------------------------------------------------------------------
def test_correction_pattern_via_earn_with_note() -> None:
    ledger = CreditLedger()
    bad = ledger.earn("agent_a", 100, "report_generation")
    # No revoke API — correct by adding an offsetting EARN with a note.
    fix = ledger.earn(
        "agent_a", 25, "report_generation", note=f"correction:{bad.txn_id}"
    )

    notes = {t.note for t in ledger.transactions("agent_a")}
    assert f"correction:{bad.txn_id}" in notes
    assert fix.note.startswith("correction:")
    assert ledger.balance("agent_a").credits == 125
    # The bad txn still exists — that's the whole point of append-only.
    assert any(t.txn_id == bad.txn_id for t in ledger.transactions("agent_a"))


# ---------------------------------------------------------------------------
# Snapshot serialisation
# ---------------------------------------------------------------------------
def test_to_dict_snapshot_is_json_serialisable() -> None:
    ledger = CreditLedger()
    ledger.earn("agent_a", 100, "report_generation")
    ledger.spend("agent_a", 40, "compute_resources", note="hello")
    snap = ledger.to_dict()

    # Round-trip through json.dumps must not raise.
    blob = json.dumps(snap)
    restored = json.loads(blob)
    assert restored["balances"] == {"agent_a": 60}
    assert restored["earned_categories"] == {"agent_a": {"report_generation": 100}}
    assert restored["spent_categories"] == {"agent_a": {"compute_resources": 40}}
    assert len(restored["transactions"]) == 2
    assert restored["transactions"][1]["note"] == "hello"


# ---------------------------------------------------------------------------
# Concurrency + uniqueness
# ---------------------------------------------------------------------------
def test_thread_safe_concurrent_earns_yield_correct_balance() -> None:
    ledger = CreditLedger()
    threads: list[threading.Thread] = []

    def worker() -> None:
        for _ in range(10):
            ledger.earn("agent_a", 1, "report_generation")

    for _ in range(10):
        t = threading.Thread(target=worker)
        threads.append(t)
        t.start()
    for t in threads:
        t.join()

    # 10 threads * 10 earns * 1 credit = 100
    assert ledger.balance("agent_a").credits == 100
    assert len(ledger.transactions("agent_a")) == 100


def test_txn_ids_are_unique_across_many_transactions() -> None:
    ledger = CreditLedger()
    for _ in range(1000):
        ledger.earn("agent_a", 1, "report_generation")
    ids = {t.txn_id for t in ledger.transactions("agent_a")}
    assert len(ids) == 1000


def test_timestamp_is_parseable_iso8601() -> None:
    ledger = CreditLedger()
    txn = ledger.earn("agent_a", 5, "report_generation")
    # If this parses without ValueError, the timestamp is valid ISO8601.
    parsed = datetime.fromisoformat(txn.timestamp)
    assert parsed.tzinfo is not None  # UTC offset captured
