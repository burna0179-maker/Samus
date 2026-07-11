"""Samus relief-forwarder lifespan task (gateway).

Wires the portable :class:`ReliefForwarder` to Samus's runtime:

  * pending_source = CRM opportunities awaiting an operator Stake Sentence
    (``crm.service.list_opportunities_pending_stake`` — the deals the operator
    console surfaces as ``pending_stake``; the genuine "stuck decision" surface
    where the engine is blocked by ``cash_engine.gate`` until the operator
    signs);
  * post_fn = :func:`post_relief_to_anita` (Samus-signed envelope, reuses
    broker_client's signing path);
  * a periodic async loop started/stopped from the gateway lifespan, mirroring
    the hub subscriber + governance ``protocol_layer`` background tasks.

Fully dormant until ``samus_agora_relief_forward_enabled`` (default False) AND
Anita's ``sn_agora_relief_intake_enabled`` are both flipped. Best-effort; never
blocks or aborts the gateway lifespan.
"""
# AXIOM-2a: boundary defender — periodic outward mirror of stale operator-pending deals.
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from .forwarder import ReliefForwarder
from .post import post_relief_to_anita

_LOG = logging.getLogger("samus.inter_agent.relief.task")

_DEFAULT_INTERVAL_SEC = 1800.0


def _iso_to_epoch(value: str) -> Optional[float]:
    """Parse an ISO-8601 created_at to epoch seconds; None if unparseable."""
    s = (value or "").strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(s).timestamp()
    except (ValueError, TypeError):
        return None


def pending_stake_items(limit: int = 50) -> List[Dict[str, Any]]:
    """Adapt CRM pending-stake opportunities -> forwarder item dicts."""
    try:
        from backend.crm.service import list_opportunities_pending_stake

        opps = list_opportunities_pending_stake(limit=limit)
    except Exception:  # noqa: BLE001 — CRM store unreachable from this process => no-op
        _LOG.debug("relief: list_opportunities_pending_stake unavailable", exc_info=True)
        return []
    out: List[Dict[str, Any]] = []
    for o in opps or []:
        oid = str(getattr(o, "opportunity_id", "") or "")
        if not oid:
            continue
        name = str(getattr(o, "name", "") or "") or oid
        stage = str(getattr(o, "stage", "") or "")
        reason = (
            f"Opportunity '{name}' (stage={stage}) is blocked awaiting the "
            f"operator's Stake Sentence before any revenue action."
        )
        out.append(
            {
                "ticket_id": oid,
                "action": "sign_stake_sentence",
                "reason": reason,
                "created_ts": _iso_to_epoch(str(getattr(o, "created_at", "") or "")),
                "payload": {
                    "stage": stage,
                    "name": name,
                    "deal_size_usd": getattr(o, "deal_size_usd", 0.0),
                    "prospect_id": getattr(o, "prospect_id", ""),
                },
            }
        )
    return out


def build_forwarder(settings: Any) -> ReliefForwarder:
    from backend.common.state_paths import state_path

    anita_url = getattr(settings, "samus_broker_base_url", "") or "https://127.0.0.1:8420"
    return ReliefForwarder(
        agent_id="samus",
        pending_source=pending_stake_items,
        post_fn=lambda req: post_relief_to_anita(req, anita_url=anita_url),
        settings=settings,
        state_path=state_path("relief", "relief_forwarded.json"),
    )


async def _relief_loop(settings: Any, interval: float) -> None:
    forwarder = build_forwarder(settings)
    while True:
        try:
            forwarder.run_once()
        except Exception:  # noqa: BLE001 — a tick fault never kills the loop
            _LOG.debug("relief_forwarder tick faulted", exc_info=True)
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            raise


async def start_relief_forwarder(app: Any, settings: Any) -> Optional[asyncio.Task]:
    """Schedule the Samus->Anita relief-forwarder loop. Idempotent.

    Gated on ``samus_agora_relief_forward_enabled`` (default False). Returns the
    task, or None when disabled.
    """
    if not bool(getattr(settings, "samus_agora_relief_forward_enabled", False)):
        _LOG.info("relief forwarder disabled (samus_agora_relief_forward_enabled=false)")
        return None
    existing = getattr(app.state, "relief_forwarder_task", None)
    if existing is not None and not existing.done():
        return existing
    interval = float(getattr(settings, "samus_agora_relief_forward_interval_sec", _DEFAULT_INTERVAL_SEC))
    task = asyncio.create_task(_relief_loop(settings, interval), name="samus.relief_forwarder")
    app.state.relief_forwarder_task = task
    _LOG.info("relief forwarder task started (interval=%.0fs)", interval)
    return task


async def stop_relief_forwarder(app: Any) -> None:
    """Cancel + await the relief-forwarder task. Idempotent + best-effort."""
    task = getattr(app.state, "relief_forwarder_task", None)
    if task is None:
        return
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):  # noqa: BLE001 — teardown swallows
        pass
    app.state.relief_forwarder_task = None
    _LOG.info("relief forwarder task stopped")


__all__ = ["pending_stake_items", "build_forwarder", "start_relief_forwarder", "stop_relief_forwarder"]
