"""Tests for the entropy workcell.

Covers: /health smoke, /work + /entropy/scan routing, the deterministic
compute_entropy_score (exactness + clamping), and countermeasure mapping for
each of the five spec conditions.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.entropy.app import app
from backend.entropy import controller
from backend.entropy import service as svc_mod
from backend.entropy.controller import recommend_countermeasures
from backend.entropy.metrics import (
    ENTROPY_SCORE_MAX,
    ENTROPY_SCORE_MIN,
    EntropyInputs,
    compute_entropy_score,
)
from backend.entropy.monitor import run_monitor


@pytest.fixture(autouse=True)
def _no_ddb(monkeypatch):
    """Stub task-state persistence so tests never reach AWS."""
    monkeypatch.setattr(svc_mod, "write_task_state", lambda **_: True)


# ── HTTP: health + routing ───────────────────────────────────────────────────


def test_health():
    client = TestClient(app)
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["service"] == "entropy"


def test_work_route_returns_score_and_countermeasures():
    client = TestClient(app)
    r = client.post(
        "/work",
        json={
            "task_id": "t-1",
            "payload": {
                "queue_variance": 0.8,
                "error_velocity": 0.2,
                "task_retry_rate": 0.5,
                "llm_failure_ratio": 0.1,
            },
            "metadata": {"action": "entropy_scan"},
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["task_id"] == "t-1"
    # 0.8*0.25 + 0.2*0.30 + 0.5*0.20 + 0.1*0.25 = 0.385
    assert body["entropy_score"] == pytest.approx(0.385)
    # queue saturation (0.8) + rising retries (0.5) both trip independently
    # of the aggregate score.
    assert controller.ACTIVATE_DETERMINISTIC_MODE in body["countermeasures"]
    assert controller.FREEZE_NONESSENTIAL_LLM_PATHS in body["countermeasures"]
    # 0.385 < INSTABILITY_THRESHOLD (0.50) → still reported stable.
    assert body["stable"] is True
    assert body["persisted"] is True


def test_work_route_marks_unstable_above_threshold():
    client = TestClient(app)
    r = client.post(
        "/work",
        json={
            "task_id": "t-unstable",
            "payload": {
                "queue_variance": 1.0,
                "error_velocity": 1.0,
                "task_retry_rate": 1.0,
                "llm_failure_ratio": 1.0,
            },
            "metadata": {"action": "entropy_scan"},
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["entropy_score"] == pytest.approx(1.0)
    assert body["stable"] is False


def test_work_unknown_action_400():
    client = TestClient(app)
    r = client.post(
        "/work",
        json={"task_id": "t-2", "payload": {}, "metadata": {"action": "bogus"}},
    )
    assert r.status_code == 400


def test_entropy_scan_rest_alias():
    client = TestClient(app)
    r = client.post(
        "/entropy/scan",
        json={"task_id": "t-3", "payload": {"queue_variance": 0.0}},
    )
    assert r.status_code == 200
    assert r.json()["task_id"] == "t-3"


# ── compute_entropy_score: exactness ─────────────────────────────────────────


def test_compute_entropy_score_exact_weighted_output():
    # 0.4*0.25 + 0.5*0.30 + 0.6*0.20 + 0.8*0.25
    # = 0.10 + 0.15 + 0.12 + 0.20 = 0.57
    inputs = EntropyInputs(
        queue_variance=0.4,
        error_velocity=0.5,
        task_retry_rate=0.6,
        llm_failure_ratio=0.8,
    )
    assert compute_entropy_score(inputs) == pytest.approx(0.57)


def test_compute_entropy_score_zero_inputs():
    assert compute_entropy_score(EntropyInputs()) == 0.0


def test_compute_entropy_score_all_max_is_one():
    inputs = EntropyInputs(1.0, 1.0, 1.0, 1.0)
    assert compute_entropy_score(inputs) == pytest.approx(1.0)


# ── compute_entropy_score: clamping ──────────────────────────────────────────


def test_compute_entropy_score_clamps_high():
    inputs = EntropyInputs(5.0, 5.0, 5.0, 5.0)
    assert compute_entropy_score(inputs) == ENTROPY_SCORE_MAX


def test_compute_entropy_score_clamps_low():
    inputs = EntropyInputs(-5.0, -5.0, -5.0, -5.0)
    assert compute_entropy_score(inputs) == ENTROPY_SCORE_MIN


# ── countermeasure mapping: five conditions ──────────────────────────────────


def test_countermeasure_rising_retries():
    inputs = EntropyInputs(task_retry_rate=0.5)
    measures = recommend_countermeasures(inputs)
    assert controller.FREEZE_NONESSENTIAL_LLM_PATHS in measures


def test_countermeasure_queue_saturation():
    inputs = EntropyInputs(queue_variance=0.9)
    measures = recommend_countermeasures(inputs)
    assert controller.ACTIVATE_DETERMINISTIC_MODE in measures


def test_countermeasure_low_efficiency_ema():
    measures = recommend_countermeasures(EntropyInputs(), efficiency_ema=0.1)
    assert controller.REDUCE_QUOTA in measures


def test_countermeasure_successful_template_route():
    measures = recommend_countermeasures(EntropyInputs(), template_success_rate=0.9)
    assert controller.CLONE_TEMPLATE_STRATEGY in measures


def test_countermeasure_rising_token_success_ratio():
    measures = recommend_countermeasures(EntropyInputs(), token_success_ratio=3.5)
    assert controller.TIGHTEN_PROSPECT_FILTERING in measures


def test_countermeasure_quiet_system_no_measures():
    measures = recommend_countermeasures(
        EntropyInputs(queue_variance=0.1, task_retry_rate=0.05),
        efficiency_ema=0.95,
        token_success_ratio=0.5,
        template_success_rate=0.1,
    )
    assert measures == []


# ── monitor orchestration ────────────────────────────────────────────────────


def test_run_monitor_marks_unstable_above_threshold():
    report = run_monitor(
        queue_variance=1.0,
        error_velocity=1.0,
        task_retry_rate=1.0,
        llm_failure_ratio=1.0,
    )
    assert report.entropy_score == pytest.approx(1.0)
    assert report.stable is False


def test_run_monitor_stable_when_quiet():
    report = run_monitor()
    assert report.entropy_score == 0.0
    assert report.stable is True
