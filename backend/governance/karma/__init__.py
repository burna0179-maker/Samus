"""Karma-gated permissions — earned, decaying, per-dimension trust that gates
how much Samus may do autonomously.

Builds ON the (previously unwired) ``backend.strategy.trust_scorer`` primitive:
the four trust dimensions (success_rate / policy_compliance / resource_efficiency
/ stability_score) become a *running* per-actor karma vector that rewards good
outcomes (sends, replies, closes), penalizes bad ones (complaints, bounces,
policy blocks, EFH refusals), and decays toward a neutral baseline over time so
neither a hot streak nor a bad patch locks in. ``trust_scorer.score_trust``
composes the vector into an ``AccessTier``; ``check`` compares it against the
tier an action requires.

Doctrine:
  * **Innocent until penalized** — an unknown actor starts at a high baseline
    (autonomous), so arming the gate is a no-op for a well-behaved Samus; karma
    only ever *restricts* after logged misbehavior. This also fixes the
    bootstrap problem (a fresh actor isn't trapped in sandbox).
  * **Fail-open on read, but it can only loosen** — a store outage returns the
    innocent baseline (revenue keeps flowing; the gate is opt-in and Samus is
    mid-cashflow-push). The authoritative restraint is the durable penalty
    record, which a recovered store re-reads.
  * **Wire-not-arm** — ``check`` is a no-op (allow) unless
    ``settings.karma_gate_enabled`` is true (default OFF).
  * **Tamper-evident** — every karma delta is written through
    ``backend.common.audit.record`` (the HMAC-chained ledger).
"""

from __future__ import annotations

__all__: list[str] = []
