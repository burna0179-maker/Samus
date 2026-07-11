"""Opportunity pipeline FSM — Phase 3.

Pure functions only. No I/O, no DDB. Service layer composes these against
:mod:`backend.crm.persistence` to read/write rows; tests mock at that seam.

Stage graph (mirrors the recovery FSM in recovery/autonomous_closer.py,
collapsed from the 7-state conversation flow into the deal-pipeline
stages that survive into the DDB ``samus_opportunities`` schema):

    new --> qualified --> proposal --> negotiation --> closed_won
                                                   --> closed_won_retainer
                                                   --> closed_lost
       \\                                          ^
        \\------------------- closed_lost ---------+
         \\                  (early disqualify)
          \\
           +-> closed_lost (immediate, hard pass)

``closed_won_retainer`` is a SECOND won-terminal stage: a deal that closes
into a recurring retainer rather than a one-off project. It is reachable
from exactly the same non-terminal stages as ``closed_won`` (qualified /
proposal / negotiation) and carries the same 1.0 win probability — the
distinction is the revenue shape, not the pipeline geometry. The
:class:`backend.crm.models.Opportunity` model, the advance-request schema,
and the empirical close-probability analytics all already recognise it as a
won close; this FSM is the gate that makes a ``target_stage=
"closed_won_retainer"`` advance actually legal (CRM Phase-2 FSM completion).

Re-opening is intentionally not allowed: closed_won, closed_won_retainer,
and closed_lost are terminal. If a "won" deal needs revisiting it becomes a
new Opportunity linked back to the same prospect (preserves historical
close metrics).

Skipping stages forward is allowed within the linear path (e.g. new ->
proposal) because in practice the operator sometimes books a call and
sends a proposal in one beat. The closed_lost transition is reachable
from every non-terminal stage to model "they ghosted" / "not a fit"
exits anywhere along the line.
"""

from __future__ import annotations

from typing import Final

from .models import OpportunityStage


# ---------------------------------------------------------------------------
# Stage probability + terminal markers
# ---------------------------------------------------------------------------

TERMINAL_STAGES: Final[set[str]] = {
    "closed_won",
    "closed_won_retainer",
    "closed_lost",
}

# Won-terminal stages (a deal that converted). Both close 1.0; the difference
# is revenue shape (one-off project vs recurring retainer), not pipeline
# geometry. Kept as a named set so callers (analytics, dispatch) share one
# definition of "this counts as a win".
WON_TERMINAL_STAGES: Final[set[str]] = {"closed_won", "closed_won_retainer"}

STAGE_PROBABILITIES: Final[dict[str, float]] = {
    "new": 0.10,
    "qualified": 0.25,
    "proposal": 0.50,
    "negotiation": 0.70,
    "closed_won": 1.0,
    "closed_won_retainer": 1.0,
    "closed_lost": 0.0,
}


# Allowed forward transitions, keyed by current stage. Every non-terminal
# stage can also transition to closed_lost (handled in the union below so
# the table stays readable). ``closed_won_retainer`` is reachable from
# exactly the same stages as ``closed_won`` — see the module docstring.
_FORWARD_TRANSITIONS: Final[dict[str, set[str]]] = {
    "new": {"qualified", "proposal", "negotiation"},
    "qualified": {"proposal", "negotiation", "closed_won", "closed_won_retainer"},
    "proposal": {"negotiation", "closed_won", "closed_won_retainer"},
    "negotiation": {"closed_won", "closed_won_retainer"},
    "closed_won": set(),
    "closed_won_retainer": set(),
    "closed_lost": set(),
}


def next_allowed_stages(current: OpportunityStage) -> set[OpportunityStage]:
    """Return the set of stages reachable in one transition from ``current``.

    Every non-terminal stage may always transition to ``closed_lost`` (a
    deal can die at any point). Terminal stages return an empty set.
    """
    if current in TERMINAL_STAGES:
        return set()
    allowed = set(_FORWARD_TRANSITIONS.get(current, set()))
    allowed.add("closed_lost")
    return allowed  # type: ignore[return-value]


def validate_transition(
    current: OpportunityStage,
    target: OpportunityStage,
) -> tuple[bool, str | None]:
    """Return ``(allowed, reason)`` for a stage transition.

    Reason is ``None`` on success and a short snake_case string on failure
    (caller surfaces it to the operator + the audit ledger).
    """
    if current == target:
        return False, "noop_same_stage"
    if current in TERMINAL_STAGES:
        return False, f"terminal_stage_{current}"
    if target not in STAGE_PROBABILITIES:
        return False, f"unknown_target_{target}"
    if target not in next_allowed_stages(current):
        return False, f"illegal_transition_{current}_to_{target}"
    return True, None
