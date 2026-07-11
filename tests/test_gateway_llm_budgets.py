"""Gateway operator endpoint: GET /admin/llm_budgets."""
from __future__ import annotations

from fastapi.testclient import TestClient


def _client():
    from backend.gateway.app import app
    return TestClient(app)


def test_admin_endpoint_returns_one_row_per_workcell(monkeypatch):
    # Seed budget state for one workcell so the snapshot has non-zero fields.
    import backend.common.llm_budget as bm
    bm.reset_store()
    store = bm.get_store()
    store.record_spend("prospecting", input_tokens=150, output_tokens=75,
                       outcome="success")

    r = _client().get("/admin/llm_budgets")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "workcells" in body
    workcells = {row["workcell"]: row for row in body["workcells"]}
    assert "prospecting" in workcells
    assert "seo" in workcells

    p = workcells["prospecting"]
    assert p["used_tokens"] == 225
    assert p["input_tokens_today"] == 150
    assert p["output_tokens_today"] == 75
    assert p["success_count_today"] == 1
    assert p["call_count_today"] == 1
    assert p["quota_tokens"] >= p["used_tokens"]
    assert p["remaining_tokens"] == p["quota_tokens"] - p["used_tokens"]
    # Fresh workcell — efficiency_ema starts at 1.0, success keeps it there.
    assert p["efficiency_ema"] == 1.0

    # Untouched workcell should still appear with zeros.
    s = workcells["seo"]
    assert s["used_tokens"] == 0
    assert s["call_count_today"] == 0


def test_admin_endpoint_exposes_store_constants():
    r = _client().get("/admin/llm_budgets")
    assert r.status_code == 200
    body = r.json()
    assert "base_token_budget" in body
    assert "ema_alpha" in body
    assert "floor_pct" in body
    assert body["base_token_budget"] > 0
    assert 0.0 < body["ema_alpha"] <= 1.0


def test_admin_endpoint_degrades_when_snapshot_raises(monkeypatch):
    """A broken backend must not crash the operator view — degrade per-row."""
    import backend.common.llm_budget as bm
    bm.reset_store()
    store = bm.get_store()

    def _boom(_workcell):
        raise RuntimeError("backend gone")

    monkeypatch.setattr(store, "snapshot", _boom)
    r = _client().get("/admin/llm_budgets")
    assert r.status_code == 200
    body = r.json()
    for row in body["workcells"]:
        assert "error" in row
        assert "snapshot_failed" in row["error"]
