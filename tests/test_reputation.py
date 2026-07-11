"""Per-workcell reputation + economics (HOTL Tranche 5, deliverable 4).

Verification target from the plan: reputation table populates from a test run.
"""

from __future__ import annotations

import json

import pytest


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("SAMUS_BUSINESS_EVENTS_PATH", str(tmp_path / "events.jsonl"))
    monkeypatch.setenv("SAMUS_DLQ_ROOT", str(tmp_path / "dlq"))
    monkeypatch.setenv("SAMUS_REPUTATION_PATH", str(tmp_path / "rep.json"))
    monkeypatch.setenv("SAMUS_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.setenv("SAMUS_ROI_ROLLUP_PATH", str(tmp_path / "roi.json"))
    monkeypatch.setenv("DDB_PORTFOLIO_SNAPSHOTS_TABLE", "")  # JSON-only in tests
    return tmp_path


def _seed_events():
    from backend.common.business_events import (
        CALL_PLACED,
        EMAIL_SENT,
        emit_business_event,
    )

    # 3 successful outreach sends.
    for i in range(3):
        emit_business_event(EMAIL_SENT, workcell="outreach", prospect_id=f"p{i}")
    # 1 successful voice call.
    emit_business_event(CALL_PLACED, workcell="voice", prospect_id="pv")


def test_reputation_populates_from_run(_isolate, monkeypatch):
    from backend.common import reputation

    _seed_events()
    # A DLQ failure against outreach drags its success_rate below 1.0.
    from backend.common import dlq

    dlq.enqueue_failure(
        "outreach", task_id="t-f", target="outreach", payload={}, error="boom", attempt=1
    )

    table = reputation.compute_reputation()
    assert "outreach" in table and "voice" in table

    out = table["outreach"]
    # 3 successes / (3 successes + 1 failure) = 0.75.
    assert out.success_rate == pytest.approx(0.75, abs=1e-6)
    assert out.sample_size >= 4

    voice = table["voice"]
    assert voice.success_rate == pytest.approx(1.0)  # 1 success, no failures

    # Composite score is in [0, 1] and reflects the lower outreach success_rate.
    assert 0.0 <= out.score <= 1.0
    assert out.score < voice.score


def test_reputation_reliability_from_autotuner(_isolate, monkeypatch):
    from backend.common import reputation

    # Seed an autotuner state file with a nonzero error EMA.
    from backend.common.state_paths import state_path

    p = state_path("autonomy", "autotuner_state.json")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"samples": 10, "error_rate_ema": 0.2}), encoding="utf-8")

    rel = reputation._reliability_from_autotuner()
    assert rel == pytest.approx(0.8)  # 1 - 0.2

    _seed_events()
    table = reputation.compute_reputation()
    assert table["outreach"].reliability == pytest.approx(0.8)


def test_reputation_reliability_defaults_high_without_samples(_isolate):
    from backend.common import reputation

    # No autotuner state -> reliability 1.0 (no evidence of unreliability).
    assert reputation._reliability_from_autotuner() == pytest.approx(1.0)


def test_reputation_profitability_from_roi(_isolate, monkeypatch):
    from backend.common import reputation

    _seed_events()
    # Stub the ROI rollup to attribute net profit to outreach.
    import backend.finance.roi as roi

    monkeypatch.setattr(
        roi,
        "get_rollup",
        lambda day=None: {"by_workcell": {"outreach": {"net_usd": 42.0}}},
    )
    table = reputation.compute_reputation()
    assert table["outreach"].profitability_usd == pytest.approx(42.0)


def test_reputation_accuracy_credits_self_caught_blocks(_isolate):
    from backend.common import reputation
    from backend.common.business_events import DECISION_MADE, emit_business_event

    # A block decision is a correct self-assessment -> accuracy 1.0.
    emit_business_event(
        DECISION_MADE, workcell="outreach", metadata={"decision": "send_cap_blocked", "cap": 1}
    )
    table = reputation.compute_reputation()
    assert table["outreach"].accuracy == pytest.approx(1.0)


def test_reputation_persists_and_reloads(_isolate):
    from backend.common import reputation

    _seed_events()
    out = reputation.get_reputation(recompute=True)
    assert "workcells" in out and "outreach" in out["workcells"]

    # Second read (no recompute) serves the persisted table.
    cached = reputation.load_reputation()
    assert cached is not None
    assert (
        cached["workcells"]["outreach"]["success_rate"]
        == out["workcells"]["outreach"]["success_rate"]
    )


# ---------------------------------------------------------------------------
# admin route
# ---------------------------------------------------------------------------


def test_admin_reputation_route(_isolate, monkeypatch):
    _seed_events()
    monkeypatch.setenv("SAMUS_SHARED_HMAC_KEY", "test-hmac-key")
    from backend.common.settings import reload_settings

    reload_settings()

    from fastapi.testclient import TestClient
    from backend.gateway import sqs_dispatch
    from backend.gateway.app import create_app

    sqs_dispatch.reload_queue_urls()
    client = TestClient(create_app())

    resp = client.get("/admin/reputation?recompute=true")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert "outreach" in body["reputation"]["workcells"]
