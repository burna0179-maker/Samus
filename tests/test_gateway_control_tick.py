"""Tests for the stack-level control-loop tick.

Covers two layers:

  * ``backend.gateway.control_tick.run_control_tick`` — the in-process
    observe->decide pass: happy path, custom signal inputs, the best-effort
    partial-failure paths (entropy fails / portfolio fails), and the ledger
    recording.
  * ``backend.gateway.app`` routes — ``POST /admin/control-tick`` and
    ``GET /admin/control-ticks``: 200 contract, body validation, capability
    gating, and read-route round-trip.

Both the entropy + portfolio_controller ``write_task_state`` persistence
calls are monkeypatched so tests stay deterministic and offline (mirrors the
per-workcell test fixtures).
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _ledger_tmpfile(tmp_path, monkeypatch):
    """Redirect the control-tick ledger to a per-test tmpfile."""
    monkeypatch.setenv("SAMUS_CONTROL_TICK_PATH", str(tmp_path / "ticks.jsonl"))


@pytest.fixture(autouse=True)
def _no_task_state_writes(monkeypatch):
    """Stub the DDB-backed task-state writes in both downstream workcells."""
    import backend.entropy.service as entropy_svc
    import backend.portfolio_controller.service as pc_svc

    monkeypatch.setattr(entropy_svc, "write_task_state", lambda **_kw: True)
    monkeypatch.setattr(pc_svc, "write_task_state", lambda **_kw: True)


@pytest.fixture(autouse=True)
def _enforcement_off_by_default(monkeypatch):
    """Disable quota enforcement for the generic tick tests so they don't write
    to the shared LLM-budget store. The dedicated enforcement tests below arm
    it explicitly against a fake store."""
    monkeypatch.setenv("SAMUS_CONTROL_TICK_ENFORCE", "0")


def _client():
    from backend.gateway.app import app

    return TestClient(app)


# ── run_control_tick: happy path ─────────────────────────────────────────────


def test_tick_happy_path_calls_both_workcells():
    from backend.gateway.control_tick import run_control_tick

    result = run_control_tick()
    assert result["ok"] is True
    assert result["entropy"]["ok"] is True
    assert result["portfolio"]["ok"] is True
    # No signals supplied -> stable baseline scan.
    assert result["entropy"]["entropy_score"] == 0.0
    assert result["entropy"]["stable"] is True
    # Default tracked workcell set was rebalanced.
    assert result["portfolio"]["workcell_count"] >= 1
    assert result["recorded"] is True
    assert "enforcement_note" in result


def test_tick_generates_task_id_when_omitted():
    from backend.gateway.control_tick import run_control_tick

    result = run_control_tick()
    assert result["task_id"].startswith("ct-")


def test_tick_honours_caller_task_id():
    from backend.gateway.control_tick import run_control_tick

    result = run_control_tick(task_id="operator-tick-42")
    assert result["task_id"] == "operator-tick-42"


def test_tick_with_unstable_entropy_inputs_flags_instability():
    from backend.gateway.control_tick import run_control_tick

    # High instability signals -> score crosses the instability threshold.
    result = run_control_tick(
        entropy_inputs={
            "queue_variance": 1.0,
            "error_velocity": 1.0,
            "task_retry_rate": 1.0,
            "llm_failure_ratio": 1.0,
        }
    )
    assert result["entropy"]["ok"] is True
    assert result["recommendations"]["stable"] is False
    assert result["recommendations"]["entropy_score"] > 0.0
    assert result["recommendations"]["countermeasures"]  # non-empty


def test_tick_with_explicit_workcells_drives_rebalance():
    from backend.gateway.control_tick import run_control_tick

    # A workcell with high error_velocity must take a quota cut.
    result = run_control_tick(
        workcells=[
            {"workcell": "prospecting", "error_velocity": 0.9},
            {"workcell": "seo", "throughput_efficiency": 0.95},
        ]
    )
    assert result["portfolio"]["ok"] is True
    assert result["portfolio"]["workcell_count"] == 2
    # quota_cut on prospecting, priority_boost on seo.
    assert result["recommendations"]["quota_cuts"] >= 1
    assert result["recommendations"]["priority_boosts"] >= 1
    adjusted = {a["workcell"] for a in result["recommendations"]["workcell_adjustments"]}
    assert adjusted == {"prospecting", "seo"}


def test_tick_records_to_ledger():
    from backend.common import control_tick_ledger
    from backend.gateway.control_tick import run_control_tick

    run_control_tick(task_id="recorded-tick")
    view = control_tick_ledger.recent_ticks()
    assert view["count"] == 1
    assert view["ticks"][0]["task_id"] == "recorded-tick"


# ── run_control_tick: best-effort partial failure ────────────────────────────


def test_tick_survives_entropy_failure(monkeypatch):
    """An entropy.scan crash is captured; the tick still completes."""
    import backend.entropy.service as entropy_svc
    from backend.gateway.control_tick import run_control_tick

    def _boom(*_a, **_kw):
        raise RuntimeError("entropy backend down")

    monkeypatch.setattr(entropy_svc, "scan", _boom)
    result = run_control_tick()
    # Overall not ok, but the tick returned a structured snapshot.
    assert result["ok"] is False
    assert result["entropy"]["ok"] is False
    assert "entropy_scan_failed" in result["entropy"]["error"]
    # The decide stage still ran.
    assert result["portfolio"]["ok"] is True
    # And the partial snapshot was still recorded.
    assert result["recorded"] is True


def test_tick_survives_rebalance_failure(monkeypatch):
    """A run_rebalance crash is captured; the tick still completes."""
    import backend.portfolio_controller.service as pc_svc
    from backend.gateway.control_tick import run_control_tick

    def _boom(*_a, **_kw):
        raise RuntimeError("portfolio backend down")

    monkeypatch.setattr(pc_svc, "run_rebalance", _boom)
    result = run_control_tick()
    assert result["ok"] is False
    assert result["portfolio"]["ok"] is False
    assert "rebalance_failed" in result["portfolio"]["error"]
    # The observe stage still produced a result.
    assert result["entropy"]["ok"] is True
    assert result["recorded"] is True


def test_tick_survives_both_failures(monkeypatch):
    """Both sub-calls failing still yields a well-formed recorded snapshot."""
    import backend.entropy.service as entropy_svc
    import backend.portfolio_controller.service as pc_svc
    from backend.gateway.control_tick import run_control_tick

    monkeypatch.setattr(
        entropy_svc, "scan", lambda *_a, **_kw: (_ for _ in ()).throw(RuntimeError("e"))
    )
    monkeypatch.setattr(
        pc_svc, "run_rebalance",
        lambda *_a, **_kw: (_ for _ in ()).throw(RuntimeError("p")),
    )
    result = run_control_tick()
    assert result["ok"] is False
    assert result["entropy"]["ok"] is False
    assert result["portfolio"]["ok"] is False
    assert result["recorded"] is True


def test_tick_survives_ledger_failure(monkeypatch):
    """A ledger write failure surfaces as recorded=False, never raises."""
    from backend.common.persistence import JsonlLedger
    from backend.gateway.control_tick import run_control_tick

    def _boom(self, record):
        raise OSError("disk full")

    monkeypatch.setattr(JsonlLedger, "append", _boom)
    result = run_control_tick()
    # Sub-calls still succeeded; only the recording failed.
    assert result["ok"] is True
    assert result["recorded"] is False


# ── ENFORCE stage: apply quota cuts to the LLM-budget store ──────────────────


class _FakeBudget:
    def __init__(self, quota: int) -> None:
        self.quota_override = quota
        self.quota_override_expires_at = "2026-07-06T12:00:00+00:00"


class _FakeStore:
    """Records set_quota_override calls; mirrors LlmBudgetStore's return shape."""

    def __init__(self, *, raises: bool = False) -> None:
        self.calls: list[tuple] = []
        self._raises = raises

    def set_quota_override(self, workcell, quota, ttl_seconds):
        self.calls.append((workcell, quota, ttl_seconds))
        if self._raises:
            raise RuntimeError("store down")
        return _FakeBudget(quota)


def test_enforcement_applies_quota_cuts_to_store(monkeypatch):
    """Armed: each rebalance quota_cut becomes a set_quota_override call."""
    import backend.common.llm_budget as budget
    from backend.gateway.control_tick import run_control_tick

    monkeypatch.setenv("SAMUS_CONTROL_TICK_ENFORCE", "1")
    monkeypatch.setenv("SAMUS_CONTROL_TICK_ENFORCE_TTL_SEC", "1800")
    store = _FakeStore()
    monkeypatch.setattr(budget, "get_store", lambda: store)

    result = run_control_tick(
        workcells=[
            {"workcell": "prospecting", "error_velocity": 0.9},   # -> quota_cut
            {"workcell": "seo", "throughput_efficiency": 0.95},   # -> priority boost only
        ]
    )

    enforcement = result["enforcement"]
    assert enforcement["enabled"] is True
    assert enforcement["ok"] is True
    # Only the quota_cut workcell (prospecting) was enforced; the priority
    # boost (seo) has no LLM-budget lever and stays advisory.
    assert enforcement["applied"] == 1
    assert [c[0] for c in store.calls] == ["prospecting"]
    # TTL threaded through from the env override.
    assert store.calls[0][2] == 1800.0
    assert enforcement["overrides"][0]["workcell"] == "prospecting"


def test_enforcement_disabled_is_noop(monkeypatch):
    import backend.common.llm_budget as budget
    from backend.gateway.control_tick import run_control_tick

    monkeypatch.setenv("SAMUS_CONTROL_TICK_ENFORCE", "0")
    store = _FakeStore()
    monkeypatch.setattr(budget, "get_store", lambda: store)

    result = run_control_tick(
        workcells=[{"workcell": "prospecting", "error_velocity": 0.9}]
    )
    assert result["enforcement"]["enabled"] is False
    assert result["enforcement"]["applied"] == 0
    assert store.calls == []


def test_enforcement_survives_store_failure(monkeypatch):
    """A store failure is captured; the tick still completes (best-effort)."""
    import backend.common.llm_budget as budget
    from backend.gateway.control_tick import run_control_tick

    monkeypatch.setenv("SAMUS_CONTROL_TICK_ENFORCE", "1")
    store = _FakeStore(raises=True)
    monkeypatch.setattr(budget, "get_store", lambda: store)

    result = run_control_tick(
        workcells=[{"workcell": "prospecting", "error_velocity": 0.9}]
    )
    assert result["enforcement"]["ok"] is False
    assert "enforcement_failed" in result["enforcement"]["error"]
    # The rebalance recommendation itself is unaffected.
    assert result["portfolio"]["ok"] is True


# ── HTTP: POST /admin/control-tick ───────────────────────────────────────────


def test_route_control_tick_empty_body():
    """An empty POST is a valid all-defaults tick."""
    r = _client().post("/admin/control-tick")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["entropy"]["ok"] is True
    assert body["portfolio"]["ok"] is True
    assert body["recorded"] is True


def test_route_control_tick_with_signals():
    r = _client().post(
        "/admin/control-tick",
        json={
            "task_id": "http-tick",
            "entropy_inputs": {"error_velocity": 1.0, "queue_variance": 1.0,
                               "task_retry_rate": 1.0, "llm_failure_ratio": 1.0},
            "workcells": [{"workcell": "prospecting", "error_velocity": 0.9}],
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["task_id"] == "http-tick"
    assert body["recommendations"]["stable"] is False
    assert body["portfolio"]["workcell_count"] == 1


def test_route_control_tick_rejects_bad_task_id():
    r = _client().post("/admin/control-tick", json={"task_id": 123})
    assert r.status_code == 422


def test_route_control_tick_rejects_bad_entropy_inputs():
    r = _client().post("/admin/control-tick", json={"entropy_inputs": "nope"})
    assert r.status_code == 422


def test_route_control_tick_rejects_bad_workcells():
    r = _client().post("/admin/control-tick", json={"workcells": {"not": "a list"}})
    assert r.status_code == 422


# ── HTTP: GET /admin/control-ticks ───────────────────────────────────────────


def test_route_control_ticks_empty_when_no_ticks():
    r = _client().get("/admin/control-ticks")
    assert r.status_code == 200
    body = r.json()
    assert body == {"ticks": [], "count": 0, "error": None}


def test_route_control_ticks_returns_recorded_ticks():
    client = _client()
    client.post("/admin/control-tick", json={"task_id": "tick-a"})
    client.post("/admin/control-tick", json={"task_id": "tick-b"})

    r = client.get("/admin/control-ticks")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 2
    assert [t["task_id"] for t in body["ticks"]] == ["tick-a", "tick-b"]


def test_route_control_ticks_respects_limit():
    client = _client()
    for i in range(5):
        client.post("/admin/control-tick", json={"task_id": f"tick-{i}"})
    r = client.get("/admin/control-ticks", params={"limit": 2})
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 2
    assert [t["task_id"] for t in body["ticks"]] == ["tick-3", "tick-4"]


def test_route_control_tick_capability_registered():
    """The gateway must advertise the control_tick capability."""
    from backend.common.capabilities import SERVICE_CAPABILITIES

    assert "control_tick" in SERVICE_CAPABILITIES["gateway"]
