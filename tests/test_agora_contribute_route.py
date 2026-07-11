"""POST /agora/contribute — Samus's Agora-v2 contribution endpoint.

Proves Samus returns its uniquely-held commercial/resource evidence + a
deterministic stance for an HMAC-verified Anita AgentEnvelope, fail-closed on a
non-Anita sender / tampered payload, and an honest inadmissible contribution when
the flag is off. Uses the real shared security_client envelope (wire-compatible
with Anita's HmacHttpContributor).
"""
import sys
from pathlib import Path

# Make `security_client` (in repo-root _shared) importable for envelope creation.
for _up in range(2, 9):
    try:
        _cand = Path(__file__).resolve().parents[_up] / "_shared"
    except IndexError:
        break
    if (_cand / "security_client" / "agent_envelope.py").exists():
        if str(_cand) not in sys.path:
            sys.path.insert(0, str(_cand))
        break

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from security_client import thumbprint
from security_client.agent_envelope import AgentEnvelope, _replay_cache_clear_for_testing
from security_client.rotating_hmac import RotatingHMACKey

from backend.standard.inter_agent.agora_contribute_route import SOURCE_KIND, register

_ANITA_KEY = "ab" * 32


@pytest.fixture(autouse=True)
def _hmac_env(monkeypatch):
    monkeypatch.setenv("SS_HMAC_KEY_ANITA", _ANITA_KEY)
    _replay_cache_clear_for_testing()
    thumbprint.reset_thumbprint_for_testing()
    yield
    _replay_cache_clear_for_testing()
    thumbprint.reset_thumbprint_for_testing()


def _client() -> TestClient:
    app = FastAPI()
    register(app)
    return TestClient(app)


def _anita_envelope(*, topic="GPU serialization protocol", deliberation_id="d-1", round_=1):
    thumbprint.reset_thumbprint_for_testing()
    thumbprint.init_thumbprint("anita")
    key = RotatingHMACKey.for_agent("anita")
    env = AgentEnvelope.create(
        from_agent="anita", to_agent="samus",
        payload={"deliberation_id": deliberation_id, "topic": topic, "round": round_},
        signing_key=key,
    )
    return env.to_wire()


def test_enabled_returns_commercial_evidence(monkeypatch):
    monkeypatch.setenv("SN_AGORA_CONTRIBUTE_ENABLED", "true")
    r = _client().post("/agora/contribute", json=_anita_envelope())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["agent"] == "samus"
    assert body["source_kind"] == SOURCE_KIND
    ev = body["evidence_payload"]
    assert ev["have_evidence"] is True
    assert ev["local_gpu_contender"] is False     # Samus's distinct evidence
    assert ev["inference_path"] == "anthropic-only"
    assert len(body["evidence_hash"]) == 64
    assert "contend for the local 4090" in body["stance"]


def test_disabled_returns_honest_inadmissible(monkeypatch):
    monkeypatch.delenv("SN_AGORA_CONTRIBUTE_ENABLED", raising=False)
    r = _client().post("/agora/contribute", json=_anita_envelope())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["agent"] == "samus"
    assert body["evidence_payload"]["have_evidence"] is False
    assert body["evidence_payload"]["reason"] == "contribute_disabled"


def test_non_anita_sender_is_403(monkeypatch):
    monkeypatch.setenv("SN_AGORA_CONTRIBUTE_ENABLED", "true")
    monkeypatch.setenv("SS_HMAC_KEY_MAJOR", "cd" * 32)
    thumbprint.reset_thumbprint_for_testing()
    thumbprint.init_thumbprint("major")
    key = RotatingHMACKey.for_agent("major")
    env = AgentEnvelope.create(from_agent="major", to_agent="samus",
                               payload={"topic": "x"}, signing_key=key)
    r = _client().post("/agora/contribute", json=env.to_wire())
    assert r.status_code == 403


def test_tampered_payload_is_rejected(monkeypatch):
    monkeypatch.setenv("SN_AGORA_CONTRIBUTE_ENABLED", "true")
    wire = _anita_envelope()
    wire["payload"] = {"topic": "INJECTED", "round": 99}  # break the HMAC-covered body
    r = _client().post("/agora/contribute", json=wire)
    assert r.status_code in (400, 403)


def test_missing_envelope_is_401():
    r = _client().post("/agora/contribute", json={"not": "an envelope"})
    assert r.status_code in (401, 403)
