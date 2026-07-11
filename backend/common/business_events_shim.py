"""Guarded shim over the unified business-event stream.

A sibling branch is building :mod:`backend.common.business_events` (Tranche 1
of the HOTL roadmap — the canonical append-only ``samus_business_events``
stream). This worktree (Tranche 2, Economic Brain) must *consume* that stream
without creating the module itself — the two branches merge later.

This shim is the ONLY place Tranche-2 code touches the event stream. It
degrades gracefully pre-merge:

* ``emit_business_event(...)`` — delegates when the real module exists;
  otherwise a no-op that returns a minimal event-shaped dict (so callers can
  log / attach it without branching).
* ``read_events(...)`` — delegates when the real module exists; otherwise
  returns ``[]`` (ROI aggregation then falls back to the concrete ledgers:
  outreach-attribution conversions, Stripe webhook ledger, reward ledger).

Contract (fixed — matches the sibling branch's API exactly)::

    emit_business_event(event_type, *, workcell, prospect_id=None,
                        opportunity_id=None, campaign_id=None,
                        variant_arm_id=None, cost_usd=None, revenue_usd=None,
                        metadata=None) -> dict
    read_events(prospect_id=None, opportunity_id=None, since=None,
                event_types=None, limit=1000) -> list[dict]

Never raises: a broken real module is treated the same as an absent one on a
per-call basis (allow-on-failure, mirrors ``llm_budget`` / ``bandit_store``).
"""
from __future__ import annotations

import logging
from typing import Any

_LOG = logging.getLogger("samus.common.business_events_shim")


def _real_module() -> Any | None:
    """Import the real event module if the sibling branch has merged.

    Resolved per-call (cheap — ``sys.modules`` hit after the first import) so
    a post-merge deployment picks it up without a restart-ordering hazard and
    tests can monkeypatch ``sys.modules['backend.common.business_events']``
    freely. Uses ``importlib.import_module`` (not ``from ... import ...``)
    because the latter looks the name up as an attribute of the parent package
    — an attribute that was bound once at real-module import time and does NOT
    update when a test replaces the sys.modules entry.
    """
    import importlib
    import sys
    # sys.modules is the authoritative first check so tests that install a
    # fake via ``monkeypatch.setitem(sys.modules, ...)`` are honored.
    cached = sys.modules.get("backend.common.business_events")
    if cached is not None:
        return cached
    try:
        return importlib.import_module("backend.common.business_events")
    except ImportError:
        return None


def events_available() -> bool:
    """True when the real unified event stream is importable."""
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
    """Emit one event to the unified stream. No-op pre-merge; never raises."""
    mod = _real_module()
    if mod is not None:
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
        except Exception as exc:  # noqa: BLE001 — emission must never break a caller
            _LOG.warning("business_events emit failed (%s); dropped %s", exc, event_type)
    # Pre-merge / degraded no-op. Returns an event-shaped dict so callers can
    # attach it to their own ledger rows uniformly.
    return {
        "event_type": event_type,
        "workcell": workcell,
        "prospect_id": prospect_id,
        "opportunity_id": opportunity_id,
        "campaign_id": campaign_id,
        "variant_arm_id": variant_arm_id,
        "cost_usd": cost_usd,
        "revenue_usd": revenue_usd,
        "metadata": metadata or {},
        "emitted": False,
    }


def read_events(
    prospect_id: str | None = None,
    opportunity_id: str | None = None,
    since: str | None = None,
    event_types: list[str] | None = None,
    limit: int = 1000,
) -> list[dict[str, Any]]:
    """Read from the unified stream. Empty pre-merge; never raises."""
    mod = _real_module()
    if mod is None:
        return []
    try:
        return list(mod.read_events(
            prospect_id=prospect_id,
            opportunity_id=opportunity_id,
            since=since,
            event_types=event_types,
            limit=limit,
        ) or [])
    except Exception as exc:  # noqa: BLE001 — reads degrade to empty
        _LOG.warning("business_events read failed (%s); returning []", exc)
        return []


__all__ = ["emit_business_event", "read_events", "events_available"]
