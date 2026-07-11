"""Tests for backend.crm.lifecycle — pure function auto-generators.

All tests exercise the lifecycle module without any persistence or I/O.
Phase 6.
"""

from __future__ import annotations

import re


from backend.crm.lifecycle import (
    OPPORTUNITY_CLOSED_LOST_TASK_KIND,
    OPPORTUNITY_CLOSED_WON_TASK_KIND,
    OPPORTUNITY_CREATED_TASK_KIND,
    OPPORTUNITY_PROPOSAL_TASK_KIND,
    tasks_for_new_opportunity,
    tasks_for_stage_advance,
)
from backend.crm.models import Opportunity


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ISO8601_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def _make_opp(**kwargs) -> Opportunity:
    defaults = dict(
        opportunity_id="opp_test001",
        prospect_id="pros_abc",
        name="Acme Corp",
        stage="new",
        intent_score=50,
        assigned_to="ops@example.com",
    )
    defaults.update(kwargs)
    return Opportunity(**defaults)


# ---------------------------------------------------------------------------
# tasks_for_new_opportunity
# ---------------------------------------------------------------------------


def test_tasks_for_new_opportunity_yields_followup_for_new_stage():
    opp = _make_opp(stage="new")
    tasks = tasks_for_new_opportunity(opp)
    assert len(tasks) >= 1
    kinds = [t.kind for t in tasks]
    assert OPPORTUNITY_CREATED_TASK_KIND in kinds


def test_tasks_for_new_opportunity_yields_proposal_for_high_intent():
    opp = _make_opp(stage="new")
    tasks = tasks_for_new_opportunity(opp, intent_score=75)
    kinds = [t.kind for t in tasks]
    assert OPPORTUNITY_CREATED_TASK_KIND in kinds
    assert OPPORTUNITY_PROPOSAL_TASK_KIND in kinds
    assert len(tasks) == 2


def test_tasks_for_new_opportunity_no_proposal_below_threshold():
    opp = _make_opp(stage="new")
    tasks = tasks_for_new_opportunity(opp, intent_score=69)
    assert len(tasks) == 1
    assert tasks[0].kind == OPPORTUNITY_CREATED_TASK_KIND


def test_tasks_for_new_opportunity_empty_for_non_new_stage():
    for stage in ("qualified", "proposal", "negotiation", "closed_won", "closed_lost"):
        opp = _make_opp(stage=stage)
        tasks = tasks_for_new_opportunity(opp, intent_score=90)
        assert tasks == [], f"expected empty for stage={stage}"


def test_tasks_for_new_opportunity_none_intent_score_yields_only_followup():
    opp = _make_opp(stage="new")
    tasks = tasks_for_new_opportunity(opp, intent_score=None)
    assert len(tasks) == 1
    assert tasks[0].kind == OPPORTUNITY_CREATED_TASK_KIND


# ---------------------------------------------------------------------------
# tasks_for_stage_advance
# ---------------------------------------------------------------------------


def test_tasks_for_stage_advance_proposal_yields_send_proposal_task():
    opp = _make_opp(stage="proposal")
    tasks = tasks_for_stage_advance(opp, prior_stage="qualified", new_stage="proposal")
    assert len(tasks) == 1
    assert tasks[0].kind == OPPORTUNITY_PROPOSAL_TASK_KIND


def test_tasks_for_stage_advance_closed_won_yields_deliver_task():
    opp = _make_opp(stage="closed_won")
    tasks = tasks_for_stage_advance(opp, prior_stage="negotiation", new_stage="closed_won")
    assert len(tasks) == 1
    assert tasks[0].kind == OPPORTUNITY_CLOSED_WON_TASK_KIND
    assert "Begin delivery" in tasks[0].title


def test_tasks_for_stage_advance_closed_lost_yields_followup_task():
    opp = _make_opp(stage="closed_lost")
    tasks = tasks_for_stage_advance(opp, prior_stage="proposal", new_stage="closed_lost")
    assert len(tasks) == 1
    assert tasks[0].kind == OPPORTUNITY_CLOSED_LOST_TASK_KIND
    assert "post-mortem" in tasks[0].title.lower() or "Post-mortem" in tasks[0].title


def test_tasks_for_stage_advance_unhandled_stage_returns_empty():
    for stage in ("new", "qualified", "negotiation"):
        opp = _make_opp(stage=stage)
        tasks = tasks_for_stage_advance(opp, prior_stage="new", new_stage=stage)
        assert tasks == [], f"expected empty for new_stage={stage}"


# ---------------------------------------------------------------------------
# Due-date and assignee assertions
# ---------------------------------------------------------------------------


def test_task_due_dates_are_iso8601_strings():
    opp = _make_opp(stage="new")
    all_tasks = tasks_for_new_opportunity(opp, intent_score=80)
    for task in all_tasks:
        assert _ISO8601_RE.match(task.due_at), (
            f"due_at={task.due_at!r} is not ISO-8601 YYYY-MM-DDTHH:MM:SSZ"
        )

    opp_proposal = _make_opp(stage="proposal")
    for task in tasks_for_stage_advance(opp_proposal, "qualified", "proposal"):
        assert _ISO8601_RE.match(task.due_at), f"due_at={task.due_at!r}"


def test_tasks_preserve_assigned_to_from_opportunity():
    assignee = "salesperson@hustleforge.tech"
    opp = _make_opp(stage="new", assigned_to=assignee)
    tasks = tasks_for_new_opportunity(opp, intent_score=80)
    for task in tasks:
        assert task.assignee == assignee

    opp_won = _make_opp(stage="closed_won", assigned_to=assignee)
    for task in tasks_for_stage_advance(opp_won, "negotiation", "closed_won"):
        assert task.assignee == assignee


def test_new_opportunity_follow_up_references_opportunity_entity():
    opp = _make_opp(stage="new", opportunity_id="opp_ref123")
    tasks = tasks_for_new_opportunity(opp)
    follow_up = tasks[0]
    assert follow_up.related_entity_kind == "opportunity"
    assert follow_up.related_entity_id == "opp_ref123"
    assert follow_up.source == "lifecycle_auto_generator"
