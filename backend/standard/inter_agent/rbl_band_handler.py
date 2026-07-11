"""Hub-event handler — ingest Major's RBL band broadcasts into the local cache.

This is the inbound bridge that closes the RBL consume loop. Major (the
Runway-Band-Ledger owner) broadcasts a band transition over the Quorum Hub;
this handler picks that event up and writes the band to the local cache file
that :class:`backend.governance.commercial_wrap.RblBandConsumer` reads. Without
it, the consumer's cache fallback had no producer and commercial_wrap silently
defaulted to ``healthy`` whenever no live ``samus_major_rbl_status_url`` HTTP
endpoint was configured (the default local deployment) — i.e. the RBL signal
was never actually consumed.

Authority model: the hub itself authenticates and quorum-scores events
upstream (see ``_shared/quorum_hub``); this handler additionally restricts the
producer to ``caller == "major"`` and only writes bands the wrapper already
recognises (``ingest_rbl_band`` is fail-closed on an unknown band). A
forged/garbled alert therefore can never *clear* a real freeze by overwriting
the cache with garbage. The live HTTP probe, when configured, remains the
authoritative source — this cache is only the fallback the probe falls back to.

Best-effort by contract: any exception is swallowed by the dispatcher (one bad
handler must never take down the SSE consumer), and a write failure leaves the
previously-cached band intact.
"""

from __future__ import annotations

import logging
from typing import Any

from .event_handler import register_handler

_LOG = logging.getLogger("samus.inter_agent.rbl_band_handler")

# The agent that owns the Runway-Band-Ledger and broadcasts band transitions.
RBL_PRODUCER_AGENT = "major"

# Hub-event ``action`` verbs that carry an RBL band transition. Matched
# case-insensitively against a substring so a future Major action verb that
# still mentions "rbl"/"band" continues to route here.
_RBL_ACTION_HINTS = ("rbl_band", "rbl_status", "band_change", "band_transition", "rbl")

# Field names a band value may travel under in the event payload.
_BAND_FIELDS = ("current_band", "band", "rbl_band", "new_band", "to_band")


def _extract_band(event: dict[str, Any]) -> str:
    """Pull the band string out of the event (top-level or a nested payload)."""
    for field in _BAND_FIELDS:
        val = event.get(field)
        if isinstance(val, str) and val.strip():
            return val.strip()
    payload = event.get("payload")
    if isinstance(payload, dict):
        for field in _BAND_FIELDS:
            val = payload.get(field)
            if isinstance(val, str) and val.strip():
                return val.strip()
    return ""


def _is_rbl_event(event: dict[str, Any]) -> bool:
    if str(event.get("caller", "")).strip().lower() != RBL_PRODUCER_AGENT:
        return False
    action = str(event.get("action", "")).strip().lower()
    return any(hint in action for hint in _RBL_ACTION_HINTS)


def rbl_band_event_handler(event: dict[str, Any]) -> None:
    """Ingest a Major RBL band broadcast into the commercial_wrap cache.

    No-op for any event that is not an RBL band transition from Major, or that
    carries no recognisable band. Deferred import of ``ingest_rbl_band`` keeps
    the inter_agent layer importable even if the governance layer is unavailable.
    """
    if not _is_rbl_event(event):
        return
    band = _extract_band(event)
    if not band:
        _LOG.warning(
            "rbl_band_handler: major RBL event id=%s carried no band field",
            event.get("id"),
        )
        return
    ts = event.get("ts")
    from backend.governance.commercial_wrap import ingest_rbl_band

    ok = ingest_rbl_band(
        band,
        source=f"hub:{RBL_PRODUCER_AGENT}",
        ts=str(ts) if ts is not None else None,
    )
    if ok:
        _LOG.info("rbl_band_handler: cached band=%s from major event id=%s", band, event.get("id"))


def register_rbl_band_handler() -> None:
    """Register the RBL band handler on the inbound hub-event dispatcher.

    Idempotent (``register_handler`` dedupes by callable identity).
    """
    register_handler(rbl_band_event_handler)


__all__ = [
    "RBL_PRODUCER_AGENT",
    "rbl_band_event_handler",
    "register_rbl_band_handler",
]
