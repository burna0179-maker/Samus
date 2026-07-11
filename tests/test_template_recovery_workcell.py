"""TestClient + unit smoke for the template-recovery workcell.

Covers the contract §10 conventions: /health smoke, /work route, a
determinism check, an unknown-task-kind safe-generic-scaffold check, and a
pivot-decision boundary check around ``efficiency_ema`` 0.45.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.template_recovery.app import app
from backend.template_recovery.fallback import clear_cache, render_scaffold
from backend.template_recovery.selector import (
    EFFICIENCY_PIVOT_THRESHOLD,
    select_scaffold,
    should_recover,
)
from backend.template_recovery.templates import SCAFFOLD_LIBRARY


@pytest.fixture(autouse=True)
def _clear_render_cache():
    """Each test sees a cold render cache so determinism is observable."""
    clear_cache()
    yield
    clear_cache()


client = TestClient(app)


# ── HTTP smoke ───────────────────────────────────────────────────────────────


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["service"] == "template_recovery"


def test_work_route_serves_known_scaffold():
    r = client.post(
        "/work",
        json={
            "task_id": "t-1",
            "payload": {
                "task_kind": "seo_audit",
                "context": {
                    "business_name": "Acme Co",
                    "target_keywords": ["plumber", "yuba city"],
                },
                "failure_reason": "llm timeout",
            },
            "metadata": {"action": "recover"},
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["task_kind"] == "seo_audit"
    assert body["template_version"] == "seo_template_v3"
    assert body["fallback_triggered"] is True
    assert body["generic_fallback"] is False
    assert "Acme Co" in body["scaffold"]
    assert "plumber" in body["scaffold"]


def test_work_route_unknown_action_400():
    r = client.post(
        "/work",
        json={
            "task_id": "t-2",
            "payload": {"task_kind": "seo_audit"},
            "metadata": {"action": "demolish"},
        },
    )
    assert r.status_code == 400


def test_rest_alias_recover():
    r = client.post(
        "/template_recovery/recover",
        json={"task_kind": "proposal", "context": {"business_name": "Beta LLC"}},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["template_version"] == "proposal_template_v2"
    assert "Beta LLC" in body["scaffold"]


def test_work_route_invalid_payload_422():
    r = client.post(
        "/work",
        json={"task_id": "t-3", "payload": {}, "metadata": {"action": "recover"}},
    )
    assert r.status_code == 422


# ── Determinism ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("task_kind", sorted(SCAFFOLD_LIBRARY))
def test_render_is_deterministic(task_kind):
    """Same input twice -> byte-identical scaffold (cache cleared between)."""
    context = {
        "business_name": "Determinism Test Co",
        "target_keywords": ["k1", "k2"],
        "contact_name": "Sam",
        "offer": "an audit",
        "industry": "retail",
        "phone": "555-0100",
        "url": "https://example.test",
        "price": "$500",
    }
    first = render_scaffold(task_kind, dict(context))
    clear_cache()
    second = render_scaffold(task_kind, dict(context))
    assert first.scaffold == second.scaffold
    assert first.template_version == second.template_version
    assert first.generic_fallback is False


def test_work_route_determinism_over_http():
    payload = {
        "task_id": "t-det",
        "payload": {"task_kind": "callsheet", "context": {"business_name": "X"}},
        "metadata": {"action": "recover"},
    }
    first = client.post("/work", json=payload).json()
    clear_cache()
    second = client.post("/work", json=payload).json()
    assert first["scaffold"] == second["scaffold"]


# ── Unknown task kind -> safe generic scaffold ───────────────────────────────


def test_unknown_task_kind_returns_generic_scaffold():
    result = render_scaffold("nonexistent_kind", {"business_name": "Gamma Inc"})
    assert result.generic_fallback is True
    assert result.template_version == "generic_template_v1"
    assert "Gamma Inc" in result.scaffold
    assert result.scaffold  # non-empty — no exception raised


def test_unknown_task_kind_over_http():
    r = client.post(
        "/work",
        json={
            "task_id": "t-unknown",
            "payload": {"task_kind": "totally_unknown", "context": {}},
            "metadata": {"action": "recover"},
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["generic_fallback"] is True
    assert body["template_version"] == "generic_template_v1"
    assert body["fallback_triggered"] is True


def test_select_scaffold_never_raises_on_unknown():
    builder, version, is_generic = select_scaffold("???", {})
    assert is_generic is True
    assert version == "generic_template_v1"
    assert callable(builder)


# ── Pivot-decision boundary around efficiency_ema 0.45 ───────────────────────


def test_pivot_threshold_constant():
    assert EFFICIENCY_PIVOT_THRESHOLD == 0.45


def test_pivot_decision_boundary():
    # Strictly below threshold + error outcome -> recover.
    assert should_recover("error", 0.44) is True
    assert should_recover("error", 0.0) is True
    # Exactly at threshold -> does NOT trigger (strictly-below rule).
    assert should_recover("error", 0.45) is False
    # Above threshold -> does NOT trigger.
    assert should_recover("error", 0.46) is False
    assert should_recover("error", 1.0) is False
    # Non-error outcome never triggers, even with a low EMA.
    assert should_recover("success", 0.10) is False
    assert should_recover("failure", 0.10) is False
