"""D6-01 — Samus signs its /quorum/vote ballot so the collector can't forge it.

Samus is dormant (SAMUS_QUORUM_VOTING_ENABLED unset) AND not yet in the
operator-signed roster (admission is the operator U8 step), but the signing
path is wired now so it is live the moment Samus is admitted + enabled. These
tests exercise the best-effort signer directly.
"""
from __future__ import annotations

import backend.standard.inter_agent.quorum_vote_route as qv

# The route's shim adds the _shared DIR to sys.path so `security.*` imports.
qv._ensure_security_client_on_path()

from security.agent_keypair import AgentKeypair  # noqa: E402
from security.quorum_roster import AgentEntry, QuorumRoster  # noqa: E402
from security.quorum_vote_envelope import verify_vote_response  # noqa: E402

from cryptography.hazmat.primitives.asymmetric.ed25519 import (  # noqa: E402
    Ed25519PrivateKey,
)


def test_d6_01_signs_ballot_verifiable(monkeypatch):
    priv = Ed25519PrivateKey.generate()
    kp = AgentKeypair(agent_id="samus", private_key=priv, public_key=priv.public_key())
    monkeypatch.setattr(qv, "_kp_cache", {"loaded": True, "kp": kp})

    ballot = {
        "voter": "samus",
        "proposal_id": "p-sig-1",
        "vote": "ABSTAIN",
        "confidence": 0.5,
        "self_benefit": "neutral",
        "ecosystem_benefit": "neutral",
        "abuse_risk": "low",
        "reasoning": "n/a",
        "method": "policy",
    }
    signed = qv._sign_vote_response(ballot, "p-sig-1")
    assert "vote_envelope" in signed

    roster = QuorumRoster(
        version=1,
        agents={"samus": AgentEntry("samus", kp.public_hex, "https://127.0.0.1:0")},
        operator_breakglass_pubkey_hex="00" * 32,
        threshold_operational=2,
        threshold_elevated=3,
        signed_at="2026-06-07T00:00:00Z",
        signature="00",
    )
    payload = verify_vote_response(
        response=signed, roster=roster, expected_voter="samus", proposal_id="p-sig-1"
    )
    assert payload["vote"] == "ABSTAIN"
    assert payload["voter"] == "samus"


def test_d6_01_unsigned_when_no_key(monkeypatch):
    monkeypatch.setattr(qv, "_kp_cache", {"loaded": True, "kp": None})
    ballot = {"voter": "samus", "proposal_id": "p-x", "vote": "ABSTAIN"}
    signed = qv._sign_vote_response(ballot, "p-x")
    assert "vote_envelope" not in signed
