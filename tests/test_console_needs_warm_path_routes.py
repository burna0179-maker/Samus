"""Operator console: /api/console/needs_warm_path list + promote.

Reuses the gateway create_app boot pattern from
``test_app_boot_operator_console_pack``. The fixture mirrors that file so the
G8 routes are exercised through the same bearer-gated path the live console
uses.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient


_phase_a_pending = False
_pending_reason = ""

try:
    from backend.common import autonomy, dlq, governance  # noqa: F401

    if not (hasattr(governance, "classify_risk") and hasattr(governance, "approval_decision")):
        _phase_a_pending = True
        _pending_reason = "governance interface incomplete"
    if not hasattr(autonomy, "run_cycle"):
        _phase_a_pending = True
        _pending_reason = "autonomy.run_cycle missing"
    for _n in ("enqueue_failure", "read_pending", "read_archive"):
        if not hasattr(dlq, _n):
            _phase_a_pending = True
            _pending_reason = f"dlq.{_n} missing"
            break
except (ImportError, AttributeError) as exc:
    _phase_a_pending = True
    _pending_reason = f"common module missing: {exc}"

pytestmark = pytest.mark.skipif(
    _phase_a_pending, reason=f"depends on Phase A rewrite ({_pending_reason})",
)


_BEARER = "g8-test-bearer"


@pytest.fixture
def gateway_client(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SAMUS_SHARED_HMAC_KEY", "test-hmac-key")
    data_root = tmp_path / "samus_data"
    data_root.mkdir()
    monkeypatch.setenv("SAMUS_DATA_ROOT", str(data_root))
    monkeypatch.setenv("SAMUS_OPERATOR_TOKEN", _BEARER)
    monkeypatch.setenv("SAMUS_ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    (tmp_path / "artifacts").mkdir()
    monkeypatch.setenv("DDB_NEEDS_WARM_PATH_TABLE", "")

    from backend.common.settings import reload_settings
    reload_settings()

    from backend.gateway.app import create_app
    app = create_app()
    with TestClient(app) as c:
        yield c


def _admin() -> dict[str, str]:
    return {"Authorization": f"Bearer {_BEARER}"}


def test_list_requires_bearer(gateway_client):
    r = gateway_client.get("/api/console/needs_warm_path")
    assert r.status_code == 401


def test_list_returns_empty_initially(gateway_client):
    r = gateway_client.get("/api/console/needs_warm_path", headers=_admin())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 0
    assert body["diverted"] == []


def test_promote_404_when_unknown(gateway_client):
    r = gateway_client.post(
        "/api/console/needs_warm_path/ghost/promote",
        headers=_admin(),
        json={"signal_kind": "rfp", "signal_source": "x"},
    )
    assert r.status_code == 404


def test_promote_rejects_invalid_signal_kind(gateway_client):
    # Seed a divert via the underlying module so the route has a row to promote.
    from backend.crm.needs_warm_path import divert

    class _P:
        prospect_id = "p1"
        company = "Acme"
        def model_dump(self):
            return {"prospect_id": "p1", "company": "Acme"}

    divert("p1", prospect=_P())

    r = gateway_client.post(
        "/api/console/needs_warm_path/p1/promote",
        headers=_admin(),
        json={"signal_kind": "vibes", "signal_source": "x"},
    )
    assert r.status_code == 400


def test_promote_succeeds_and_removes_from_pending(gateway_client):
    from backend.crm.needs_warm_path import divert

    class _P:
        prospect_id = "p2"
        company = "Beta"
        def model_dump(self):
            return {"prospect_id": "p2", "company": "Beta"}

    divert("p2", prospect=_P())

    r = gateway_client.post(
        "/api/console/needs_warm_path/p2/promote",
        headers=_admin(),
        json={
            "signal_kind": "rfp",
            "signal_source": "https://gov.example/rfp/1",
            "operator_id": "alex",
        },
    )
    assert r.status_code == 200, r.text
    promoted = r.json()["promoted"]
    assert promoted["status"] == "promoted"
    assert promoted["promoted_signal_kind"] == "rfp"

    r2 = gateway_client.get("/api/console/needs_warm_path", headers=_admin())
    assert all(d["prospect_id"] != "p2" for d in r2.json()["diverted"])
