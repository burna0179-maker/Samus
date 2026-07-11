"""POST /inter_agent/reward-summary — Samus's W5 reward/harm readout endpoint.

Proves Samus returns an AGGREGATE reward/harm fitness summary for an
HMAC-verified Darwin AgentEnvelope, fail-closed on a non-Darwin sender / missing
envelope, and an honest no-signal readout when the flag is off. Uses the real
shared security_client envelope (wire-compatible with Darwin's HMAC client).
"""

import json
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

from backend.standard.inter_agent.reward_readout_route import SOURCE_KIND, register

_DARWIN_KEY = "ab" * 32


@pytest.fixture(autouse=True)
def _hmac_env(monkeypatch):
    monkeypatch.setenv("SS_HMAC_KEY_DARWIN", _DARWIN_KEY)
    _replay_cache_clear_for_testing()
    thumbprint.reset_thumbprint_for_testing()
    yield
    _replay_cache_clear_for_testing()
    thumbprint.reset_thumbprint_for_testing()


def _client() -> TestClient:
    app = FastAPI()
    register(app)
    return TestClient(app)


def _darwin_envelope(*, window=100, request_id="r-1"):
    thumbprint.reset_thumbprint_for_testing()
    thumbprint.init_thumbprint("darwin")
    key = RotatingHMACKey.for_agent("darwin")
    env = AgentEnvelope.create(
        from_agent="darwin",
        to_agent="samus",
        payload={"window": window, "request_id": request_id},
        signing_key=key,
    )
    return env.to_wire()


def _seed_ledger(monkeypatch, tmp_path, rows):
    path = tmp_path / "reward_computations.jsonl"
    with open(path, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    monkeypatch.setenv("SAMUS_REWARD_PERSIST_PATH", str(path))


def _row(reward, *, terminal_paid=0, retracted=0, unsubs=0, complaints=0, at="2026-06-04T10:00:00"):
    return {
        "opportunity_id": "op1",
        "reward": reward,
        "correlation_id": "c",
        "computed_at": at,
        "components": {
            "terminal_paid": terminal_paid,
            "retracted_claims": retracted,
            "unsubscribes": unsubs,
            "complaints": complaints,
            "harm_count": retracted + unsubs + complaints,
        },
    }


def test_enabled_returns_aggregate_summary(monkeypatch, tmp_path):
    monkeypatch.setenv("SN_REWARD_READOUT_ENABLED", "true")
    _seed_ledger(
        monkeypatch,
        tmp_path,
        [
            _row(1.0, terminal_paid=1, at="2026-06-04T09:00:00"),
            _row(0.0, unsubs=2, at="2026-06-04T11:00:00"),
            _row(0.5, complaints=1, at="2026-06-04T10:00:00"),
        ],
    )
    r = _client().post("/inter_agent/reward-summary", json=_darwin_envelope())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["agent"] == "samus" and body["source_kind"] == SOURCE_KIND
    s = body["summary"]
    assert s["have_signal"] is True
    assert s["window_n"] == 3
    assert s["mean_reward"] == pytest.approx(0.5)  # (1.0+0.0+0.5)/3
    assert s["min_reward"] == 0.0 and s["max_reward"] == 1.0
    assert s["terminal_paid"] == 1
    assert s["harm"]["unsubscribes"] == 2 and s["harm"]["complaints"] == 1
    assert s["harm_total"] == 2 + 1 + (2 + 1)  # unsubs + complaints + harm_count
    assert s["last_computed_at"] == "2026-06-04T11:00:00"
    assert len(body["summary_hash"]) == 64


def test_disabled_returns_honest_no_signal(monkeypatch, tmp_path):
    monkeypatch.delenv("SN_REWARD_READOUT_ENABLED", raising=False)
    _seed_ledger(monkeypatch, tmp_path, [_row(1.0)])
    r = _client().post("/inter_agent/reward-summary", json=_darwin_envelope())
    assert r.status_code == 200, r.text
    s = r.json()["summary"]
    assert s["have_signal"] is False and s["reason"] == "reward_readout_disabled"


def test_enabled_empty_ledger_is_no_reward_data(monkeypatch, tmp_path):
    monkeypatch.setenv("SN_REWARD_READOUT_ENABLED", "true")
    monkeypatch.setenv("SAMUS_REWARD_PERSIST_PATH", str(tmp_path / "missing.jsonl"))
    r = _client().post("/inter_agent/reward-summary", json=_darwin_envelope())
    assert r.status_code == 200, r.text
    s = r.json()["summary"]
    assert s["have_signal"] is False and s["reason"] == "no_reward_data"


def test_non_darwin_sender_is_403(monkeypatch):
    monkeypatch.setenv("SN_REWARD_READOUT_ENABLED", "true")
    monkeypatch.setenv("SS_HMAC_KEY_MAJOR", "cd" * 32)
    thumbprint.reset_thumbprint_for_testing()
    thumbprint.init_thumbprint("major")
    key = RotatingHMACKey.for_agent("major")
    env = AgentEnvelope.create(
        from_agent="major", to_agent="samus", payload={"window": 10}, signing_key=key
    )
    r = _client().post("/inter_agent/reward-summary", json=env.to_wire())
    assert r.status_code == 403


def test_missing_envelope_is_401(monkeypatch):
    monkeypatch.setenv("SN_REWARD_READOUT_ENABLED", "true")
    r = _client().post("/inter_agent/reward-summary", json={"not": "an envelope"})
    assert r.status_code in (401, 403)


def test_tampered_payload_is_rejected(monkeypatch):
    monkeypatch.setenv("SN_REWARD_READOUT_ENABLED", "true")
    wire = _darwin_envelope()
    wire["payload"] = {"window": 9999}  # break the HMAC-covered body
    r = _client().post("/inter_agent/reward-summary", json=wire)
    assert r.status_code in (400, 403)
