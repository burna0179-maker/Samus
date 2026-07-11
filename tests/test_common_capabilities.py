"""SERVICE_CAPABILITIES registry + check_capability behavior."""
from __future__ import annotations

import pytest
from fastapi import HTTPException


def test_capabilities_registry_includes_prospecting():
    from backend.common.capabilities import SERVICE_CAPABILITIES
    assert "prospecting" in SERVICE_CAPABILITIES
    assert {"discover", "build_call_sheet"} <= SERVICE_CAPABILITIES["prospecting"]


def test_check_capability_passes_for_registered():
    from backend.common.capabilities import check_capability
    check_capability("prospecting", "discover")  # no raise


def test_check_capability_denies_unregistered_capability():
    from backend.common.capabilities import check_capability
    with pytest.raises(HTTPException) as exc:
        check_capability("prospecting", "delete_all")
    assert exc.value.status_code == 403
    assert "capability denied" in exc.value.detail


def test_check_capability_denies_unknown_service():
    from backend.common.capabilities import check_capability
    with pytest.raises(HTTPException) as exc:
        check_capability("nonexistent_service", "discover")
    assert exc.value.status_code == 403


def test_every_capability_service_is_gateway_routable():
    """Regression: every workcell in SERVICE_CAPABILITIES (except gateway
    itself, which is the router) must also appear in the gateway_urls
    iteration list in bootstrap_settings.

    Without this, you can ship a new workcell + its capabilities + its
    container without realizing the gateway will return 404 'unknown
    target' when anything tries to dispatch to it. The CRM workcell hit
    this on first deploy — caught only at runtime. This test now catches
    it at commit time.
    """
    import inspect
    from backend.common.capabilities import SERVICE_CAPABILITIES
    from backend.common import settings as settings_mod

    src = inspect.getsource(settings_mod.bootstrap_settings)
    missing = []
    for service in SERVICE_CAPABILITIES:
        # 'gateway' is the dispatcher itself; it never appears as a
        # routable target in its own gateway_urls map.
        if service == "gateway":
            continue
        # The loop in bootstrap_settings looks like:
        #   for service in (..., "intake", "crm"):
        # so a literal string match against the source is the simplest
        # reliable check that doesn't depend on running bootstrap_settings.
        if f'"{service}"' not in src:
            missing.append(service)
    assert not missing, (
        f"capabilities-registered workcells missing from bootstrap_settings "
        f"gateway_urls iteration: {missing}. Add them to the tuple in "
        f"backend/common/settings.py so the gateway can resolve them."
    )
