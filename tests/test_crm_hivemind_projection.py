"""Tests for backend.crm.hivemind_projection — CRM Phase-2 KG projection.

Covers the four doctrine guarantees:
  * flag-OFF (default) is a clean no-op (no graph writes),
  * armed + graph available -> the prospect/contact/opportunity sub-graph is
    projected with the correct nodes + allowlisted edges,
  * armed + graph UNAVAILABLE (Neo4j down) degrades to a local-default no-op,
  * the projection never raises (best-effort), and tier stamping follows
    SAMUS_KG_TIER_MODE.
"""

from __future__ import annotations

from typing import Any

import pytest

from backend.common.settings import reload_settings
from backend.crm import hivemind_projection as hp
from backend.crm.models import Contact, Conversation, Opportunity, Prospect


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeClient:
    """Minimal GraphClient stand-in, mirroring test_common_neo4j_runtime."""

    def __init__(self, available: bool = True, *, raise_on_node: bool = False) -> None:
        self.available = available
        self._raise_on_node = raise_on_node
        self.node_calls: list[tuple[str, dict[str, Any]]] = []
        self.rel_calls: list[tuple[str, Any, str, str, Any]] = []

    def write_node(self, label: str, properties: dict[str, Any]) -> bool:
        if self._raise_on_node:
            raise RuntimeError("boom")
        self.node_calls.append((label, dict(properties)))
        return True

    def write_relationship(
        self,
        source_label: str,
        source_key: Any,
        rel_type: str,
        target_label: str,
        target_key: Any,
    ) -> bool:
        self.rel_calls.append((source_label, source_key, rel_type, target_label, target_key))
        return True


def _opp(**over: Any) -> Opportunity:
    base: dict[str, Any] = dict(
        opportunity_id="op_1",
        prospect_id="pr_1",
        contact_id="co_1",
        stage="qualified",
        deal_size_usd=1200.0,
        close_probability=0.25,
    )
    base.update(over)
    return Opportunity(**base)


@pytest.fixture
def _armed(monkeypatch):
    """Arm the projection flag; tier mode off by default."""
    monkeypatch.setenv("SAMUS_CRM_HIVEMIND_PROJECTION_ENABLED", "true")
    monkeypatch.delenv("SAMUS_KG_TIER_MODE", raising=False)
    reload_settings()
    yield
    monkeypatch.delenv("SAMUS_CRM_HIVEMIND_PROJECTION_ENABLED", raising=False)
    reload_settings()


# ---------------------------------------------------------------------------
# Flag gating (default ON; explicit-off is a clean no-op)
# ---------------------------------------------------------------------------


def test_flag_explicitly_off_is_noop(monkeypatch):
    monkeypatch.setenv("SAMUS_CRM_HIVEMIND_PROJECTION_ENABLED", "false")
    reload_settings()
    fake = _FakeClient(available=True)

    result = hp.project_opportunity(_opp(), client=fake)

    assert result["reason"] == hp.SKIPPED_FLAG_OFF
    assert fake.node_calls == []
    assert fake.rel_calls == []
    monkeypatch.delenv("SAMUS_CRM_HIVEMIND_PROJECTION_ENABLED", raising=False)
    reload_settings()


def test_projection_enabled_by_default(monkeypatch):
    monkeypatch.delenv("SAMUS_CRM_HIVEMIND_PROJECTION_ENABLED", raising=False)
    reload_settings()
    assert hp.projection_enabled() is True
    monkeypatch.setenv("SAMUS_CRM_HIVEMIND_PROJECTION_ENABLED", "false")
    reload_settings()
    assert hp.projection_enabled() is False
    monkeypatch.delenv("SAMUS_CRM_HIVEMIND_PROJECTION_ENABLED", raising=False)
    reload_settings()


# ---------------------------------------------------------------------------
# Local-default degrade (Neo4j down)
# ---------------------------------------------------------------------------


def test_armed_but_graph_unavailable_is_noop(_armed):
    fake = _FakeClient(available=False)

    result = hp.project_opportunity(_opp(), client=fake)

    assert result["reason"] == hp.SKIPPED_GRAPH_UNAVAILABLE
    assert fake.node_calls == []
    assert fake.rel_calls == []


# ---------------------------------------------------------------------------
# Happy path — full sub-graph projected
# ---------------------------------------------------------------------------


def test_projects_full_subgraph_when_armed(_armed):
    fake = _FakeClient(available=True)
    prospect = Prospect(prospect_id="pr_1", company_name="Acme LLC")
    contact = Contact(
        contact_id="co_1", prospect_id="pr_1", name="Jane", email="jane@acme.test", role="Owner"
    )

    result = hp.project_opportunity(
        _opp(),
        prospect=prospect,
        contact=contact,
        client=fake,
    )

    assert result["reason"] == hp.PROJECTED
    labels = [c[0] for c in fake.node_calls]
    assert labels == ["Prospect", "Opportunity", "Contact"]

    # Opportunity node carries the pipeline stage + probability.
    opp_props = next(p for lbl, p in fake.node_calls if lbl == "Opportunity")
    assert opp_props["opportunity_id"] == "op_1"
    assert opp_props["stage"] == "qualified"
    assert opp_props["close_probability"] == 0.25
    assert opp_props["deal_size_usd"] == 1200.0

    # Prospect node carries enrichment.
    pr_props = next(p for lbl, p in fake.node_calls if lbl == "Prospect")
    assert pr_props["company_name"] == "Acme LLC"

    # Three allowlisted edges: prospect->opp, prospect->contact, contact->opp.
    assert ("Prospect", "pr_1", "HAS_OPPORTUNITY", "Opportunity", "op_1") in fake.rel_calls
    assert ("Prospect", "pr_1", "HAS_CONTACT", "Contact", "co_1") in fake.rel_calls
    assert ("Contact", "co_1", "PARTICIPATES_IN", "Opportunity", "op_1") in fake.rel_calls
    assert len(fake.rel_calls) == 3


def test_projects_without_contact_when_none(_armed):
    fake = _FakeClient(available=True)

    result = hp.project_opportunity(_opp(contact_id=""), client=fake)

    assert result["reason"] == hp.PROJECTED
    labels = [c[0] for c in fake.node_calls]
    assert labels == ["Prospect", "Opportunity"]
    # Only the prospect->opportunity edge.
    assert fake.rel_calls == [
        ("Prospect", "pr_1", "HAS_OPPORTUNITY", "Opportunity", "op_1"),
    ]


def test_no_prospect_id_skips(_armed):
    fake = _FakeClient(available=True)

    result = hp.project_opportunity(_opp(prospect_id="", contact_id=""), client=fake)

    assert result["reason"] == hp.SKIPPED_NO_PROSPECT
    assert fake.node_calls == []


# ---------------------------------------------------------------------------
# Tier stamping follows SAMUS_KG_TIER_MODE
# ---------------------------------------------------------------------------


def test_tier_property_written_only_in_label_mode(monkeypatch):
    monkeypatch.setenv("SAMUS_CRM_HIVEMIND_PROJECTION_ENABLED", "true")
    monkeypatch.setenv("SAMUS_KG_TIER_MODE", "label")
    reload_settings()
    fake = _FakeClient(available=True)

    hp.project_opportunity(_opp(contact_id=""), client=fake)

    for _lbl, props in fake.node_calls:
        assert props.get("tier") == "private"

    monkeypatch.delenv("SAMUS_KG_TIER_MODE", raising=False)
    monkeypatch.delenv("SAMUS_CRM_HIVEMIND_PROJECTION_ENABLED", raising=False)
    reload_settings()


# ---------------------------------------------------------------------------
# Best-effort: never raises
# ---------------------------------------------------------------------------


def test_never_raises_on_graph_error(_armed):
    fake = _FakeClient(available=True, raise_on_node=True)

    result = hp.project_opportunity(_opp(), client=fake)

    assert result["reason"] == hp.ERROR
    # Did not propagate the exception.


# ---------------------------------------------------------------------------
# Conversation projection (prospect -> contact -> conversation)
# ---------------------------------------------------------------------------


def _conv(**over: Any) -> Conversation:
    base: dict[str, Any] = dict(
        conversation_id="cv_1",
        prospect_id="pr_1",
        contact_id="co_1",
        channel="call",
        status="completed",
        direction="outbound",
        outcome="follow_up",
        started_at="2026-07-06T10:00:00Z",
    )
    base.update(over)
    return Conversation(**base)


def test_conversation_flag_explicitly_off_is_noop(monkeypatch):
    monkeypatch.setenv("SAMUS_CRM_HIVEMIND_PROJECTION_ENABLED", "false")
    reload_settings()
    fake = _FakeClient(available=True)

    result = hp.project_conversation(_conv(), client=fake)

    assert result["reason"] == hp.SKIPPED_FLAG_OFF
    assert fake.node_calls == []
    monkeypatch.delenv("SAMUS_CRM_HIVEMIND_PROJECTION_ENABLED", raising=False)
    reload_settings()


def test_conversation_armed_but_graph_unavailable_is_noop(_armed):
    fake = _FakeClient(available=False)

    result = hp.project_conversation(_conv(), client=fake)

    assert result["reason"] == hp.SKIPPED_GRAPH_UNAVAILABLE
    assert fake.node_calls == []
    assert fake.rel_calls == []


def test_projects_conversation_subgraph_when_armed(_armed):
    fake = _FakeClient(available=True)
    prospect = Prospect(prospect_id="pr_1", company_name="Acme LLC")
    contact = Contact(
        contact_id="co_1", prospect_id="pr_1", name="Jane", email="jane@acme.test", role="Owner"
    )

    result = hp.project_conversation(
        _conv(),
        prospect=prospect,
        contact=contact,
        client=fake,
    )

    assert result["reason"] == hp.PROJECTED
    labels = [c[0] for c in fake.node_calls]
    assert labels == ["Prospect", "Conversation", "Contact"]

    # Conversation node carries channel / status / outcome / direction.
    cv_props = next(p for lbl, p in fake.node_calls if lbl == "Conversation")
    assert cv_props["conversation_id"] == "cv_1"
    assert cv_props["channel"] == "call"
    assert cv_props["status"] == "completed"
    assert cv_props["direction"] == "outbound"
    assert cv_props["outcome"] == "follow_up"
    # Long free-text is NOT projected (topology mirror, not document store).
    assert "transcript" not in cv_props
    assert "summary" not in cv_props

    # Three allowlisted edges: prospect->conv, prospect->contact, contact->conv.
    assert ("Prospect", "pr_1", "HAS_CONVERSATION", "Conversation", "cv_1") in fake.rel_calls
    assert ("Prospect", "pr_1", "HAS_CONTACT", "Contact", "co_1") in fake.rel_calls
    assert ("Contact", "co_1", "PARTICIPATES_IN", "Conversation", "cv_1") in fake.rel_calls
    assert len(fake.rel_calls) == 3


def test_projects_conversation_without_contact(_armed):
    fake = _FakeClient(available=True)

    result = hp.project_conversation(_conv(contact_id=""), client=fake)

    assert result["reason"] == hp.PROJECTED
    labels = [c[0] for c in fake.node_calls]
    assert labels == ["Prospect", "Conversation"]
    assert fake.rel_calls == [
        ("Prospect", "pr_1", "HAS_CONVERSATION", "Conversation", "cv_1"),
    ]


def test_conversation_no_prospect_id_skips(_armed):
    fake = _FakeClient(available=True)

    result = hp.project_conversation(_conv(prospect_id="", contact_id=""), client=fake)

    assert result["reason"] == hp.SKIPPED_NO_PROSPECT
    assert fake.node_calls == []


def test_conversation_never_raises_on_graph_error(_armed):
    fake = _FakeClient(available=True, raise_on_node=True)

    result = hp.project_conversation(_conv(), client=fake)

    assert result["reason"] == hp.ERROR
