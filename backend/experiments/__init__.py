"""Experiment registry workcell-shared package (Tranche 3 learning loop).

Generalizes the email-variant UCB1 bandit in :mod:`backend.attribution.engine`
to registered experiments over openers, offers, pricing tiers, cadence
patterns, call-script elements, subjects, CTAs and landing variants.

Modules:
  * :mod:`backend.experiments.registry` — Experiment records, persisted JSON
    registry, UCB1-delegating ``assign`` with an append-only assignment ledger.
  * :mod:`backend.experiments.significance` — stdlib two-proportion z-test.
  * :mod:`backend.experiments.promoter` — nightly promotion/demotion +
    campaign stop rule.
  * :mod:`backend.experiments.routes` — gateway ``/admin/experiments`` routes.
"""
