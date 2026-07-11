"""Samus inter_agent.relief forwarder — logic + pending-stake adapter.

Deterministic, no network / no envelope signing (post_fn faked). Proves the
forwarder gates on the flag, applies staleness, dedups across runs, is
best-effort on post failure, and that pending_stake_items() maps CRM
opportunities into the forwarder's item shape (with ISO created_at -> epoch).

Run (PARENT only — sub-agent pytest is sandbox-blocked):
    .venv\\Scripts\\python.exe -m pytest tests/test_relief_forwarder.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.standard.inter_agent.relief.forwarder import ReliefForwarder  # noqa: E402
from backend.standard.inter_agent.relief import task as relief_task  # noqa: E402


def _settings(**over):
    base = dict(
        samus_agora_relief_forward_enabled=True,
        samus_agora_relief_forward_staleness_sec=0,
        samus_agora_relief_forward_max_per_run=5,
    )
    base.update(over)
    return SimpleNamespace(**base)


def test_forwarder_disabled_is_noop(tmp_path):
    sent = []
    fwd = ReliefForwarder(
        agent_id="samus",
        pending_source=lambda: [{"ticket_id": "o1", "created_ts": 0.0}],
        post_fn=lambda r: sent.append(r) or True,
        settings=_settings(samus_agora_relief_forward_enabled=False),
        state_path=tmp_path / "f.json",
    )
    assert fwd.run_once()["ran"] is False
    assert sent == []


def test_forwarder_forwards_and_dedups(tmp_path):
    sent = []
    fwd = ReliefForwarder(
        agent_id="samus",
        pending_source=lambda: [
            {
                "ticket_id": "o1",
                "action": "sign_stake_sentence",
                "reason": "deal X",
                "created_ts": 0.0,
                "payload": {"stage": "new"},
            }
        ],
        post_fn=lambda r: sent.append(r) or True,
        settings=_settings(),
        state_path=tmp_path / "f.json",
        clock=lambda: 10_000.0,
    )
    assert fwd.run_once()["sent"] == 1
    assert sent[0]["origin_agent"] == "samus"
    assert sent[0]["remote_ticket_id"] == "o1"
    assert sent[0]["payload"]["stage"] == "new"
    assert fwd.run_once()["sent"] == 0  # persisted dedup


def test_forwarder_best_effort_retry(tmp_path):
    calls = {"n": 0}

    def _flaky(r):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("anita down")
        return True

    fwd = ReliefForwarder(
        agent_id="samus",
        pending_source=lambda: [{"ticket_id": "o1", "created_ts": 0.0}],
        post_fn=_flaky,
        settings=_settings(),
        state_path=tmp_path / "f.json",
        clock=lambda: 10_000.0,
    )
    assert fwd.run_once()["sent"] == 0
    assert fwd.run_once()["sent"] == 1


def test_iso_to_epoch():
    assert relief_task._iso_to_epoch("") is None
    assert relief_task._iso_to_epoch("not-a-date") is None
    v = relief_task._iso_to_epoch("2026-06-03T00:00:00Z")
    assert isinstance(v, float) and v > 0


def test_pending_stake_items_maps_opportunities(monkeypatch):
    opps = [
        SimpleNamespace(
            opportunity_id="op1",
            name="Acme",
            stage="new",
            created_at="2026-06-03T00:00:00Z",
            deal_size_usd=740.0,
            prospect_id="p1",
        ),
        SimpleNamespace(
            opportunity_id="",
            name="bad",
            stage="new",
            created_at="",
            deal_size_usd=0.0,
            prospect_id="",
        ),
    ]
    import backend.crm.service as crm_service

    monkeypatch.setattr(
        crm_service, "list_opportunities_pending_stake", lambda limit=50: opps, raising=False
    )
    items = relief_task.pending_stake_items()
    assert len(items) == 1  # blank id dropped
    it = items[0]
    assert it["ticket_id"] == "op1"
    assert it["action"] == "sign_stake_sentence"
    assert "Acme" in it["reason"]
    assert it["payload"]["deal_size_usd"] == 740.0
    assert isinstance(it["created_ts"], float)
