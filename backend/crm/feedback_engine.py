"""Call-outcome feedback metrics — objection frequencies, close counts, and
angle win/loss rates.

v2 (2026-06-01): this is now a thin delegator over the shared, PERSISTENT
:mod:`backend.common.feedback_store`. v1 stored state in a process-local
module dict that was lost on restart AND was a byte-identical twin of
``backend.outreach.metrics``, so CRM- and outreach-logged outcomes never met.
Both modules now delegate to the single store, so outcomes accumulate together
and survive workcell restarts. Public API is unchanged.

Feeds back into ``prospecting.intelligence.determine_pitch_angle`` angle
selection and ``outreach.closer`` angle selection.

``reset_metrics()`` is a test-isolation helper and is NOT for production use.
"""

from __future__ import annotations

from typing import Any

from backend.common import feedback_store as _store

__all__ = [
    "log_interaction",
    "get_top_objections",
    "get_best_products",
    "get_angle_performance",
    "optimize_weights",
    "snapshot",
    "reset_metrics",
]


def reset_metrics() -> None:
    """Test helper — wipe the shared store. Not for production use."""
    _store.reset_metrics()


def log_interaction(
    prospect_id: str,
    outcome: str,
    objection: str | None,
    product: str,
    angle: str,
) -> None:
    """Record one call outcome (thread-safe + persistent)."""
    _store.log_interaction(
        prospect_id=prospect_id,
        outcome=outcome,
        objection=objection,
        product=product,
        angle=angle,
    )


def get_top_objections() -> list[tuple[str, int]]:
    """Objection counts sorted descending by frequency."""
    return _store.get_top_objections()


def get_best_products() -> list[tuple[str, int]]:
    """Close counts per product sorted descending."""
    return _store.get_best_products()


def get_angle_performance() -> dict[str, float]:
    """Win-rate per angle; skips angles with zero interactions."""
    return _store.get_angle_performance()


def optimize_weights(intel: dict[str, Any]) -> dict[str, Any]:
    """Inject best-performing angle bias into an intel dict (mutated in place)."""
    return _store.optimize_weights(intel)


def snapshot() -> dict[str, Any]:
    """JSON-serialisable plain-dict snapshot of all counters."""
    return _store.snapshot()
