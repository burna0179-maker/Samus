"""Tests for backend.crm.client_intent_routing.route_client_intent."""

from __future__ import annotations

from datetime import datetime, timezone

from backend.crm.client_intent_routing import (
    IntentAction,
    all_known_intents,
    route_client_intent,
)

_NOW = datetime(2026, 7, 10, 18, 0, 0, tzinfo=timezone.utc)


def test_service_issue_reported_goes_to_customer_service_urgent():
    a = route_client_intent("service_issue_reported", now=_NOW)
    assert a.task_kind == "customer_service"
    assert a.customer_service is True
    assert a.urgency == "urgent"
    assert a.title_prefix == "[CS/SERVICE]"
    # urgent = due now
    assert a.due_at == "2026-07-10T18:00:00Z"


def test_escalation_needed_is_urgent_cs():
    a = route_client_intent("escalation_needed", now=_NOW)
    assert a.task_kind == "customer_service"
    assert a.customer_service is True
    assert a.title_prefix == "[CS/ESCALATION]"


def test_agreed_to_move_forward_is_high_priority():
    a = route_client_intent("agreed_to_move_forward", now=_NOW)
    assert a.task_kind == "client_correspondence"
    assert a.customer_service is False
    assert a.urgency == "high"
    assert a.title_prefix == "[CLIENT/AGREED]"
    # high = +4h
    assert a.due_at == "2026-07-10T22:00:00Z"


def test_counter_offered_is_normal_priority():
    a = route_client_intent("counter_offered", now=_NOW)
    assert a.urgency == "normal"
    assert a.title_prefix == "[CLIENT/COUNTER]"
    # normal = +24h
    assert a.due_at == "2026-07-11T18:00:00Z"


def test_multiple_objections_share_prefix():
    for tag in ("objected_price", "objected_scope", "objected_timing"):
        assert route_client_intent(tag, now=_NOW).title_prefix == "[CLIENT/OBJECTION]"


def test_acknowledgment_is_low_priority():
    a = route_client_intent("acknowledgment", now=_NOW)
    assert a.urgency == "low"
    assert a.title_prefix == "[CLIENT/ACK]"
    # low = +3d
    assert a.due_at == "2026-07-13T18:00:00Z"


def test_unknown_intent_falls_back_to_generic_client():
    a = route_client_intent("something_not_in_vocab", now=_NOW)
    assert a.task_kind == "client_correspondence"
    assert a.customer_service is False
    assert a.urgency == "normal"
    assert a.title_prefix == "[CLIENT]"


def test_none_intent_falls_back_gracefully():
    a = route_client_intent(None, now=_NOW)
    assert a.title_prefix == "[CLIENT]"


def test_empty_intent_falls_back_gracefully():
    a = route_client_intent("", now=_NOW)
    assert a.title_prefix == "[CLIENT]"
    assert a.urgency == "normal"


def test_case_insensitive_lookup():
    a = route_client_intent("COUNTER_OFFERED", now=_NOW)
    assert a.title_prefix == "[CLIENT/COUNTER]"


def test_urgency_ordering_by_due_at():
    # A queue sorted ascending by due_at MUST put urgent tasks first.
    tags = [
        "service_issue_reported",  # urgent
        "agreed_to_move_forward",  # high
        "counter_offered",  # normal
        "acknowledgment",  # low
    ]
    dues = [route_client_intent(t, now=_NOW).due_at for t in tags]
    assert dues == sorted(dues), (
        f"tasks should be strictly ordered by due_at: {list(zip(tags, dues))}"
    )


def test_all_known_intents_returns_stable_sorted_list():
    known = all_known_intents()
    assert known == sorted(known)
    assert "service_issue_reported" in known
    assert "unknown" in known


def test_intent_action_is_hashable_and_immutable():
    # frozen dataclass — safe to use as a dict key or in a set
    a = route_client_intent("counter_offered", now=_NOW)
    assert isinstance(a, IntentAction)
    d = {a: "test"}
    assert d[a] == "test"
