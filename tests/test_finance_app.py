"""Finance workcell FastAPI endpoint tests."""

from __future__ import annotations


def _reset_idempotency(monkeypatch):
    from backend.common.idempotency import IdempotencyStore
    import backend.common.idempotency as idem_mod

    fresh = IdempotencyStore()
    monkeypatch.setattr(idem_mod, "GLOBAL_IDEMPOTENCY_STORE", fresh)
    import backend.finance.service as svc_mod

    monkeypatch.setattr(svc_mod, "GLOBAL_IDEMPOTENCY_STORE", fresh)


def _override_settings(monkeypatch, *, stripe_api_key: str = ""):
    class _S:
        pass

    settings = _S()
    settings.stripe_api_key = stripe_api_key
    import backend.finance.service as svc_mod

    monkeypatch.setattr(svc_mod, "get_settings", lambda: settings)


def _seed_codb(monkeypatch, tmp_path):
    yaml_path = tmp_path / "codb.yaml"
    yaml_path.write_text(
        "costs:\n"
        "  - id: aws\n"
        "    name: AWS\n"
        "    category: infrastructure\n"
        "    criticality: critical\n"
        "    estimated_monthly_usd: 30\n"
        "    notes: ''\n"
        "revenue_targets:\n"
        "  monthly_minimum_usd: 500\n"
        "  runway_alert_days: 60\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SAMUS_CODB_REGISTRY_PATH", str(yaml_path))


def test_get_snapshot_endpoint_works_without_stripe_key(tmp_path, monkeypatch):
    _reset_idempotency(monkeypatch)
    _override_settings(monkeypatch, stripe_api_key="")
    monkeypatch.setenv("SAMUS_FINANCE_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    _seed_codb(monkeypatch, tmp_path)
    from fastapi.testclient import TestClient
    from backend.finance.app import app

    client = TestClient(app)
    r = client.get("/snapshot")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["stripe_reachable"] is False
    assert body["stripe_error"] == "stripe_api_key_unset"
    assert "codb_summary" in body
    assert "runway" in body


def test_post_snapshot_endpoint_accepts_limits(tmp_path, monkeypatch):
    _reset_idempotency(monkeypatch)
    _override_settings(monkeypatch, stripe_api_key="")
    monkeypatch.setenv("SAMUS_FINANCE_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    _seed_codb(monkeypatch, tmp_path)
    from fastapi.testclient import TestClient
    from backend.finance.app import app

    client = TestClient(app)
    r = client.post("/snapshot", json={"charges_limit": 5, "payouts_limit": 3})
    assert r.status_code == 200, r.text


def test_codb_summary_endpoint(tmp_path, monkeypatch):
    _reset_idempotency(monkeypatch)
    _seed_codb(monkeypatch, tmp_path)
    from fastapi.testclient import TestClient
    from backend.finance.app import app

    client = TestClient(app)
    r = client.get("/codb_summary")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total_monthly_burn_usd"] == 30
    assert body["by_criticality"] == {"critical": 30}


def test_runway_endpoint_with_override(tmp_path, monkeypatch):
    _reset_idempotency(monkeypatch)
    _override_settings(monkeypatch, stripe_api_key="")
    _seed_codb(monkeypatch, tmp_path)
    from fastapi.testclient import TestClient
    from backend.finance.app import app

    client = TestClient(app)
    r = client.post("/runway", json={"override_balance_usd": 60.0})
    assert r.status_code == 200, r.text
    body = r.json()
    # burn 30/mo -> daily 1.0; balance 60 -> runway 60 days; alert at threshold = False
    assert body["daily_burn_usd"] == 1.0
    assert body["days_of_runway"] == 60.0
    assert body["alert_triggered"] is False


def test_runway_get_endpoint_uses_zero_when_no_stripe(tmp_path, monkeypatch):
    _reset_idempotency(monkeypatch)
    _override_settings(monkeypatch, stripe_api_key="")
    _seed_codb(monkeypatch, tmp_path)
    from fastapi.testclient import TestClient
    from backend.finance.app import app

    client = TestClient(app)
    r = client.get("/runway")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["available_balance_usd"] == 0.0
    assert body["alert_triggered"] is True


def test_work_endpoint_routes_by_metadata_action(tmp_path, monkeypatch):
    _reset_idempotency(monkeypatch)
    _override_settings(monkeypatch, stripe_api_key="")
    _seed_codb(monkeypatch, tmp_path)
    from fastapi.testclient import TestClient
    from backend.finance.app import app

    client = TestClient(app)
    r = client.post(
        "/work",
        json={
            "task_id": "t-1",
            "payload": {},
            "metadata": {"action": "codb_summary"},
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total_monthly_burn_usd"] == 30


def test_work_endpoint_runway_action(tmp_path, monkeypatch):
    _reset_idempotency(monkeypatch)
    _override_settings(monkeypatch, stripe_api_key="")
    _seed_codb(monkeypatch, tmp_path)
    from fastapi.testclient import TestClient
    from backend.finance.app import app

    client = TestClient(app)
    r = client.post(
        "/work",
        json={
            "task_id": "t-r",
            "payload": {"override_balance_usd": 30},
            "metadata": {"action": "runway"},
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["available_balance_usd"] == 30
    assert body["daily_burn_usd"] == 1.0


def test_work_endpoint_unknown_action_400(tmp_path, monkeypatch):
    _reset_idempotency(monkeypatch)
    _override_settings(monkeypatch, stripe_api_key="")
    _seed_codb(monkeypatch, tmp_path)
    from fastapi.testclient import TestClient
    from backend.finance.app import app

    client = TestClient(app)
    r = client.post(
        "/work",
        json={
            "task_id": "t-x",
            "payload": {},
            "metadata": {"action": "bogus"},
        },
    )
    assert r.status_code == 400
    assert "unknown_action" in r.text


def test_liabilities_endpoint_returns_summary(tmp_path, monkeypatch):
    _reset_idempotency(monkeypatch)
    from fastapi.testclient import TestClient
    from backend.finance.app import app

    client = TestClient(app)
    r = client.get("/liabilities")
    assert r.status_code == 200, r.text
    body = r.json()
    # Default registry: $1,642 outstanding from 4 lenders.
    assert body["total_outstanding_usd"] == 1642.0
    assert len(body["by_lender"]) == 4


def test_declines_endpoint_default_window(tmp_path, monkeypatch):
    _reset_idempotency(monkeypatch)
    from fastapi.testclient import TestClient
    from backend.finance.app import app

    client = TestClient(app)
    r = client.get("/declines")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["window_days"] == 30
    assert "cash_distress" in body
    assert "recent_events" in body


def test_declines_endpoint_custom_window(tmp_path, monkeypatch):
    _reset_idempotency(monkeypatch)
    from fastapi.testclient import TestClient
    from backend.finance.app import app

    client = TestClient(app)
    r = client.get("/declines?window_days=7")
    assert r.status_code == 200, r.text
    assert r.json()["window_days"] == 7


def test_work_endpoint_liabilities_action(tmp_path, monkeypatch):
    _reset_idempotency(monkeypatch)
    from fastapi.testclient import TestClient
    from backend.finance.app import app

    client = TestClient(app)
    r = client.post(
        "/work",
        json={
            "task_id": "t-liab",
            "payload": {},
            "metadata": {"action": "liabilities"},
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["total_outstanding_usd"] == 1642.0


def test_work_endpoint_declines_action(tmp_path, monkeypatch):
    _reset_idempotency(monkeypatch)
    from fastapi.testclient import TestClient
    from backend.finance.app import app

    client = TestClient(app)
    r = client.post(
        "/work",
        json={
            "task_id": "t-decl",
            "payload": {"window_days": 365},
            "metadata": {"action": "declines"},
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["window_days"] == 365


def _isolate_phase3_to_empty(monkeypatch, tmp_path):
    """Point Phase 3 yaml loaders at nonexistent files (returns empty registries)."""
    monkeypatch.setenv("SAMUS_DEBTS_PATH", str(tmp_path / "no_debts.yaml"))
    monkeypatch.setenv("SAMUS_ACTIONS_PATH", str(tmp_path / "no_actions.yaml"))
    monkeypatch.setenv("SAMUS_INFO_GAPS_PATH", str(tmp_path / "no_gaps.yaml"))
    monkeypatch.setenv("SAMUS_HARDSHIP_PATH", str(tmp_path / "no_hardship.yaml"))


def test_debts_endpoint_empty_when_no_registry(tmp_path, monkeypatch):
    _reset_idempotency(monkeypatch)
    _isolate_phase3_to_empty(monkeypatch, tmp_path)
    from fastapi.testclient import TestClient
    from backend.finance.app import app

    client = TestClient(app)
    r = client.get("/debts")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["registry_loaded"] is False
    assert body["debt_count"] == 0


def test_actions_endpoint_empty_when_no_registry(tmp_path, monkeypatch):
    _reset_idempotency(monkeypatch)
    _isolate_phase3_to_empty(monkeypatch, tmp_path)
    from fastapi.testclient import TestClient
    from backend.finance.app import app

    client = TestClient(app)
    r = client.get("/actions")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["registry_loaded"] is False
    assert body["open_total"] == 0


def test_info_gaps_endpoint_empty_when_no_registry(tmp_path, monkeypatch):
    _reset_idempotency(monkeypatch)
    _isolate_phase3_to_empty(monkeypatch, tmp_path)
    from fastapi.testclient import TestClient
    from backend.finance.app import app

    client = TestClient(app)
    r = client.get("/info_gaps")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["registry_loaded"] is False
    assert body["open_total"] == 0


def test_hardship_endpoint_empty_when_no_registry(tmp_path, monkeypatch):
    _reset_idempotency(monkeypatch)
    _isolate_phase3_to_empty(monkeypatch, tmp_path)
    from fastapi.testclient import TestClient
    from backend.finance.app import app

    client = TestClient(app)
    r = client.get("/hardship")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["registry_loaded"] is False
    assert body["calfresh"]["approved"] is False


def test_work_endpoint_all_phase3_actions(tmp_path, monkeypatch):
    _reset_idempotency(monkeypatch)
    _isolate_phase3_to_empty(monkeypatch, tmp_path)
    from fastapi.testclient import TestClient
    from backend.finance.app import app

    client = TestClient(app)
    for action in ("debts", "actions", "info_gaps", "hardship"):
        r = client.post(
            "/work",
            json={
                "task_id": f"t-{action}",
                "payload": {},
                "metadata": {"action": action},
            },
        )
        assert r.status_code == 200, f"{action}: {r.text}"
