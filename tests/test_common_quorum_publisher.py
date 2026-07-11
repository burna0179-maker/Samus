"""Tests for backend.common.quorum_publisher — shape conversion + fail-open."""
from __future__ import annotations

import datetime as _dt
from unittest.mock import MagicMock

import pytest

from backend.common import quorum_client as qc
from backend.common import quorum_publisher as qp


@pytest.fixture
def fake_client(monkeypatch):
    """Replace the singleton with a MagicMock whose .publish returns True."""
    mock = MagicMock()
    mock.publish.return_value = True
    qc._reset_for_tests()
    monkeypatch.setattr(qp, "get_quorum_client", lambda: mock)
    return mock


def _sample_veto() -> dict:
    return {
        "veto_id": "v-1234",
        "ts": _dt.datetime.utcnow().isoformat() + "Z",
        "proposing_agent": "samus",
        "proposed_action": {"kind": "send_email", "body": {}},
        "inviolable_axioms_breached": [
            "axiom.inviolable.no_unconsented_influence",
            "axiom.inviolable.counterparty_data_minimum",
        ],
        "escalation": "gate_only",
    }


def _sample_finding(verdict: str = "BLOCK") -> dict:
    return {
        "finding_id": "f-9999",
        "ts": _dt.datetime.utcnow().isoformat() + "Z",
        "verdict": verdict,
        "proposed_action": {"kind": "send_email", "proposing_agent": "samus"},
        "efh_veto": {"veto_id": "v-1"} if verdict == "BLOCK" else None,
        "confusion": {"score": 0.55, "grade": "C"},
        "elegance": {"score": 0.3, "grade": "D"},
        "adversarial": {"classified": False, "target_is_internal": False},
        "rationale": ["confusion elevated (0.55)", "elegance below floor (0.30)"],
    }


# ---------------------------------------------------------------------------
# publish_efh_veto
# ---------------------------------------------------------------------------

def test_publish_efh_veto_maps_to_governance_publish(fake_client):
    veto = _sample_veto()
    assert qp.publish_efh_veto(veto) is True
    args = fake_client.publish.call_args.kwargs
    assert args["caller"] == "samus"
    assert args["action"] == "efh_veto"
    assert args["risk_score"] == 1.0
    assert args["approved"] is False
    assert args["approval_score"] == 0.0
    assert args["threshold"] == 1.0
    assert args["votes"] == [{"voter": "efh", "vote": "VETO", "weight": 1.0}]
    assert "v-1234" in args["reason"]
    assert "axiom.inviolable.no_unconsented_influence" in args["reason"]


def test_publish_efh_veto_missing_veto_id_skips(fake_client):
    assert qp.publish_efh_veto({"foo": "bar"}) is False
    fake_client.publish.assert_not_called()


def test_publish_efh_veto_fails_open_on_client_exception(fake_client):
    fake_client.publish.side_effect = RuntimeError("hub on fire")
    assert qp.publish_efh_veto(_sample_veto()) is False  # does not raise


# ---------------------------------------------------------------------------
# publish_pdc_finding
# ---------------------------------------------------------------------------

def test_publish_pdc_finding_skips_pass(fake_client):
    assert qp.publish_pdc_finding(_sample_finding("PASS")) is False
    fake_client.publish.assert_not_called()


def test_publish_pdc_finding_skips_review(fake_client):
    assert qp.publish_pdc_finding(_sample_finding("REVIEW")) is False
    fake_client.publish.assert_not_called()


def test_publish_pdc_finding_publishes_block(fake_client):
    assert qp.publish_pdc_finding(_sample_finding("BLOCK")) is True
    args = fake_client.publish.call_args.kwargs
    assert args["action"] == "pdc_finding"
    assert args["risk_score"] == 0.95
    assert args["approved"] is False
    assert args["caller"] == "samus"
    # Votes should include efh + confusion + elegance (all fired)
    voter_set = {v["voter"] for v in args["votes"]}
    assert "efh" in voter_set
    assert "confusion_meter" in voter_set
    assert "elegance_scorer" in voter_set
    assert "f-9999" in args["reason"]


def test_publish_pdc_finding_publishes_escalate(fake_client):
    finding = _sample_finding("ESCALATE")
    finding["adversarial"] = {
        "classified": True,
        "target_is_internal": False,
        "criterion_id": "intrusion_signature_match",
    }
    assert qp.publish_pdc_finding(finding) is True
    args = fake_client.publish.call_args.kwargs
    assert args["risk_score"] == 0.7
    assert any(v["voter"] == "adversarial_actor" and v["vote"] == "ESCALATE"
               for v in args["votes"])


def test_publish_pdc_finding_internal_adversarial_votes_block(fake_client):
    finding = _sample_finding("BLOCK")
    finding["adversarial"] = {
        "classified": True,
        "target_is_internal": True,
        "criterion_id": "intrusion_signature_match",
    }
    assert qp.publish_pdc_finding(finding) is True
    args = fake_client.publish.call_args.kwargs
    assert any(v["voter"] == "adversarial_actor" and v["vote"] == "BLOCK"
               for v in args["votes"])


def test_publish_pdc_finding_accepts_dataclass_with_to_dict(fake_client):
    class _Fake:
        def to_dict(self):
            return _sample_finding("BLOCK")

    assert qp.publish_pdc_finding(_Fake()) is True


def test_publish_pdc_finding_fails_open_on_client_exception(fake_client):
    fake_client.publish.side_effect = RuntimeError("hub on fire")
    assert qp.publish_pdc_finding(_sample_finding("BLOCK")) is False


def test_publish_pdc_finding_synthetic_vote_when_no_signals(fake_client):
    """If a BLOCK arrives with no contributing signals, synthesize a vote."""
    finding = _sample_finding("BLOCK")
    finding["efh_veto"] = None
    finding["confusion"] = {"score": 0.0}
    finding["elegance"] = {"score": 0.9}
    finding["adversarial"] = {"classified": False}
    assert qp.publish_pdc_finding(finding) is True
    args = fake_client.publish.call_args.kwargs
    assert args["votes"] == [{"voter": "pdc_composite", "vote": "BLOCK", "weight": 1.0}]
