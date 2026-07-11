"""Outreach call-outcome metrics — objection / close / failure counts and
angle win-rate.

v2 (2026-06-01): now a thin delegator over the shared, PERSISTENT
:mod:`backend.common.feedback_store`. This module and ``crm.feedback_engine``
were byte-identical in-memory twins written by different workcells, so outcomes
split across two divergent process-local dicts and were lost on restart. Both
now delegate to the single store, so CRM- and outreach-logged outcomes
accumulate together and persist. Public API is unchanged.

``reset_metrics()`` is the test-isolation hook.

v3 (Tranche 3, learning loop): every ``log_interaction`` is ALSO appended to
an append-only JSONL ledger (``open_ledger``-portable), so the raw
interaction history survives independently of the folded counter snapshot
and ``rebuild_from_ledger()`` can reconstruct the aggregate store from the
event trail. Ledger writes are fail-soft — the counter path stays primary.
"""
from __future__ import annotations

import logging
from typing import Any

from backend.common import feedback_store as _store
from backend.common.dates import iso_now
from backend.common.persistence import open_ledger
from backend.common.state_paths import state_path

_LOG = logging.getLogger("samus.outreach.metrics")

_LEDGER_JSONL = ("outreach", "interaction_ledger.jsonl")
_LEDGER_COLLECTION = "outreach_interactions"

# Rebuild scans at most this many recent rows (ordered oldest-first).
_REBUILD_TAIL = 100_000


def _ledger():
    return open_ledger(
        jsonl_path=state_path(*_LEDGER_JSONL),
        collection=_LEDGER_COLLECTION,
    )


def reset_metrics() -> None:
    """Test helper — wipe the shared store."""
    _store.reset_metrics()


def log_interaction(
    prospect_id: str,
    outcome: str,
    objection: str | None,
    product: str,
    angle: str,
) -> None:
    """Record one outcome (thread-safe + persistent + ledger-appended)."""
    _store.log_interaction(
        prospect_id=prospect_id,
        outcome=outcome,
        objection=objection,
        product=product,
        angle=angle,
    )
    try:
        _ledger().append({
            "ts": iso_now(),
            "prospect_id": prospect_id or "",
            "outcome": outcome or "",
            "objection": objection or "",
            "product": product or "",
            "angle": angle or "",
        })
    except Exception as exc:  # noqa: BLE001 — ledger is additive telemetry
        _LOG.warning("interaction ledger append failed: %s", exc)


def rebuild_from_ledger() -> dict[str, Any]:
    """Reconstruct the aggregate store by replaying the interaction ledger.

    Resets the shared feedback store and re-folds every ledger row through
    ``log_interaction`` (store path only — no re-append). Returns the rebuilt
    snapshot. Fail-soft: a read failure returns the current snapshot
    untouched; malformed rows are skipped.
    """
    try:
        rows = _ledger().tail(limit=_REBUILD_TAIL)
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("interaction ledger rebuild read failed: %s", exc)
        return _store.snapshot()
    _store.reset_metrics()
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            _store.log_interaction(
                prospect_id=str(row.get("prospect_id", "")),
                outcome=str(row.get("outcome", "")),
                objection=(str(row.get("objection")) if row.get("objection") else None),
                product=str(row.get("product", "")),
                angle=str(row.get("angle", "")),
            )
        except Exception as exc:  # noqa: BLE001 — one poison row is not fatal
            _LOG.warning("interaction ledger rebuild row skipped: %s", exc)
    return _store.snapshot()


def get_top_objections() -> list[tuple[str, int]]:
    return _store.get_top_objections()


def get_best_products() -> list[tuple[str, int]]:
    return _store.get_best_products()


def get_angle_performance() -> dict[str, float]:
    return _store.get_angle_performance()


def snapshot(*, rebuild: bool = False) -> dict[str, Any]:
    """Aggregate snapshot; ``rebuild=True`` reconstructs it from the ledger."""
    if rebuild:
        return rebuild_from_ledger()
    return _store.snapshot()
