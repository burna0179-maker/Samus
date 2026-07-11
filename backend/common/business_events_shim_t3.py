"""Guarded shim over the unified business-event stream (Tranche 1 sibling).

``backend/common/business_events.py`` is being built on a sibling branch
(Tranche 1) with the contract below. Until that branch merges, this module is
the ONLY place Tranche-3 code touches the unified stream: a guarded import
that degrades to a structured no-op when the module is absent, so every
Tranche-3 feature works (and tests pass) pre-merge and starts emitting the
moment the sibling lands — with zero call-site edits.

Contract (owned by the sibling branch — do NOT re-implement it here):

    emit_business_event(event_type, *, workcell, prospect_id=None,
                        opportunity_id=None, campaign_id=None,
                        variant_arm_id=None, cost_usd=None, revenue_usd=None,
                        metadata=None) -> dict
    read_events(prospect_id=None, opportunity_id=None, since=None,
                event_types=None, limit=1000) -> list[dict]

Taxonomy used by Tranche 3: ``experiment.assigned``, ``decision.made``,
``payment.received``.
"""
from __future__ import annotations

import importlib
import logging
import sys
from typing import Any

_LOG = logging.getLogger("samus.common.business_events_shim_t3")


def _real_module() -> Any | None:
    """Per-call resolve — honors ``monkeypatch.setitem(sys.modules, ...)``.

    Uses ``importlib.import_module``/``sys.modules`` lookup instead of a
    module-level ``from backend.common import business_events`` because the
    latter binds the attribute on the parent package once at import time and
    is therefore not swappable by tests that replace the sys.modules entry.
    """
    cached = sys.modules.get("backend.common.business_events")
    if cached is not None:
        return cached
    try:
        return importlib.import_module("backend.common.business_events")
    except ImportError:
        return None


def events_available() -> bool:
    """True when the unified business-event stream module is importable."""
    return _real_module() is not None


def emit_business_event(
    event_type: str,
    *,
    workcell: str,
    prospect_id: str | None = None,
    opportunity_id: str | None = None,
    campaign_id: str | None = None,
    variant_arm_id: str | None = None,
    cost_usd: float | None = None,
    revenue_usd: float | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Emit one unified business event; ``{}`` when the stream is absent.

    Fail-soft even when the module IS present: an emit failure must never
    break the learning-loop path that fired it.
    """
    mod = _real_module()
    if mod is None:
        return {}
    try:
        return mod.emit_business_event(
            event_type,
            workcell=workcell,
            prospect_id=prospect_id,
            opportunity_id=opportunity_id,
            campaign_id=campaign_id,
            variant_arm_id=variant_arm_id,
            cost_usd=cost_usd,
            revenue_usd=revenue_usd,
            metadata=metadata,
        )
    except Exception as exc:  # noqa: BLE001 — telemetry, never load-bearing
        _LOG.warning("emit_business_event(%s) failed: %s", event_type, exc)
        return {}


def read_events(
    prospect_id: str | None = None,
    opportunity_id: str | None = None,
    since: Any = None,
    event_types: Any = None,
    limit: int = 1000,
) -> list[dict[str, Any]]:
    """Read unified events; ``[]`` when the stream is absent or errors.

    Tranche-3 consumers (the consolidator) must ALSO read the concrete
    per-service ledgers that exist today — this read is additive context,
    never the sole source.
    """
    mod = _real_module()
    if mod is None:
        return []
    try:
        return list(
            mod.read_events(
                prospect_id=prospect_id,
                opportunity_id=opportunity_id,
                since=since,
                event_types=event_types,
                limit=limit,
            )
        )
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("read_events failed: %s", exc)
        return []


__all__ = ["events_available", "emit_business_event", "read_events"]
