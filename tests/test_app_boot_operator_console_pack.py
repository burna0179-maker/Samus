"""End-to-end: gateway create_app() auto-mounts the operator_console pack.

Proves the Canon §4 pack wiring works for Samus's microservices layout:
the gateway workcell's ``create_app`` imports
``backend.packs.operator_console`` and calls its top-level
``register(app)`` hook, so the SPA shell + JSON API surface that result
are reachable through ``TestClient`` without any caller having to mount
the pack by hand.

Samus has no manifest resolver / profile JSON (unlike Major) -- each
workcell is its own FastAPI app -- so this test asserts the
gateway-specific shape: pack lives at ``backend.packs.operator_console``,
the gateway is the only workcell that mounts it, and the SPA shell +
``/api/console/*`` JSON endpoints come up through the gateway port.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


# Mirror the gateway-app smoke skip-gate -- without governance/autonomy/dlq
# in place ``backend.gateway.app`` import-time fails and there is no point
# asserting on pack wiring.
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
    for _name in ("enqueue_failure", "read_pending", "read_archive"):
        if not hasattr(dlq, _name):
            _phase_a_pending = True
            _pending_reason = f"dlq.{_name} missing"
            break
except (ImportError, AttributeError) as exc:
    _phase_a_pending = True
    _pending_reason = f"common module missing: {exc}"

pytestmark = pytest.mark.skipif(
    _phase_a_pending, reason=f"depends on Phase A rewrite landing ({_pending_reason})"
)


_BEARER = "boot-test-bearer-token"


@pytest.fixture
def gateway_client(tmp_path: Path, monkeypatch):
    """Boot the gateway via create_app() with a tmp data root + operator token."""
    # Gateway requires the shared HMAC key in non-development env. Tests
    # run with the development default, but set it anyway for parity with
    # the other gateway smoke fixtures.
    monkeypatch.setenv("SAMUS_SHARED_HMAC_KEY", "test-hmac-key")

    # Point the pack at a tmp data root so the auto-seeded persona
    # registry + sqlite WAL history land in pytest's tmp tree, not
    # /opt/samus/data.
    data_root = tmp_path / "samus_data"
    data_root.mkdir()
    monkeypatch.setenv("SAMUS_DATA_ROOT", str(data_root))
    monkeypatch.setenv("SAMUS_OPERATOR_TOKEN", _BEARER)

    # Force a fresh settings load so the env mutations above are picked up.
    from backend.common.settings import reload_settings
    reload_settings()

    from backend.gateway.app import create_app

    app = create_app()
    with TestClient(app) as c:
        yield c


def _admin() -> dict[str, str]:
    return {"Authorization": f"Bearer {_BEARER}"}


def test_console_shell_is_mounted_by_create_app(gateway_client):
    """The Jinja SPA shell answers on the gateway port."""
    r = gateway_client.get("/console")
    assert r.status_code == 200
    assert "text/html" in r.headers.get("content-type", "")


def test_console_static_route_is_mounted(gateway_client):
    """The static mount is wired -- missing asset 404s rather than 405s."""
    r = gateway_client.get("/console/static/__missing__.js")
    assert r.status_code == 404


def test_api_console_state_requires_bearer(gateway_client):
    """With SAMUS_OPERATOR_TOKEN set the JSON API gates on bearer."""
    r = gateway_client.get("/api/console/state")
    assert r.status_code == 401


def test_api_console_state_returns_default_persona(gateway_client):
    """The state endpoint reports samus_console as the default persona.

    Unlike Major's port, Samus's persona manager has no auto-seeder
    (``backend/standard/persona/persona_manager.py`` ships a bare
    JSON-backed registry; the hand-curated baseline lives in
    ``Samus/data/identity/personas/personas.json`` and is loaded at
    boot when present). For a fresh tmp data root the registry comes
    up empty, but ``default_persona`` is the pod constant -- the gate
    we care about for boot-wiring correctness.
    """
    r = gateway_client.get("/api/console/state", headers=_admin())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["default_persona"] == "samus_console"
    # persona_count may be 0 on a tmp data root (no auto-seed in Samus
    # standard/persona); see docstring above.
    assert "persona_count" in body


def test_api_console_personas_endpoint_lists_registry(gateway_client):
    """The personas listing endpoint is reachable through the bearer gate.

    On a tmp data root the registry is empty (Samus's persona manager
    has no auto-seeder); the boot-wiring assertion is that the route is
    mounted + bearer-gated + returns a well-formed envelope, not that
    any particular facet is pre-seeded.
    """
    r = gateway_client.get("/api/console/personas", headers=_admin())
    assert r.status_code == 200, r.text
    body = r.json()
    assert "personas" in body
    assert isinstance(body["personas"], list)


def test_pack_state_is_attached_to_app(gateway_client):
    """Pack contract: pod stashes OperatorConsoleState on app.state."""
    # TestClient exposes the underlying app via .app -- confirm the
    # gateway's create_app placed the pack's state object there, which
    # is the pack's promise that downstream code (other gateway routes,
    # background tasks) can introspect the operator surface.
    app_state = gateway_client.app.state
    assert hasattr(app_state, "operator_console"), "pack did not attach state"
    state = app_state.operator_console
    assert state.default_persona == "samus_console"
    assert state.api_token == _BEARER
