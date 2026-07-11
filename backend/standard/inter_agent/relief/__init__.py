"""inter_agent.relief — Samus peer-side operator-relief forwarder.

The SENDING half of cross-agent relief routing: mirrors Samus's stale
operator-pending deals (CRM opportunities awaiting a Stake Sentence) to Anita's
``/api/relief/intake`` so they reach the Agora for tribal deliberation when the
operator is away. Dormant until ``samus_agora_relief_forward_enabled`` (and
Anita's intake flag) are flipped; never resolves anything (Axiom A).
"""
from __future__ import annotations

from .task import (
    build_forwarder,
    pending_stake_items,
    start_relief_forwarder,
    stop_relief_forwarder,
)

__all__ = [
    "build_forwarder",
    "pending_stake_items",
    "start_relief_forwarder",
    "stop_relief_forwarder",
]
