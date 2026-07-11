"""Tests for the Samus cross-agent Quorum VOTE protocol (voter side).

Two layers:

* :func:`decide_ballot` — the pure 3-axis policy logic. Asserts the
  fail-closed aggregation: any harm/high → REJECT; clean all-good → APPROVE;
  mixed → ABSTAIN; opaque/unknown → never APPROVE.
* ``POST /quorum/vote`` — the route boundary contract. Asserts the dormant
  gate (503 when the flag is unset), the fail-closed HMAC envelope verify
  (403 on a missing/forged/non-major envelope), and the happy path (200 +
  well-formed ballot when a valid Major envelope arrives with the flag on).

The route tests mount ``register_quorum_vote_route`` on a bare FastAPI app
(no full gateway boot) so the boundary contract is exercised in isolation —
mirroring the isolation philosophy of ``test_broker_client.py``. Major
envelopes are signed for real with the shared ``security_client`` exactly as
``test_broker_client._broker_enabled_with_key`` signs Samus envelopes.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.standard.inter_agent.quorum_voter import decide_ballot
from backend.standard.inter_agent.quorum_vote_route import (
    ENV_VOTING_ENABLED,
    register as register_quorum_vote_route,
)


# Put _shared on the path so we can sign real Major envelopes in the route
# tests (mirrors tests/test_broker_client.py's bootstrap).
_REPO_ROOT = Path(__file__).resolve().parents[2]
_SHARED = _REPO_ROOT / "_shared"
if (_SHARED / "security_client" / "agent_envelope.py").exists():
    if str(_SHARED) not in sys.path:
        sys.path.insert(0, str(_SHARED))


# Deterministic 64-char hex key for the Major signer in route tests.
_FAKE_MAJOR_KEY = "b" * 64


def _proposal(**overrides):
    """A minimal well-formed CROSS_AGENT proposal; override per test."""
    base = {
        "proposal_id": "p-test-001",
        "proposer": "darwin",
        "classification": "CROSS_AGENT",
        "impact": "LOW",
        "target_agent": "samus",
        "action": "no-op",
        "payload": {},
        "rationale": "",
        "ts": 0.0,
    }
    base.update(overrides)
    return base


# ===========================================================================
# decide_ballot — pure policy logic
# ===========================================================================


def test_ballot_shape_is_well_formed():
    """The ballot carries exactly the spec's keys + method=='policy'."""
    b = decide_ballot(_proposal())
    assert b["voter"] == "samus"
    assert b["proposal_id"] == "p-test-001"
    assert b["vote"] in ("APPROVE", "REJECT", "ABSTAIN")
    assert 0.0 <= b["confidence"] <= 1.0
    assert b["self_benefit"] in ("benefit", "neutral", "harm")
    assert b["ecosystem_benefit"] in ("benefit", "neutral", "harm")
    assert b["abuse_risk"] in ("low", "medium", "high")
    assert isinstance(b["reasoning"], str) and b["reasoning"]
    assert b["method"] == "policy"


def test_harm_or_high_axis_rejects():
    """A privilege/security/secrets token drives abuse_risk=high → REJECT."""
    b = decide_ballot(
        _proposal(
            action="grant samus secret key scope to optimus",
            impact="HIGH",
        )
    )
    assert b["abuse_risk"] == "high"
    assert b["vote"] == "REJECT"
    # security-first: a high-abuse proposal is also harm to Samus's posture.
    assert b["self_benefit"] == "harm"


def test_disable_control_rejects():
    """'disable'/'bypass' a control → harm to ecosystem axioms → REJECT."""
    b = decide_ballot(
        _proposal(
            proposer="optimus",
            action="disable fail-closed auth check on shared broker",
        )
    )
    assert b["vote"] == "REJECT"
    assert b["ecosystem_benefit"] == "harm"


def test_all_good_approves():
    """self!=harm AND ecosystem=benefit AND abuse=low → APPROVE.

    A sandboxed, audit-strengthening change that does NOT touch any
    privilege/secret/Samus-revenue token: self=benefit, ecosystem=benefit,
    abuse=low.
    """
    b = decide_ballot(
        _proposal(
            proposer="darwin",
            impact="LOW",
            action="add sandboxed observability metrics audit dashboard",
            rationale="strengthens auditability and resilience across the mesh",
        )
    )
    assert b["abuse_risk"] == "low"
    assert b["self_benefit"] != "harm"
    assert b["ecosystem_benefit"] == "benefit"
    assert b["vote"] == "APPROVE"


def test_mixed_neutral_ecosystem_abstains():
    """A legible, benign-but-not-ecosystem-beneficial change → ABSTAIN.

    No harm/high signal, but ecosystem_benefit is only 'neutral' (no shared
    upside keyword), so it cannot reach APPROVE → ABSTAIN.
    """
    b = decide_ballot(
        _proposal(
            proposer="anita",
            impact="LOW",
            action="rename internal widget alpha to beta",
            rationale="cosmetic relabel",
        )
    )
    assert b["abuse_risk"] == "low"
    assert b["self_benefit"] == "neutral"
    assert b["ecosystem_benefit"] == "neutral"
    assert b["vote"] == "ABSTAIN"


def test_opaque_proposal_never_approves():
    """An empty / illegible proposal cannot certify low abuse risk → not APPROVE."""
    b = decide_ballot({"proposal_id": "p-opaque"})
    assert b["vote"] != "APPROVE"
    # opaque ⇒ cannot certify low ⇒ abuse_risk is at least medium.
    assert b["abuse_risk"] in ("medium", "high")


def test_malformed_proposal_fails_closed():
    """A non-dict proposal is coerced to empty and never APPROVES."""
    b = decide_ballot(None)  # type: ignore[arg-type]
    assert b["voter"] == "samus"
    assert b["vote"] != "APPROVE"
    assert b["proposal_id"] == ""


def test_samus_revenue_surface_reach_is_harm():
    """An external reach into Samus's customer/outreach surface → self=harm → not APPROVE."""
    b = decide_ballot(
        _proposal(
            proposer="optimus",
            action="read samus customer outreach campaign pipeline",
            impact="MEDIUM",
        )
    )
    assert b["self_benefit"] == "harm"
    assert b["vote"] == "REJECT"


def test_high_impact_alone_does_not_force_approve():
    """HIGH impact raises abuse_risk to at least medium → blocks APPROVE."""
    b = decide_ballot(
        _proposal(
            proposer="darwin",
            impact="HIGH",
            action="restructure shared coordination layer",
            rationale="improves interop and resilience",
        )
    )
    # ecosystem may read 'benefit' (interop/resilience) but abuse_risk>=medium
    # because impact==HIGH, so APPROVE (which needs abuse=low) is impossible.
    assert b["abuse_risk"] != "low"
    assert b["vote"] != "APPROVE"


# ===========================================================================
# Route — dormant gate + HMAC fail-closed + happy path
# ===========================================================================


def _make_app() -> FastAPI:
    app = FastAPI()
    register_quorum_vote_route(app)
    return app


@pytest.fixture()
def _reset_envelope_state():
    """Reset thumbprint + replay cache so each route test starts clean."""
    from security_client import agent_envelope as _ae
    from security_client import thumbprint as _tp

    _ae._replay_cache_clear_for_testing()  # noqa: SLF001
    _tp.reset_thumbprint_for_testing()
    yield
    _ae._replay_cache_clear_for_testing()  # noqa: SLF001
    _tp.reset_thumbprint_for_testing()


def _sign_major_envelope(payload: dict, *, from_agent: str = "major") -> dict:
    """Sign a wire AgentEnvelope as ``from_agent`` → samus. Requires the
    matching thumbprint to be initialised by the caller."""
    from security_client.agent_envelope import AgentEnvelope
    from security_client.rotating_hmac import RotatingHMACKey

    key = RotatingHMACKey.for_agent(from_agent)
    env = AgentEnvelope.create(
        from_agent=from_agent,
        to_agent="samus",
        payload=payload,
        signing_key=key,
    )
    return env.to_wire()


# ----- dormant gate -----


def test_route_dormant_returns_503_when_flag_unset(monkeypatch):
    """Flag unset → 503 BEFORE any work (even with no body)."""
    monkeypatch.delenv(ENV_VOTING_ENABLED, raising=False)
    client = _make_app()
    resp = TestClient(client).post("/quorum/vote", json={"anything": True})
    assert resp.status_code == 503
    detail = resp.json()["detail"]
    assert detail["error"] == "quorum_voting_dormant"


# ----- HMAC fail-closed -----


def test_route_missing_envelope_is_rejected_not_200(monkeypatch, _reset_envelope_state):
    """Flag ON but a non-envelope body (no signature) → 4xx, never 200."""
    monkeypatch.setenv(ENV_VOTING_ENABLED, "1")
    monkeypatch.setenv("SS_HMAC_KEY_MAJOR", _FAKE_MAJOR_KEY)
    client = TestClient(_make_app())
    resp = client.post("/quorum/vote", json={"not": "an envelope"})
    assert resp.status_code in (401, 403)
    assert resp.status_code != 200


def test_route_forged_signature_is_rejected(monkeypatch, _reset_envelope_state):
    """An envelope signed with the WRONG key fails verification → 403."""
    monkeypatch.setenv(ENV_VOTING_ENABLED, "1")
    # The route verifies with _FAKE_MAJOR_KEY...
    monkeypatch.setenv("SS_HMAC_KEY_MAJOR", _FAKE_MAJOR_KEY)
    client = TestClient(_make_app())

    # ...but we sign with a DIFFERENT key for 'major'. Init the major
    # thumbprint, then craft a wire envelope whose signature won't match.
    from security_client import thumbprint as _tp

    _tp.init_thumbprint("major")
    from security_client.agent_envelope import AgentEnvelope
    from security_client.rotating_hmac import RotatingHMACKey

    wrong_key = RotatingHMACKey("c" * 64, agent_id="major")
    env = AgentEnvelope.create(
        from_agent="major",
        to_agent="samus",
        payload=_proposal(),
        signing_key=wrong_key,
    )
    resp = client.post("/quorum/vote", json=env.to_wire())
    assert resp.status_code == 403


def test_route_non_major_sender_is_rejected(monkeypatch, _reset_envelope_state):
    """A correctly-signed envelope from a NON-collector agent → 403."""
    monkeypatch.setenv(ENV_VOTING_ENABLED, "1")
    monkeypatch.setenv("SS_HMAC_KEY_MAJOR", _FAKE_MAJOR_KEY)
    monkeypatch.setenv("SS_HMAC_KEY_DARWIN", _FAKE_MAJOR_KEY)
    client = TestClient(_make_app())

    from security_client import thumbprint as _tp

    _tp.init_thumbprint("darwin")
    wire = _sign_major_envelope(_proposal(), from_agent="darwin")
    resp = client.post("/quorum/vote", json=wire)
    assert resp.status_code == 403


def test_route_key_unprovisioned_returns_503(monkeypatch, _reset_envelope_state):
    """Flag ON but Major's verifying key is unset → 503 (ops gap, fail-closed)."""
    monkeypatch.setenv(ENV_VOTING_ENABLED, "1")
    monkeypatch.delenv("SS_HMAC_KEY_MAJOR", raising=False)
    monkeypatch.delenv("MAJOR_AGENT_HMAC_SECRET", raising=False)
    client = TestClient(_make_app())
    # Even a structurally-valid-looking envelope can't be verified with no key.
    resp = client.post(
        "/quorum/vote",
        json={
            "version": 1,
            "from_agent": "major",
            "to_agent": "samus",
            "ts": 0.0,
            "nonce": "x",
            "fingerprint": "y",
            "payload": {},
            "epoch": 0,
            "signature": "z",
        },
    )
    assert resp.status_code == 503


# ----- happy path -----


def test_route_valid_major_envelope_returns_ballot(monkeypatch, _reset_envelope_state):
    """Flag ON + valid Major envelope → 200 + well-formed ballot."""
    monkeypatch.setenv(ENV_VOTING_ENABLED, "1")
    monkeypatch.setenv("SS_HMAC_KEY_MAJOR", _FAKE_MAJOR_KEY)
    client = TestClient(_make_app())

    from security_client import thumbprint as _tp

    _tp.init_thumbprint("major")

    proposal = _proposal(
        proposer="darwin",
        action="add sandboxed observability metrics audit dashboard",
        rationale="strengthens auditability and resilience",
        impact="LOW",
    )
    wire = _sign_major_envelope(proposal)
    resp = client.post("/quorum/vote", json=wire)
    assert resp.status_code == 200
    ballot = resp.json()
    assert ballot["voter"] == "samus"
    assert ballot["proposal_id"] == proposal["proposal_id"]
    assert ballot["method"] == "policy"
    assert ballot["vote"] in ("APPROVE", "REJECT", "ABSTAIN")
    assert ballot["self_benefit"] in ("benefit", "neutral", "harm")
    assert ballot["ecosystem_benefit"] in ("benefit", "neutral", "harm")
    assert ballot["abuse_risk"] in ("low", "medium", "high")


def test_route_valid_envelope_harm_proposal_votes_reject(monkeypatch, _reset_envelope_state):
    """End-to-end: a privilege-escalation proposal → 200 with vote==REJECT."""
    monkeypatch.setenv(ENV_VOTING_ENABLED, "1")
    monkeypatch.setenv("SS_HMAC_KEY_MAJOR", _FAKE_MAJOR_KEY)
    client = TestClient(_make_app())

    from security_client import thumbprint as _tp

    _tp.init_thumbprint("major")

    proposal = _proposal(
        proposer="optimus",
        action="grant unrestricted admin scope over samus secrets",
        impact="HIGH",
    )
    wire = _sign_major_envelope(proposal)
    resp = client.post("/quorum/vote", json=wire)
    assert resp.status_code == 200
    assert resp.json()["vote"] == "REJECT"
