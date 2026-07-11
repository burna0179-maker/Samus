"""Capability marketplace persistence — append-only JSONL + replay-on-startup.

Companion to :mod:`backend.strategy.capability_marketplace`. The core
:class:`CapabilityMarketplace` is a pure in-memory registry (no I/O by
design); this module is the durability layer around it.

Design
------
- Every ``publish`` / ``withdraw`` is appended as a row to a JSONL ledger
  under the writable state root. On process restart, :func:`replay_into`
  walks the ledger oldest-first and rebuilds the marketplace's live view.
- The ledger uses ``open_ledger`` (jsonl / firestore backend-portable),
  matching :mod:`backend.experiments.registry` for assignments — the
  established Samus persistence convention.
- Path override: ``SAMUS_CAPABILITY_MARKETPLACE_PATH`` env var, mirroring
  ``SAMUS_EXPERIMENTS_REGISTRY_PATH``. Default lands under
  ``state_root() / "strategy" / "capability_marketplace.jsonl"`` which is
  writable in both the host (``Samus/state/``) and container
  (``/opt/samus/data/state/``) via the existing ``state_paths`` resolver.

Row shape
---------
Two row kinds share one ledger::

    {"ts": iso, "op": "publish",  "capability_id": ..., "provider_agent": ...,
     "cost": int, "performance_score": float, "latency_ms": int,
     "tags": [str, ...]}

    {"ts": iso, "op": "withdraw", "capability_id": ..., "provider_agent": ...}

Republishing overwrites the prior in-memory row (marketplace contract);
replay honours this by applying rows in order — the final ``publish`` for
a ``(provider_agent, capability_id)`` pair wins, and a trailing
``withdraw`` removes it.

Fail-open
---------
Ledger writes are best-effort. A ``publish`` that reaches the in-memory
marketplace but fails to hit the ledger still returns; the next restart
just won't see it. Matches the existing ledger philosophy in
:mod:`backend.experiments.registry` and :mod:`backend.common.audit_ledger`.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from backend.common.dates import iso_now
from backend.common.persistence import open_ledger
from backend.common.state_paths import state_path

from backend.strategy.capability_marketplace import (
    CapabilityListing,
    CapabilityMarketplace,
)

_LOG = logging.getLogger("samus.strategy.capability_marketplace_store")

ENV_MARKETPLACE_PATH = "SAMUS_CAPABILITY_MARKETPLACE_PATH"

_DEFAULT_JSONL = ("strategy", "capability_marketplace.jsonl")
_COLLECTION = "capability_marketplace"

OP_PUBLISH = "publish"
OP_WITHDRAW = "withdraw"


def _ledger_path() -> Path:
    override = (os.getenv(ENV_MARKETPLACE_PATH) or "").strip()
    if override:
        return Path(override)
    return state_path(*_DEFAULT_JSONL)


def _ledger():
    """Open the append-only marketplace ledger (backend-portable)."""
    return open_ledger(jsonl_path=_ledger_path(), collection=_COLLECTION)


def _listing_to_row(listing: CapabilityListing) -> dict[str, Any]:
    return {
        "ts": iso_now(),
        "op": OP_PUBLISH,
        "capability_id": listing.capability_id,
        "provider_agent": listing.provider_agent,
        "cost": int(listing.cost),
        "performance_score": float(listing.performance_score),
        "latency_ms": int(listing.latency_ms),
        "tags": list(listing.tags),
    }


def _row_to_listing(row: dict[str, Any]) -> CapabilityListing | None:
    """Rebuild a listing from a persisted publish row. Returns None on shape errors."""
    try:
        return CapabilityListing(
            capability_id=str(row["capability_id"]),
            provider_agent=str(row["provider_agent"]),
            cost=int(row["cost"]),
            performance_score=float(row["performance_score"]),
            latency_ms=int(row["latency_ms"]),
            tags=tuple(str(t) for t in (row.get("tags") or ())),
        )
    except (KeyError, TypeError, ValueError) as exc:
        _LOG.warning("skipping malformed publish row: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Public API — append-on-mutate + replay-on-startup
# ---------------------------------------------------------------------------
def append_publish(listing: CapabilityListing) -> bool:
    """Append a ``publish`` row for ``listing`` to the durable ledger.

    Best-effort: returns True on success, False if the ledger write raised.
    Never propagates the exception — a full disk must not crash the caller
    (the in-memory marketplace already accepted the listing).
    """
    try:
        _ledger().append(_listing_to_row(listing))
        return True
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("capability publish ledger append failed: %s", exc)
        return False


def append_withdraw(capability_id: str, provider_agent: str) -> bool:
    """Append a ``withdraw`` row. Best-effort; see :func:`append_publish`."""
    row = {
        "ts": iso_now(),
        "op": OP_WITHDRAW,
        "capability_id": str(capability_id),
        "provider_agent": str(provider_agent),
    }
    try:
        _ledger().append(row)
        return True
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("capability withdraw ledger append failed: %s", exc)
        return False


def replay_into(marketplace: CapabilityMarketplace) -> int:
    """Rebuild ``marketplace`` state from the durable ledger, oldest-first.

    Applies rows in the order they were written: a later ``publish`` for
    the same ``(provider_agent, capability_id)`` overwrites its prior row
    (matches marketplace contract), and a later ``withdraw`` removes the
    pair. Malformed / unknown-op rows are skipped with a warning.

    Returns the number of rows applied (both publishes and withdrawals).
    """
    try:
        rows = _ledger().scan()
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("capability marketplace replay scan failed: %s", exc)
        return 0

    applied = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        op = str(row.get("op") or "").strip().lower()
        if op == OP_PUBLISH:
            listing = _row_to_listing(row)
            if listing is not None:
                marketplace.publish(listing)
                applied += 1
        elif op == OP_WITHDRAW:
            try:
                marketplace.withdraw(
                    str(row["capability_id"]),
                    str(row["provider_agent"]),
                )
                applied += 1
            except (KeyError, TypeError) as exc:
                _LOG.warning("skipping malformed withdraw row: %s", exc)
        else:
            _LOG.debug("skipping unknown marketplace op %r", op)
    return applied


def load_marketplace() -> CapabilityMarketplace:
    """Convenience — build a fresh marketplace with replayed durable state."""
    mp = CapabilityMarketplace()
    replay_into(mp)
    return mp


class PersistentCapabilityMarketplace(CapabilityMarketplace):
    """Marketplace subclass that also appends every mutation to the ledger.

    Additive-only: extends the pure in-memory marketplace by wrapping
    ``publish`` / ``withdraw`` with best-effort ledger appends. All read
    methods (``list_providers`` / ``all_capabilities`` / ``select_best``)
    inherit unchanged from the parent.

    On instantiation the ledger is replayed so the marketplace comes up
    with its last durable state already in memory.
    """

    def __init__(self, *, replay: bool = True) -> None:
        super().__init__()
        if replay:
            replay_into(self)

    def publish(self, listing: CapabilityListing) -> None:  # type: ignore[override]
        super().publish(listing)
        append_publish(listing)

    def withdraw(self, capability_id: str, provider_agent: str) -> bool:  # type: ignore[override]
        removed = super().withdraw(capability_id, provider_agent)
        if removed:
            append_withdraw(capability_id, provider_agent)
        return removed


__all__ = [
    "ENV_MARKETPLACE_PATH",
    "OP_PUBLISH",
    "OP_WITHDRAW",
    "append_publish",
    "append_withdraw",
    "replay_into",
    "load_marketplace",
    "PersistentCapabilityMarketplace",
]
