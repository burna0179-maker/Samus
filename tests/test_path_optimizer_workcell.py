"""Tests for the path-optimizer workcell.

Covers the deterministic routing core (``select_execution_path`` boundaries
and ``detect_error_spike``) and the FastAPI app (``/health``, ``/work``,
``/path_optimizer/select``). The LLM budget-store snapshot is monkeypatched so
every test runs offline and deterministically.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.path_optimizer.app import app
from backend.path_optimizer.router import (
    AUTONOMOUS_EMA_THRESHOLD,
    ERROR_SPIKE_RATIO,
    ERROR_SPIKE_WINDOW,
    HYBRID_EMA_THRESHOLD,
    PATH_AUTONOMOUS_LLM,
    PATH_DETERMINISTIC_SCAFFOLD,
    PATH_HYBRID_TEMPLATE,
    PATH_SAFE_STATIC_FALLBACK,
    PathOptimizer,
    WorkcellState,
    detect_error_spike,
)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _state(ema: float, *, spike: bool = False) -> WorkcellState:
    return WorkcellState(workcell="prospecting", efficiency_ema=ema, error_spike_detected=spike)


def _stub_ema(monkeypatch, ema: float) -> None:
    """Force the service's efficiency_ema lookup to return ``ema``."""
    import backend.path_optimizer.service as svc_mod

    monkeypatch.setattr(svc_mod, "_resolve_efficiency_ema", lambda _wc: ema)


def _outcomes(*kinds: str) -> list[dict]:
    return [{"outcome": k} for k in kinds]


# ── select_execution_path — boundary tests ──────────────────────────────────


def test_select_path_autonomous_above_threshold():
    assert PathOptimizer().select_execution_path(_state(0.95)) == PATH_AUTONOMOUS_LLM


def test_select_path_autonomous_exact_threshold():
    """0.82 is inclusive → autonomous."""
    assert AUTONOMOUS_EMA_THRESHOLD == 0.82
    assert PathOptimizer().select_execution_path(_state(0.82)) == PATH_AUTONOMOUS_LLM


def test_select_path_hybrid_just_below_autonomous():
    assert PathOptimizer().select_execution_path(_state(0.8199)) == PATH_HYBRID_TEMPLATE


def test_select_path_hybrid_exact_threshold():
    """0.55 is inclusive → hybrid_template."""
    assert HYBRID_EMA_THRESHOLD == 0.55
    assert PathOptimizer().select_execution_path(_state(0.55)) == PATH_HYBRID_TEMPLATE


def test_select_path_hybrid_outranks_error_spike():
    """A workcell above the hybrid threshold stays hybrid even with a spike."""
    assert PathOptimizer().select_execution_path(_state(0.70, spike=True)) == PATH_HYBRID_TEMPLATE


def test_select_path_deterministic_scaffold_on_error_spike():
    """Low EMA + error spike → deterministic scaffold."""
    assert (
        PathOptimizer().select_execution_path(_state(0.5499, spike=True))
        == PATH_DETERMINISTIC_SCAFFOLD
    )


def test_select_path_safe_static_fallback():
    """Low EMA, no spike → safe static fallback."""
    assert (
        PathOptimizer().select_execution_path(_state(0.10, spike=False))
        == PATH_SAFE_STATIC_FALLBACK
    )


def test_select_path_zero_ema_no_spike():
    assert (
        PathOptimizer().select_execution_path(_state(0.0, spike=False)) == PATH_SAFE_STATIC_FALLBACK
    )


# ── detect_error_spike — boundary tests ─────────────────────────────────────


def test_detect_error_spike_empty_history():
    assert detect_error_spike([]) is False


def test_detect_error_spike_below_ratio():
    """6 errors / 20 = 0.30 < 0.35 → no spike."""
    history = _outcomes(*(["error"] * 6 + ["success"] * 14))
    assert len(history) == 20
    assert detect_error_spike(history) is False


def test_detect_error_spike_exact_ratio():
    """7 errors / 20 = 0.35 → spike (inclusive)."""
    assert ERROR_SPIKE_RATIO == 0.35
    history = _outcomes(*(["error"] * 7 + ["success"] * 13))
    assert detect_error_spike(history) is True


def test_detect_error_spike_above_ratio():
    history = _outcomes(*(["error"] * 15 + ["success"] * 5))
    assert detect_error_spike(history) is True


def test_detect_error_spike_only_inspects_last_window():
    """Old errors outside the 20-entry window are ignored."""
    assert ERROR_SPIKE_WINDOW == 20
    # 30 leading errors, then 20 clean successes — only the last 20 count.
    history = _outcomes(*(["error"] * 30 + ["success"] * 20))
    assert detect_error_spike(history) is False


def test_detect_error_spike_window_catches_recent_errors():
    """Recent errors inside the window trigger even with old successes."""
    history = _outcomes(*(["success"] * 30 + ["error"] * 10 + ["success"] * 10))
    # last 20 = 10 error + 10 success → 0.50 ≥ 0.35
    assert detect_error_spike(history) is True


def test_detect_error_spike_short_history():
    """1 error / 1 entry = 1.0 ≥ 0.35 → spike."""
    assert detect_error_spike(_outcomes("error")) is True


def test_detect_error_spike_ignores_non_error_outcomes():
    history = _outcomes("success", "failure", "success", "failure")
    assert detect_error_spike(history) is False


# ── /health smoke ────────────────────────────────────────────────────────────


def test_health():
    client = TestClient(app)
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["service"] == "path_optimizer"
    assert body["status"] == "ok"


# ── /work envelope route ─────────────────────────────────────────────────────


def test_work_select_path_autonomous(monkeypatch):
    _stub_ema(monkeypatch, 0.90)
    client = TestClient(app)
    r = client.post(
        "/work",
        json={
            "task_id": "t-auto",
            "payload": {"workcell": "prospecting", "history": []},
            "metadata": {"action": "select_path"},
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["decision_path"] == PATH_AUTONOMOUS_LLM
    assert body["efficiency_ema"] == 0.90
    assert body["workcell"] == "prospecting"
    assert body["error_spike_detected"] is False


def test_work_select_path_default_action(monkeypatch):
    """No action in metadata → defaults to select_path."""
    _stub_ema(monkeypatch, 0.60)
    client = TestClient(app)
    r = client.post(
        "/work",
        json={"task_id": "t-default", "payload": {"workcell": "seo"}},
    )
    assert r.status_code == 200
    assert r.json()["decision_path"] == PATH_HYBRID_TEMPLATE


def test_work_select_path_error_spike(monkeypatch):
    """Low EMA + error-heavy history → deterministic scaffold."""
    _stub_ema(monkeypatch, 0.20)
    client = TestClient(app)
    history = [{"outcome": "error"}] * 8 + [{"outcome": "success"}] * 12
    r = client.post(
        "/work",
        json={
            "task_id": "t-spike",
            "payload": {"workcell": "outreach", "history": history},
            "metadata": {"action": "select_path"},
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["decision_path"] == PATH_DETERMINISTIC_SCAFFOLD
    assert body["error_spike_detected"] is True
    assert body["history_size"] == 20


def test_work_select_path_safe_static_fallback(monkeypatch):
    _stub_ema(monkeypatch, 0.15)
    client = TestClient(app)
    r = client.post(
        "/work",
        json={
            "task_id": "t-fallback",
            "payload": {"workcell": "voice", "history": [{"outcome": "success"}]},
            "metadata": {"action": "select_path"},
        },
    )
    assert r.status_code == 200
    assert r.json()["decision_path"] == PATH_SAFE_STATIC_FALLBACK


def test_work_unknown_action_returns_400(monkeypatch):
    _stub_ema(monkeypatch, 0.90)
    client = TestClient(app)
    r = client.post(
        "/work",
        json={"task_id": "t-bad", "payload": {"workcell": "seo"}, "metadata": {"action": "nope"}},
    )
    assert r.status_code == 400


def test_work_missing_workcell_returns_422(monkeypatch):
    _stub_ema(monkeypatch, 0.90)
    client = TestClient(app)
    r = client.post(
        "/work",
        json={"task_id": "t-novc", "payload": {}, "metadata": {"action": "select_path"}},
    )
    assert r.status_code == 422


# ── /path_optimizer/select REST alias ────────────────────────────────────────


def test_rest_alias_select(monkeypatch):
    _stub_ema(monkeypatch, 0.83)
    client = TestClient(app)
    r = client.post(
        "/path_optimizer/select",
        json={"workcell": "prospecting", "history": []},
    )
    assert r.status_code == 200
    assert r.json()["decision_path"] == PATH_AUTONOMOUS_LLM


def test_rest_alias_rejects_extra_fields(monkeypatch):
    """models use extra='forbid' — unknown field → 422."""
    _stub_ema(monkeypatch, 0.83)
    client = TestClient(app)
    r = client.post(
        "/path_optimizer/select",
        json={"workcell": "prospecting", "history": [], "bogus": 1},
    )
    assert r.status_code == 422


# ── service-level integration (real budget store, JSON fallback) ─────────────


def test_service_resolves_ema_from_store_default():
    """A never-metered workcell yields the default EMA (1.0) → autonomous.

    conftest forces the budget store to JSON-only with a fresh tempfile, so
    snapshot() of an unknown workcell returns a default WorkcellBudget.
    """
    from backend.path_optimizer.models import SelectPathRequest
    from backend.path_optimizer.service import select_path

    resp = select_path(SelectPathRequest(workcell="brand-new-workcell"), task_id="t-svc")
    assert resp.efficiency_ema == pytest.approx(1.0)
    assert resp.decision_path == PATH_AUTONOMOUS_LLM
