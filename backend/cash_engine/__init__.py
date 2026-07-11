"""Cash Engine — the autonomous revenue pipeline's escalation front door.

The Cash Engine does NOT run itself; it runs *to the operator*. A single
codex-gated ingress (``POST /api/samus/review_opportunity``, handled by
:func:`backend.cash_engine.service.review_opportunity`) accepts a
:class:`~backend.cash_engine.models.RevenueTriggerRequest` from one of three
sources — the internal ``signal_decay`` trigger
(:mod:`backend.portfolio_controller.decay_scan`), an operator
``manual_review``, or a ``macro_market_shift`` — and HALTS the automation,
throwing up the Codex Gate:

  1. Does the prospect's Opportunity carry an operator-authored Stake
     Sentence?  (mandatory — the one human input that unlocks everything)
  2. Does the proposed action clear the Codex ruleset
     (:func:`backend.common.codex.validator.check_action`)?

A failed gate does not error — it *escalates*: the deal surfaces on the
operator console's ``pending_stake`` queue and the structured response names
exactly which protocol blocked it. Only when both gates clear does the engine
mint an internal envelope and enqueue the downstream sequence
(:mod:`backend.cash_engine.queue`).

This package is the escalation skeleton: front door + trigger + gate +
enqueue. Leaf execution of the staged sequence (audit -> proposal -> contact
artifacts -> outreach -> dormancy -> re-engagement) is the next phase and
hangs off a queue worker — every stage there re-validates through the same
Codex layer at each handoff.
"""

from __future__ import annotations

__all__ = [
    "models",
    "decay",
    "gate",
    "queue",
    "service",
    "state",
    "stages",
    "worker",
]
