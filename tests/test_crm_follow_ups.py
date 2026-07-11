"""CRM follow-ups-due query — outreach_sent prospects awaiting a follow-up call.

Covers backend.crm.service.list_follow_ups_due: it scans samus_call-State for
CallStates in the ``outreach_sent`` state whose ``next_attempt_at`` is due, and
joins each to its originating outreach Conversation for the company + phone the
operator needs to place the call.

(safe_scan's real-DynamoDB pagination is covered separately in
test_crm_persistence.py; here we exercise the query + join logic with a simple
single-attr-equality table shim.)
"""

from __future__ import annotations

import datetime as _dt
from typing import Any

import backend.crm.persistence as p


class _FakeTable:
    """In-memory DDB shim — single-attr equality FilterExpression."""

    def __init__(self) -> None:
        self.items: dict[tuple, dict[str, Any]] = {}

    def put_item(self, Item: dict[str, Any]) -> None:
        key_attr = next(iter(Item.keys()))
        self.items[(key_attr, Item[key_attr])] = dict(Item)

    def get_item(self, Key: dict[str, Any]) -> dict[str, Any]:
        key_attr, key_val = next(iter(Key.items()))
        item = self.items.get((key_attr, key_val))
        return {"Item": item} if item else {}

    def scan(self, **kwargs: Any) -> dict[str, Any]:
        fe = kwargs.get("FilterExpression", "")
        vals = kwargs.get("ExpressionAttributeValues", {}) or {}
        names = kwargs.get("ExpressionAttributeNames", {}) or {}
        out = list(self.items.values())
        if fe and isinstance(fe, str) and "=" in fe:
            attr = names.get("#f")
            target = vals.get(":v")
            if attr is not None:
                out = [it for it in out if str(it.get(attr, "")) == str(target or "")]
        return {"Items": out[: kwargs.get("Limit", 50)]}


def _patch(monkeypatch) -> dict[str, _FakeTable]:
    call_state = _FakeTable()
    conversations = _FakeTable()
    opportunities = _FakeTable()
    monkeypatch.setattr(p, "_call_state_table", lambda: call_state)
    monkeypatch.setattr(p, "_conversations_table", lambda: conversations)
    monkeypatch.setattr(p, "_opportunities_table", lambda: opportunities)
    return {
        "call_state": call_state,
        "conversations": conversations,
        "opportunities": opportunities,
    }


_TODAY = _dt.date(2026, 5, 22)


def test_call_state_accepts_outreach_sent():
    """outreach_sent is a valid CallStateValue."""
    from backend.crm.models import CallState

    cs = CallState(prospect_id="pr_x", state="outreach_sent")
    assert cs.state == "outreach_sent"


def test_list_follow_ups_due_joins_outreach_conversation(monkeypatch):
    """A due outreach_sent CallState is returned, joined to its outreach
    Conversation for the company + phone the operator needs to call."""
    tables = _patch(monkeypatch)
    yesterday = (_TODAY - _dt.timedelta(days=1)).isoformat()
    emailed = (_TODAY - _dt.timedelta(days=2)).isoformat()

    tables["call_state"].put_item(
        {
            "prospect_id": "pr_due",
            "state": "outreach_sent",
            "next_attempt_at": yesterday,
            "attempt_count": 0,
        }
    )
    tables["conversations"].put_item(
        {
            "conversation_id": "cv_1",
            "prospect_id": "pr_due",
            "channel": "email",
            "status": "completed",
            "outcome": "outreach_sent",
            "started_at": emailed + "T09:00:00Z",
            "structured_data": {
                "company": "Acme HVAC",
                "phone": "555-0100",
                "subject": "Quick question",
                "emailed_on": emailed,
            },
        }
    )

    from backend.crm.service import list_follow_ups_due

    out = list_follow_ups_due(today=_TODAY.isoformat())

    assert out.count == 1
    fu = out.follow_ups[0]
    assert fu.prospect_id == "pr_due"
    assert fu.company == "Acme HVAC"
    assert fu.phone == "555-0100"
    assert fu.subject == "Quick question"
    assert fu.channel == "email"
    assert fu.follow_up_on == yesterday
    assert fu.days_waiting == 2
    assert out.ddb_error is None


def test_list_follow_ups_due_excludes_not_due_and_non_outreach(monkeypatch):
    """Future-dated, unscheduled, and already-called CallStates are excluded."""
    tables = _patch(monkeypatch)
    tomorrow = (_TODAY + _dt.timedelta(days=1)).isoformat()
    yesterday = (_TODAY - _dt.timedelta(days=1)).isoformat()

    cs = tables["call_state"]
    cs.put_item({"prospect_id": "pr_future", "state": "outreach_sent", "next_attempt_at": tomorrow})
    cs.put_item({"prospect_id": "pr_unscheduled", "state": "outreach_sent", "next_attempt_at": ""})
    cs.put_item({"prospect_id": "pr_called", "state": "completed", "next_attempt_at": yesterday})

    from backend.crm.service import list_follow_ups_due

    out = list_follow_ups_due(today=_TODAY.isoformat())
    assert out.count == 0


def test_list_follow_ups_due_due_today_is_included(monkeypatch):
    """A follow-up scheduled for exactly today counts as due."""
    tables = _patch(monkeypatch)
    tables["call_state"].put_item(
        {
            "prospect_id": "pr_today",
            "state": "outreach_sent",
            "next_attempt_at": _TODAY.isoformat(),
        }
    )
    from backend.crm.service import list_follow_ups_due

    out = list_follow_ups_due(today=_TODAY.isoformat())
    assert out.count == 1
    assert out.follow_ups[0].prospect_id == "pr_today"


def test_list_follow_ups_due_without_conversation_still_lists_prospect(monkeypatch):
    """A due CallState with no joinable Conversation still surfaces — the
    operator sees the prospect_id even if company/phone couldn't be resolved."""
    tables = _patch(monkeypatch)
    yesterday = (_TODAY - _dt.timedelta(days=1)).isoformat()
    tables["call_state"].put_item(
        {
            "prospect_id": "pr_orphan",
            "state": "outreach_sent",
            "next_attempt_at": yesterday,
        }
    )
    from backend.crm.service import list_follow_ups_due

    out = list_follow_ups_due(today=_TODAY.isoformat())
    assert out.count == 1
    assert out.follow_ups[0].prospect_id == "pr_orphan"
    assert out.follow_ups[0].company == ""


# --- second-opportunity upsell ---------------------------------------------


def test_suggest_upsell_maps_workflow_signal_to_buildout():
    from backend.crm.service import _suggest_upsell

    sku, name, pitch = _suggest_upsell("their manual workflow is a mess, wants to automate")
    assert sku == "service_workflow_buildout"
    assert name == "Workflow System Buildout"
    assert "$2,500" in pitch


def test_suggest_upsell_maps_phone_signal_to_receptionist():
    from backend.crm.service import _suggest_upsell

    sku, name, pitch = _suggest_upsell("they keep complaining about missed calls and voicemail")
    assert sku == "retainer_ai_receptionist"
    assert name == "AI Digital Receptionist"
    assert "$99/mo" in pitch


def test_suggest_upsell_empty_on_no_signal():
    from backend.crm.service import _suggest_upsell

    assert _suggest_upsell("thanks for your time, talk soon") == ("", "", "")


def test_suggest_upsell_empty_on_tie():
    """Equal signal for two SKUs -> no suggestion (never guess)."""
    from backend.crm.service import _suggest_upsell

    assert _suggest_upsell("workflow and seo") == ("", "", "")


def test_list_follow_ups_due_carries_upsell_from_conversation(monkeypatch):
    """An interest signal in a conversation transcript drives the upsell."""
    tables = _patch(monkeypatch)
    yesterday = (_TODAY - _dt.timedelta(days=1)).isoformat()
    tables["call_state"].put_item(
        {
            "prospect_id": "pr_due",
            "state": "outreach_sent",
            "next_attempt_at": yesterday,
        }
    )
    tables["conversations"].put_item(
        {
            "conversation_id": "cv_1",
            "prospect_id": "pr_due",
            "channel": "email",
            "status": "completed",
            "outcome": "outreach_sent",
            "started_at": (_TODAY - _dt.timedelta(days=2)).isoformat() + "T09:00:00Z",
            "transcript": "owner said their manual workflow is a mess, wants to automate",
            "structured_data": {"company": "Acme HVAC", "phone": "555-0100"},
        }
    )
    from backend.crm.service import list_follow_ups_due

    out = list_follow_ups_due(today=_TODAY.isoformat())
    assert out.count == 1
    assert out.follow_ups[0].upsell_sku == "service_workflow_buildout"
    assert out.follow_ups[0].upsell_name == "Workflow System Buildout"
    assert out.follow_ups[0].upsell_pitch


def test_list_follow_ups_due_carries_upsell_from_opportunity(monkeypatch):
    """An interest signal in the prospect's Opportunity also drives the upsell."""
    tables = _patch(monkeypatch)
    yesterday = (_TODAY - _dt.timedelta(days=1)).isoformat()
    tables["call_state"].put_item(
        {
            "prospect_id": "pr_opp",
            "state": "outreach_sent",
            "next_attempt_at": yesterday,
        }
    )
    tables["conversations"].put_item(
        {
            "conversation_id": "cv_2",
            "prospect_id": "pr_opp",
            "channel": "email",
            "status": "completed",
            "outcome": "outreach_sent",
            "started_at": yesterday + "T09:00:00Z",
            "structured_data": {"company": "Bell Roofing"},
        }
    )
    tables["opportunities"].put_item(
        {
            "opportunity_id": "op_1",
            "prospect_id": "pr_opp",
            "next_step": "follow up on their missed calls — they want a receptionist",
            "created_at": yesterday,
        }
    )
    from backend.crm.service import list_follow_ups_due

    out = list_follow_ups_due(today=_TODAY.isoformat())
    assert out.count == 1
    assert out.follow_ups[0].upsell_sku == "retainer_ai_receptionist"


def test_list_follow_ups_due_no_upsell_without_signal(monkeypatch):
    """No interest signal anywhere -> upsell fields stay empty."""
    tables = _patch(monkeypatch)
    yesterday = (_TODAY - _dt.timedelta(days=1)).isoformat()
    tables["call_state"].put_item(
        {
            "prospect_id": "pr_q",
            "state": "outreach_sent",
            "next_attempt_at": yesterday,
        }
    )
    tables["conversations"].put_item(
        {
            "conversation_id": "cv_3",
            "prospect_id": "pr_q",
            "channel": "email",
            "status": "completed",
            "outcome": "outreach_sent",
            "started_at": yesterday + "T09:00:00Z",
            "structured_data": {"company": "Quiet Co", "subject": "hello there"},
        }
    )
    from backend.crm.service import list_follow_ups_due

    out = list_follow_ups_due(today=_TODAY.isoformat())
    assert out.count == 1
    assert out.follow_ups[0].upsell_sku == ""
    assert out.follow_ups[0].upsell_pitch == ""
