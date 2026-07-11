"""Tests for the portfolio_controller workcell.

Covers: capability registration, /health smoke, /work routing, the REST
alias, and rebalance boundary behaviour around the two policy thresholds
(error_velocity 0.4, throughput_efficiency 0.8). The LLM budget store and
the task-state write are monkeypatched so every test is deterministic and
fully offline.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient


# --- deterministic fakes ----------------------------------------------------


@dataclass
class _FakeBudget:
    """Stand-in for ``WorkcellBudget`` — only ``efficiency_ema`` is read."""

    efficiency_ema: float = 1.0


class _FakeStore:
    """Stand-in for ``LlmBudgetStore`` — returns a fixed EMA per workcell."""

    def __init__(self, emas: dict[str, float] | None = None, default: float = 1.0):
        self._emas = emas or {}
        self._default = default

    def snapshot(self, workcell: str) -> _FakeBudget:
        return _FakeBudget(efficiency_ema=self._emas.get(workcell, self._default))


@pytest.fixture(autouse=True)
def _no_task_state_writes(monkeypatch):
    """Neutralise the DDB task-state write so tests never touch AWS."""
    import backend.portfolio_controller.service as svc_mod

    monkeypatch.setattr(svc_mod, "write_task_state", lambda **_kw: True)
    yield


def _client() -> TestClient:
    from backend.portfolio_controller.app import app

    return TestClient(app)


# --- capability registration ------------------------------------------------


def test_capability_registered():
    from backend.common.capabilities import SERVICE_CAPABILITIES

    assert "portfolio_controller" in SERVICE_CAPABILITIES
    assert "plan_execution" in SERVICE_CAPABILITIES["portfolio_controller"]


# --- /health smoke ----------------------------------------------------------


def test_health():
    r = _client().get("/health")
    assert r.status_code == 200
    assert r.json()["service"] == "portfolio_controller"


# --- /work routing ----------------------------------------------------------


def test_work_rebalance_action(monkeypatch):
    monkeypatch.setattr(
        "backend.portfolio_controller.signals.get_store",
        lambda: _FakeStore(default=1.0),
        raising=False,
    )
    client = _client()
    r = client.post(
        "/work",
        json={
            "task_id": "t-pc-1",
            "payload": {"workcells": [{"workcell": "seo"}]},
            "metadata": {"action": "rebalance"},
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["task_id"] == "t-pc-1"
    assert body["decision_path"] == "rebalance"
    assert body["capability"] == "plan_execution"
    assert body["workcell_count"] == 1
    assert body["allocations"][0]["workcell"] == "seo"


def test_work_unknown_action_returns_400():
    r = _client().post(
        "/work",
        json={"task_id": "t-bad", "payload": {}, "metadata": {"action": "nope"}},
    )
    assert r.status_code == 400
    assert "unknown_action" in r.json()["detail"]


def test_work_default_action_is_rebalance(monkeypatch):
    monkeypatch.setattr(
        "backend.portfolio_controller.signals.get_store",
        lambda: _FakeStore(default=1.0),
        raising=False,
    )
    # No metadata.action → defaults to "rebalance"; empty payload → default set.
    r = _client().post("/work", json={"task_id": "t-def", "payload": {}})
    assert r.status_code == 200, r.text
    assert r.json()["workcell_count"] >= 1


def test_rest_alias_rebalance(monkeypatch):
    monkeypatch.setattr(
        "backend.portfolio_controller.signals.get_store",
        lambda: _FakeStore(default=1.0),
        raising=False,
    )
    r = _client().post(
        "/portfolio_controller/rebalance",
        json={"task_id": "rest-1", "workcells": [{"workcell": "prospecting"}]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["task_id"] == "rest-1"


# --- rebalance boundary tests (allocator policy) ----------------------------


def _alloc(error_velocity=0.0, throughput_efficiency=0.0):
    from backend.portfolio_controller.allocator import WorkcellAllocation
    from backend.portfolio_controller.signals import PortfolioSignals

    return WorkcellAllocation(
        workcell="wc",
        token_quota=100_000.0,
        priority_weight=1.0,
        signals=PortfolioSignals(
            error_velocity=error_velocity,
            throughput_efficiency=throughput_efficiency,
        ),
    )


def test_error_velocity_just_above_threshold_halves_quota():
    from backend.portfolio_controller.allocator import rebalance

    a = _alloc(error_velocity=0.41)
    rebalance([a])
    assert a.token_quota == pytest.approx(50_000.0)
    assert a.quota_cut is True


def test_error_velocity_just_below_threshold_keeps_quota():
    from backend.portfolio_controller.allocator import rebalance

    a = _alloc(error_velocity=0.39)
    rebalance([a])
    assert a.token_quota == pytest.approx(100_000.0)
    assert a.quota_cut is False


def test_error_velocity_exactly_at_threshold_keeps_quota():
    """Threshold is strict ``>`` — exactly 0.4 does NOT cut."""
    from backend.portfolio_controller.allocator import rebalance

    a = _alloc(error_velocity=0.4)
    rebalance([a])
    assert a.token_quota == pytest.approx(100_000.0)
    assert a.quota_cut is False


def test_throughput_just_above_threshold_boosts_priority():
    from backend.portfolio_controller.allocator import rebalance

    a = _alloc(throughput_efficiency=0.81)
    rebalance([a])
    assert a.priority_weight == pytest.approx(1.3)
    assert a.priority_boosted is True


def test_throughput_just_below_threshold_keeps_priority():
    from backend.portfolio_controller.allocator import rebalance

    a = _alloc(throughput_efficiency=0.79)
    rebalance([a])
    assert a.priority_weight == pytest.approx(1.0)
    assert a.priority_boosted is False


def test_throughput_exactly_at_threshold_keeps_priority():
    """Threshold is strict ``>`` — exactly 0.8 does NOT boost."""
    from backend.portfolio_controller.allocator import rebalance

    a = _alloc(throughput_efficiency=0.8)
    rebalance([a])
    assert a.priority_weight == pytest.approx(1.0)
    assert a.priority_boosted is False


def test_combined_cut_and_boost():
    """A workcell can take both rules independently in one rebalance."""
    from backend.portfolio_controller.allocator import rebalance

    a = _alloc(error_velocity=0.9, throughput_efficiency=0.95)
    rebalance([a])
    assert a.token_quota == pytest.approx(50_000.0)
    assert a.priority_weight == pytest.approx(1.3)
    assert a.quota_cut is True
    assert a.priority_boosted is True


# --- signal builder ---------------------------------------------------------


def test_build_signals_derives_from_ema():
    """Low EMA → high error_velocity + low throughput_efficiency."""
    from backend.portfolio_controller.signals import build_signals

    store = _FakeStore(emas={"seo": 0.2})
    sig = build_signals("seo", store=store)
    assert sig.error_velocity == pytest.approx(0.8)
    assert sig.throughput_efficiency == pytest.approx(0.2)


def test_build_signals_explicit_inputs_override_ema():
    from backend.portfolio_controller.signals import build_signals

    store = _FakeStore(emas={"seo": 0.2})
    sig = build_signals(
        "seo",
        store=store,
        inputs={"error_velocity": 0.05, "queue_depth_ratio": 2.5},
    )
    assert sig.error_velocity == pytest.approx(0.05)
    assert sig.queue_depth_ratio == pytest.approx(2.5)
    # throughput_efficiency not overridden → still EMA-derived
    assert sig.throughput_efficiency == pytest.approx(0.2)


def test_service_rebalance_low_ema_workcell_gets_quota_cut():
    """End-to-end through the service: a 0.2-EMA workcell is quota-cut."""
    from backend.portfolio_controller.models import RebalanceRequest, WorkcellInput
    from backend.portfolio_controller.service import run_rebalance

    store = _FakeStore(emas={"seo": 0.2, "prospecting": 0.95})
    req = RebalanceRequest(
        task_id="svc-1",
        workcells=[
            WorkcellInput(workcell="seo"),
            WorkcellInput(workcell="prospecting"),
        ],
    )
    resp = run_rebalance(req, store=store)
    by_wc = {a.workcell: a for a in resp.allocations}
    # seo: error_velocity 0.8 > 0.4 → cut; throughput 0.2 → no boost
    assert by_wc["seo"].quota_cut is True
    assert by_wc["seo"].priority_boosted is False
    # prospecting: error_velocity 0.05 → no cut; throughput 0.95 > 0.8 → boost
    assert by_wc["prospecting"].quota_cut is False
    assert by_wc["prospecting"].priority_boosted is True
    assert resp.quota_cuts == 1
    assert resp.priority_boosts == 1
