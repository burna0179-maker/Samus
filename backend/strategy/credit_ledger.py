"""Agent credit ledger — append-only earn/spend with category attribution.

Source: `Samus/recovery/agent_civilization_blueprint.md`, "Agent economy" section
(chat 44 / paper-derived production architecture).

This module is pure substrate: it tracks who earned/spent what for which
category. It does NOT enforce economic-incentive policy, does NOT decide
allocation, and does NOT autocreate agents. Downstream strategy modules
consume the ledger surface and layer behaviour on top.

Design notes
------------
* Append-only — there is no `revoke_transaction`. Corrections are recorded
  as a fresh EARN with ``note="correction:<old_txn_id>"`` so the history
  remains auditable.
* Thread-safe — every mutation takes ``self._lock`` so concurrent earn /
  spend calls from worker threads don't race.
* In-memory only — `to_dict()` produces a JSON-serialisable snapshot the
  caller can persist however it wants (or feed back into a future
  reconstitution constructor — out of scope here).
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class TxnType(str, Enum):
    """Direction of a credit movement."""

    EARN = "earn"
    SPEND = "spend"


@dataclass(frozen=True)
class CreditTransaction:
    """A single immutable ledger entry.

    `amount` is always positive; the sign is implied by `txn_type`.
    """

    agent_id: str
    txn_type: TxnType
    amount: int
    category: str
    timestamp: str  # ISO8601 UTC
    txn_id: str  # uuid4 hex
    note: str = ""


@dataclass
class AgentBalance:
    """Aggregated snapshot of one agent's position in the ledger."""

    agent_id: str
    credits: int  # signed running balance
    earned_categories: dict[str, int] = field(default_factory=dict)
    spent_categories: dict[str, int] = field(default_factory=dict)


class InsufficientCreditsError(Exception):
    """Raised when `spend` is called for more than the current balance."""


class InvalidAmountError(ValueError):
    """Raised when `amount` is not strictly positive."""


def _utcnow_iso() -> str:
    """Wall-clock UTC ISO8601 string (with offset, e.g. ``+00:00``)."""
    return datetime.now(timezone.utc).isoformat()


class CreditLedger:
    """In-memory append-only ledger. Thread-safe via internal lock."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._transactions: list[CreditTransaction] = []
        # Per-agent running totals — kept in sync inside the lock so we
        # don't have to re-fold the whole transaction list for every
        # balance() lookup.
        self._balances: dict[str, int] = {}
        self._earned: dict[str, dict[str, int]] = {}
        self._spent: dict[str, dict[str, int]] = {}

    # ------------------------------------------------------------------
    # Mutation API
    # ------------------------------------------------------------------
    def earn(
        self,
        agent_id: str,
        amount: int,
        category: str,
        note: str = "",
    ) -> CreditTransaction:
        """Credit ``amount`` to ``agent_id`` under ``category``."""
        self._validate_amount(amount)
        with self._lock:
            txn = self._build_txn(agent_id, TxnType.EARN, amount, category, note)
            self._transactions.append(txn)
            self._balances[agent_id] = self._balances.get(agent_id, 0) + amount
            agent_earned = self._earned.setdefault(agent_id, {})
            agent_earned[category] = agent_earned.get(category, 0) + amount
            return txn

    def spend(
        self,
        agent_id: str,
        amount: int,
        category: str,
        note: str = "",
    ) -> CreditTransaction:
        """Debit ``amount`` from ``agent_id``.

        Raises
        ------
        InsufficientCreditsError
            If the current balance is below ``amount``. The ledger is
            NOT mutated when this happens.
        """
        self._validate_amount(amount)
        with self._lock:
            current = self._balances.get(agent_id, 0)
            if current < amount:
                raise InsufficientCreditsError(
                    f"agent_id={agent_id!r} balance={current} < spend amount={amount}"
                )
            txn = self._build_txn(agent_id, TxnType.SPEND, amount, category, note)
            self._transactions.append(txn)
            self._balances[agent_id] = current - amount
            agent_spent = self._spent.setdefault(agent_id, {})
            agent_spent[category] = agent_spent.get(category, 0) + amount
            return txn

    # ------------------------------------------------------------------
    # Read API
    # ------------------------------------------------------------------
    def balance(self, agent_id: str) -> AgentBalance:
        """Snapshot of one agent's position.

        Unknown agents return a zeroed AgentBalance (no KeyError).
        """
        with self._lock:
            return AgentBalance(
                agent_id=agent_id,
                credits=self._balances.get(agent_id, 0),
                earned_categories=dict(self._earned.get(agent_id, {})),
                spent_categories=dict(self._spent.get(agent_id, {})),
            )

    def transactions(
        self,
        agent_id: str | None = None,
    ) -> list[CreditTransaction]:
        """All transactions in chronological order (insertion order).

        When ``agent_id`` is given, results are filtered to that agent.
        Returns a fresh list each call so the caller can mutate it
        freely without disturbing the ledger.
        """
        with self._lock:
            if agent_id is None:
                return list(self._transactions)
            return [t for t in self._transactions if t.agent_id == agent_id]

    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisable snapshot of the ledger."""
        with self._lock:
            return {
                "transactions": [
                    {
                        "agent_id": t.agent_id,
                        "txn_type": t.txn_type.value,
                        "amount": t.amount,
                        "category": t.category,
                        "timestamp": t.timestamp,
                        "txn_id": t.txn_id,
                        "note": t.note,
                    }
                    for t in self._transactions
                ],
                "balances": dict(self._balances),
                "earned_categories": {agent: dict(cats) for agent, cats in self._earned.items()},
                "spent_categories": {agent: dict(cats) for agent, cats in self._spent.items()},
            }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _validate_amount(amount: int) -> None:
        if not isinstance(amount, int) or isinstance(amount, bool):
            raise InvalidAmountError(
                f"amount must be a positive int, got {type(amount).__name__}={amount!r}"
            )
        if amount <= 0:
            raise InvalidAmountError(f"amount must be strictly positive, got {amount}")

    @staticmethod
    def _build_txn(
        agent_id: str,
        txn_type: TxnType,
        amount: int,
        category: str,
        note: str,
    ) -> CreditTransaction:
        return CreditTransaction(
            agent_id=agent_id,
            txn_type=txn_type,
            amount=amount,
            category=category,
            timestamp=_utcnow_iso(),
            txn_id=uuid.uuid4().hex,
            note=note,
        )
