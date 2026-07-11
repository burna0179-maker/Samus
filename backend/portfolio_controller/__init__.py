"""Portfolio-controller workcell — portfolio-level coordination across workcells.

Tracks per-workcell efficiency / cost signals and rebalances token quotas
and priority weights to suppress queue entropy. It *extends* the existing
``backend.strategy`` subsystem (UCB1 bandit, event-driven triggers, allocation
engine) rather than duplicating it — strategy is imported read-only.

The workcell is request-driven and makes ZERO LLM calls of its own: the only
LLM round-trip in this neighbourhood is strategy's metered
``propose_allocation`` proposal, which this workcell never invokes.
"""
