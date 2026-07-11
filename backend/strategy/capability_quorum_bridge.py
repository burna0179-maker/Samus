"""Capability marketplace → quorum hub bridge.

Publishes ``capability.published`` and ``capability.withdrawn`` events to
the cross-agent Quorum Hub via :mod:`backend.common.quorum_client`.

Matches the ``quorum_publisher`` pattern (:mod:`backend.common.quorum_publisher`)
one-for-one:

- Uses ``get_quorum_client()`` — inherits its HMAC-signing envelope
  (``SAMUS_QUORUM_HUB_HMAC_KEY``), the dormancy gate
  (``SAMUS_QUORUM_PUBLISH_ENABLED``), and its fail-open transport.
- Emits via the hub's ``governance_publish`` tool — the sole
  hub-accepted publish surface. We map capability events onto its
  ``(caller, action, risk_score, approved, approval_score, threshold,
  votes, reason)`` schema so no hub-side change is required.
- Best-effort + fail-open. Every helper returns ``False`` on any
  transport / hub error; the caller (capability marketplace) never
  crashes on a hub outage.

Direction
---------
Samus-outbound only for now. The plan (Concept 6, verdict "BUILD LATER"
excerpt in the assimilation doc) explicitly notes no other agent yet
publishes into Samus's registry. A future subscriber that mirrors
remote listings into a local marketplace would attach to the same
event names emitted here.
"""
from __future__ import annotations

import logging
from typing import Any

from backend.common.quorum_client import get_quorum_client
from backend.strategy.capability_marketplace import CapabilityListing

_LOG = logging.getLogger("samus.strategy.capability_quorum_bridge")

# Cross-agent action names. The hub records these verbatim in its event
# log so downstream subscribers can filter on them.
ACTION_PUBLISHED = "capability.published"
ACTION_WITHDRAWN = "capability.withdrawn"

# Capability publish/withdraw is a routine advertising event — low risk,
# not an escalation. The hub records the risk as an axis for downstream
# consumers rather than as an approval gate.
_ROUTINE_RISK = 0.0
_ROUTINE_THRESHOLD = 0.5


def _listing_reason(listing: CapabilityListing) -> str:
    tags = ",".join(listing.tags) if listing.tags else "none"
    return (
        f"capability={listing.capability_id}; "
        f"provider={listing.provider_agent}; "
        f"cost={listing.cost}; "
        f"perf={listing.performance_score:.2f}; "
        f"latency_ms={listing.latency_ms}; "
        f"tags={tags}"
    )


def _listing_votes(listing: CapabilityListing) -> list[dict[str, Any]]:
    """Single vote row that carries the listing summary.

    ``governance_publish`` requires a ``votes`` list; we use it to convey
    the listing's decisive fields so a subscriber can reconstruct the
    listing without a schema extension on the hub side.
    """
    return [
        {
            "voter": listing.provider_agent,
            "vote": "OFFER",
            "weight": max(0.0, min(1.0, float(listing.performance_score))),
        }
    ]


def publish_capability_published(listing: CapabilityListing, *, caller: str = "samus") -> bool:
    """Announce a new / updated capability listing to the quorum hub.

    Maps to ``governance_publish``::

        caller         = caller (default 'samus'; the announcing agent)
        action         = 'capability.published'
        risk_score     = 0.0  (routine advertisement, not an escalation)
        approved       = True (a published capability is an accepted offer)
        approval_score = performance_score, clamped to [0, 1]
        threshold      = 0.5
        votes          = [{voter: provider_agent, vote: 'OFFER',
                          weight: performance_score}]
        reason         = "capability=<id>; provider=<agent>; cost=...;
                          perf=...; latency_ms=...; tags=..."

    Best-effort + fail-open. Returns ``False`` on any error or when
    publishing is disabled by the hub client's env gate.
    """
    try:
        perf = max(0.0, min(1.0, float(listing.performance_score)))
        return get_quorum_client().publish(
            caller=caller,
            action=ACTION_PUBLISHED,
            risk_score=_ROUTINE_RISK,
            approved=True,
            approval_score=perf,
            threshold=_ROUTINE_THRESHOLD,
            votes=_listing_votes(listing),
            reason=_listing_reason(listing),
        )
    except Exception as exc:  # noqa: BLE001
        _LOG.debug("publish_capability_published failed: %s", exc)
        return False


def publish_capability_withdrawn(
    capability_id: str,
    provider_agent: str,
    *,
    caller: str = "samus",
) -> bool:
    """Announce a capability withdrawal to the quorum hub.

    Same schema as :func:`publish_capability_published`, action set to
    ``capability.withdrawn`` and ``approved=False`` (the capability is
    no longer on offer).
    """
    reason = f"capability={capability_id}; provider={provider_agent}; withdrawn"
    try:
        return get_quorum_client().publish(
            caller=caller,
            action=ACTION_WITHDRAWN,
            risk_score=_ROUTINE_RISK,
            approved=False,
            approval_score=0.0,
            threshold=_ROUTINE_THRESHOLD,
            votes=[{"voter": provider_agent, "vote": "WITHDRAW", "weight": 1.0}],
            reason=reason,
        )
    except Exception as exc:  # noqa: BLE001
        _LOG.debug("publish_capability_withdrawn failed: %s", exc)
        return False


__all__ = [
    "ACTION_PUBLISHED",
    "ACTION_WITHDRAWN",
    "publish_capability_published",
    "publish_capability_withdrawn",
]
