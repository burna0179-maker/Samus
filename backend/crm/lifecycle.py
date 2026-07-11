"""Lifecycle auto-generators — produces OperatorTask records when CRM lifecycle
events fire (new opportunity, stage advance, artifact created, etc.).
Pure functions; caller (service.py) persists the produced tasks.  Phase 6.

Tranche 3 (learning loop): terminal stage transitions additionally trigger the
ADR-004 reward computation automatically (:func:`trigger_terminal_reward`) so
the bandit's credit signal no longer depends on a manual invocation. The hook
is strictly fail-soft — a reward failure never blocks the CRM transition or
the operator-task generation it rides on.
"""
from __future__ import annotations

import logging
from typing import Any

from backend.common.dates import hours_from_now

from .models import CreateOperatorTaskRequest, Opportunity

_LOG = logging.getLogger("samus.crm.lifecycle")

# Stages that end an opportunity's pipeline life — the moment the closed-loop
# reward becomes computable (mirrors reward_density._TERMINAL_STAGES).
TERMINAL_STAGES = frozenset({"closed_won", "closed_won_retainer", "closed_lost"})

# ---------------------------------------------------------------------------
# Event-type → task-template lookup constants
# ---------------------------------------------------------------------------

OPPORTUNITY_CREATED_TASK_KIND = "follow_up"
OPPORTUNITY_PROPOSAL_TASK_KIND = "send_proposal"
OPPORTUNITY_CLOSED_WON_TASK_KIND = "deliver"
OPPORTUNITY_CLOSED_LOST_TASK_KIND = "follow_up"


# ---------------------------------------------------------------------------
# Automatic reward trigger (Tranche 3 — close the learning loop)
# ---------------------------------------------------------------------------

def trigger_terminal_reward(opportunity_id: str, *, new_stage: str) -> bool:
    """Fire the ADR-004 reward computation for a terminal transition.

    Called from :func:`tasks_for_stage_advance` whenever ``new_stage`` is
    terminal. Fail-soft by contract: every failure (missing opportunity,
    ledger error, codex veto, harm-collector outage) is logged and swallowed
    — learning is an optimization, never load-bearing for the CRM write path.

    Returns True when a reward was computed and persisted, False otherwise.
    """
    if new_stage not in TERMINAL_STAGES:
        return False
    try:
        from backend.strategy.reward_density import compute_reward

        comp = compute_reward(
            opportunity_id,
            correlation_id=f"lifecycle_terminal:{new_stage}",
        )
        _LOG.info(
            "auto reward computed opp=%s stage=%s reward=%.3f",
            opportunity_id, new_stage, comp.reward,
        )
        return True
    except Exception as exc:  # noqa: BLE001 — fail-soft by contract
        _LOG.warning(
            "auto reward failed opp=%s stage=%s: %s",
            opportunity_id, new_stage, exc,
        )
        return False


# ---------------------------------------------------------------------------
# Pure lifecycle functions
# ---------------------------------------------------------------------------

def tasks_for_new_opportunity(
    opp: Opportunity,
    intent_score: int | None = None,
) -> list[CreateOperatorTaskRequest]:
    """Produce OperatorTask requests when a new Opportunity is created.

    Rules
    -----
    - stage == "new"  →  always yields a follow_up task (24 h due)
    - intent_score >= 70  →  also yields a high-priority send_proposal task (4 h due)

    Returns an empty list if the opportunity is not in the "new" stage.
    """
    if opp.stage != "new":
        return []

    label = opp.name or opp.prospect_id

    tasks: list[CreateOperatorTaskRequest] = [
        CreateOperatorTaskRequest(
            kind=OPPORTUNITY_CREATED_TASK_KIND,
            title=f"Follow up with prospect for {label}",
            related_entity_kind="opportunity",
            related_entity_id=opp.opportunity_id,
            source="lifecycle_auto_generator",
            source_ref="opportunity_created",
            assignee=opp.assigned_to,
            due_at=hours_from_now(24),
        ),
    ]

    if intent_score is not None and intent_score >= 70:
        tasks.append(
            CreateOperatorTaskRequest(
                kind=OPPORTUNITY_PROPOSAL_TASK_KIND,
                title=f"Send proposal to {label} (high-intent lead)",
                related_entity_kind="opportunity",
                related_entity_id=opp.opportunity_id,
                source="lifecycle_auto_generator",
                source_ref="opportunity_created_high_intent",
                assignee=opp.assigned_to,
                due_at=hours_from_now(4),
            ),
        )

    return tasks


def tasks_for_stage_advance(
    opp: Opportunity,
    prior_stage: str,
    new_stage: str,
) -> list[CreateOperatorTaskRequest]:
    """Produce OperatorTask requests when an Opportunity advances stages.

    Rules
    -----
    - new_stage == "proposal"     →  send_proposal task (24 h due)
    - new_stage == "closed_won"   →  deliver task (24 h due)
    - new_stage == "closed_lost"  →  follow_up post-mortem task (72 h due)
    - anything else               →  empty list

    Parameters
    ----------
    opp:         The Opportunity *after* the stage transition has been applied.
    prior_stage: Stage before the transition.
    new_stage:   Stage after the transition (should equal opp.stage).

    Side effect (Tranche 3): a terminal ``new_stage`` also triggers the
    automatic ADR-004 reward computation (fail-soft; see
    :func:`trigger_terminal_reward`).
    """
    label = opp.name or opp.prospect_id

    if new_stage in TERMINAL_STAGES:
        trigger_terminal_reward(opp.opportunity_id, new_stage=new_stage)

    if new_stage == "proposal":
        return [
            CreateOperatorTaskRequest(
                kind=OPPORTUNITY_PROPOSAL_TASK_KIND,
                title=f"Send proposal to {label}",
                related_entity_kind="opportunity",
                related_entity_id=opp.opportunity_id,
                source="lifecycle_auto_generator",
                source_ref="stage_advance_proposal",
                assignee=opp.assigned_to,
                due_at=hours_from_now(24),
            ),
        ]

    if new_stage == "closed_won":
        return [
            CreateOperatorTaskRequest(
                kind=OPPORTUNITY_CLOSED_WON_TASK_KIND,
                title=f"Begin delivery for {label}",
                related_entity_kind="opportunity",
                related_entity_id=opp.opportunity_id,
                source="lifecycle_auto_generator",
                source_ref="stage_advance_closed_won",
                assignee=opp.assigned_to,
                due_at=hours_from_now(24),
            ),
        ]

    if new_stage == "closed_lost":
        return [
            CreateOperatorTaskRequest(
                kind=OPPORTUNITY_CLOSED_LOST_TASK_KIND,
                title="Post-mortem: review lost-reason and update prospect status",
                related_entity_kind="opportunity",
                related_entity_id=opp.opportunity_id,
                source="lifecycle_auto_generator",
                source_ref="stage_advance_closed_lost",
                assignee=opp.assigned_to,
                due_at=hours_from_now(72),
            ),
        ]

    return []


__all__ = [
    "OPPORTUNITY_CREATED_TASK_KIND",
    "OPPORTUNITY_PROPOSAL_TASK_KIND",
    "OPPORTUNITY_CLOSED_WON_TASK_KIND",
    "OPPORTUNITY_CLOSED_LOST_TASK_KIND",
    "TERMINAL_STAGES",
    "trigger_terminal_reward",
    "tasks_for_new_opportunity",
    "tasks_for_stage_advance",
]
