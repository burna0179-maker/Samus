"""R-1 remediation — per-service HMAC keys + caller→callee authorization.

Covers the four pieces of the R-1 fix:

1. Per-service HMAC key resolution (``security.resolve_service_key``) with the
   mandatory shared-key fallback — a stack with only the shared key is
   unchanged.
2. Caller identity binding into the HMAC canonical string and onto
   ``request.state.caller_service`` after verification.
3. The ``CALLER_GRANTS`` matrix + ``authorize`` / ``authorize_caller_to_callee``
   grant / deny logic.
4. The three ``SAMUS_AUTHZ_MODE`` modes: ``off`` no-ops, ``audit`` allows +
   logs, ``enforce`` denies.

The full pytest session sets ``SAMUS_DISABLE_HMAC_MIDDLEWARE=1`` in
conftest.py; the middleware-level tests here un-set it per-test (mirroring
test_common_app_factory_hmac_default.py).
"""

from __future__ import annotations

import logging

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request


# ---------------------------------------------------------------------------
# 1. Per-service HMAC key resolution + shared-key fallback
# ---------------------------------------------------------------------------


def test_per_service_key_env_name():
    from backend.common.security import per_service_key_env_name

    assert per_service_key_env_name("crm") == "SAMUS_HMAC_KEY_CRM"
    # Multi-word workcell names normalise underscores cleanly.
    assert per_service_key_env_name("signal_filter") == "SAMUS_HMAC_KEY_SIGNAL_FILTER"


def test_resolve_service_key_falls_back_to_shared(monkeypatch):
    """No per-service key configured -> shared key is used (non-breaking)."""
    from backend.common.security import resolve_service_key

    monkeypatch.delenv("SAMUS_HMAC_KEY_CRM", raising=False)
    assert resolve_service_key("crm", "shared-secret") == "shared-secret"


def test_resolve_service_key_prefers_per_service(monkeypatch):
    """A configured per-service key wins over the shared key."""
    from backend.common.security import resolve_service_key

    monkeypatch.setenv("SAMUS_HMAC_KEY_CRM", "crm-dedicated-key")
    assert resolve_service_key("crm", "shared-secret") == "crm-dedicated-key"


def test_resolve_service_key_empty_when_nothing_configured(monkeypatch):
    from backend.common.security import resolve_service_key

    monkeypatch.delenv("SAMUS_HMAC_KEY_CRM", raising=False)
    assert resolve_service_key("crm", "") == ""


# ---------------------------------------------------------------------------
# 2. Caller-identity binding in the HMAC canonical string
# ---------------------------------------------------------------------------


def test_sign_request_caller_none_is_legacy_canonical():
    """caller=None reproduces the byte-identical pre-R-1 signature."""
    from backend.common.security import sign_request

    legacy = sign_request("k", "POST", "/work", "1700000000", "abc", b'{"x":1}')
    explicit_none = sign_request(
        "k",
        "POST",
        "/work",
        "1700000000",
        "abc",
        b'{"x":1}',
        caller=None,
    )
    assert legacy == explicit_none


def test_sign_request_caller_changes_signature():
    """Folding a caller into the MAC changes the signature -> unforgeable."""
    from backend.common.security import sign_request

    no_caller = sign_request("k", "POST", "/work", "1", "abc", b"{}")
    with_caller = sign_request("k", "POST", "/work", "1", "abc", b"{}", caller="crm")
    assert no_caller != with_caller
    # A different caller name yields a different signature.
    other = sign_request("k", "POST", "/work", "1", "abc", b"{}", caller="voice")
    assert with_caller != other


# ---------------------------------------------------------------------------
# 3. CALLER_GRANTS matrix — grant / deny
# ---------------------------------------------------------------------------


def test_gateway_is_authorized_for_everything():
    from backend.common.capabilities import is_authorized

    assert is_authorized("gateway", "crm", "write_opportunity")
    assert is_authorized("gateway", "memory", "delete")
    assert is_authorized("gateway", "voice", "initiate_call")


def test_outreach_granted_crm_write_conversation():
    from backend.common.capabilities import is_authorized

    assert is_authorized("outreach", "crm", "write_conversation")


def test_outreach_denied_crm_delete_capability_it_lacks():
    """outreach has a CRM grant but only for the listed capabilities."""
    from backend.common.capabilities import is_authorized

    # 'write_artifact' is a real CRM capability but NOT in outreach's grant.
    assert not is_authorized("outreach", "crm", "write_artifact")


def test_seo_has_no_direct_crm_grant():
    """seo dispatches THROUGH the gateway; it has no direct CRM grant."""
    from backend.common.capabilities import is_authorized, caller_reaches_callee

    assert not is_authorized("seo", "crm", "write_artifact")
    assert not caller_reaches_callee("seo", "crm")
    # seo CAN reach the gateway (its only mesh privilege).
    assert caller_reaches_callee("seo", "gateway")


def test_unknown_caller_denied_by_default():
    from backend.common.capabilities import is_authorized, caller_reaches_callee

    assert not is_authorized("unknown", "crm", "read_prospects")
    assert not caller_reaches_callee("unknown", "crm")


def test_external_caller_always_allowed():
    """Exempt/unsigned paths (caller='external') are never matrix-gated."""
    from backend.common.capabilities import is_authorized, caller_reaches_callee

    assert is_authorized("external", "crm", "write_opportunity")
    assert caller_reaches_callee("external", "finance")


def test_voice_reaches_memory_crm_finance_only():
    from backend.common.capabilities import caller_reaches_callee

    assert caller_reaches_callee("voice", "memory")
    assert caller_reaches_callee("voice", "crm")
    assert caller_reaches_callee("voice", "finance")
    # voice has no grant to seo / proposal.
    assert not caller_reaches_callee("voice", "seo")


# ---------------------------------------------------------------------------
# 4. authz_mode resolution
# ---------------------------------------------------------------------------


def test_authz_mode_defaults_enforce(monkeypatch):
    """ARMED by default — HOTL Tranche 1 operator decision (2026-07-05)."""
    from backend.common.capabilities import authz_mode

    monkeypatch.delenv("SAMUS_AUTHZ_MODE", raising=False)
    assert authz_mode() == "enforce"


@pytest.mark.parametrize("value", ["off", "audit", "enforce"])
def test_authz_mode_valid_values(monkeypatch, value):
    from backend.common.capabilities import authz_mode

    monkeypatch.setenv("SAMUS_AUTHZ_MODE", value)
    assert authz_mode() == value


def test_authz_mode_bad_value_falls_back_enforce(monkeypatch):
    """A typo falls back to the ARMED default, never silently disarms."""
    from backend.common.capabilities import authz_mode

    monkeypatch.setenv("SAMUS_AUTHZ_MODE", "ENFROCE")  # typo
    assert authz_mode() == "enforce"


# ---------------------------------------------------------------------------
# 4a. authorize() — the three modes
# ---------------------------------------------------------------------------


def test_authorize_off_mode_is_noop_even_on_denial():
    """off mode: a would-be-denied call returns without raising."""
    from backend.common.capabilities import authorize

    # outreach has no grant to memory at all — would be denied in enforce.
    authorize("outreach", "memory", "delete", mode="off")  # no raise


def test_authorize_audit_mode_allows_and_logs(caplog):
    from backend.common.capabilities import authorize

    with caplog.at_level(logging.WARNING, logger="samus.authz"):
        authorize("outreach", "memory", "delete", path="/delete", mode="audit")
    # Request allowed (no raise) but a structured would_deny line is logged.
    assert any("would_deny" in r.message for r in caplog.records)
    assert any("caller=outreach" in r.message for r in caplog.records)


def test_authorize_enforce_mode_denies():
    from backend.common.capabilities import authorize

    with pytest.raises(HTTPException) as ei:
        authorize("outreach", "memory", "delete", path="/delete", mode="enforce")
    assert ei.value.status_code == 403
    assert "authorization denied" in ei.value.detail


def test_authorize_enforce_mode_allows_a_real_grant():
    from backend.common.capabilities import authorize

    # voice -> finance:report_meter_event is a real grant.
    authorize("voice", "finance", "report_meter_event", mode="enforce")  # no raise


def test_authorize_unknown_caller_denied_in_enforce():
    from backend.common.capabilities import authorize

    with pytest.raises(HTTPException) as ei:
        authorize(None, "crm", "read_prospects", mode="enforce")
    assert ei.value.status_code == 403


def test_authorize_external_caller_never_gated():
    from backend.common.capabilities import authorize

    authorize("external", "crm", "write_opportunity", mode="enforce")  # no raise


# ---------------------------------------------------------------------------
# 4b. authorize_caller_to_callee() — coarse boundary gate
# ---------------------------------------------------------------------------


def test_coarse_gate_off_mode_always_allows():
    from backend.common.capabilities import authorize_caller_to_callee

    assert authorize_caller_to_callee("outreach", "memory", mode="off") is True


def test_coarse_gate_audit_allows_but_logs(caplog):
    from backend.common.capabilities import authorize_caller_to_callee

    with caplog.at_level(logging.WARNING, logger="samus.authz"):
        ok = authorize_caller_to_callee(
            "outreach",
            "memory",
            path="/x",
            mode="audit",
        )
    assert ok is True
    assert any("would_deny" in r.message for r in caplog.records)


def test_coarse_gate_enforce_denies_ungranted_callee():
    from backend.common.capabilities import authorize_caller_to_callee

    assert (
        authorize_caller_to_callee(
            "outreach",
            "memory",
            path="/x",
            mode="enforce",
        )
        is False
    )


def test_coarse_gate_enforce_allows_granted_callee():
    from backend.common.capabilities import authorize_caller_to_callee

    assert (
        authorize_caller_to_callee(
            "outreach",
            "crm",
            path="/x",
            mode="enforce",
        )
        is True
    )


# ---------------------------------------------------------------------------
# 5. check_capability — backward compatibility unchanged
# ---------------------------------------------------------------------------


def test_check_capability_unchanged_signature_and_behaviour():
    from backend.common.capabilities import check_capability

    check_capability("crm", "read_prospects")  # no raise
    with pytest.raises(HTTPException) as ei:
        check_capability("crm", "nuke_everything")
    assert ei.value.status_code == 403


def test_check_capability_for_runs_static_check_in_off_mode(monkeypatch):
    """check_capability_for still raises on a bad capability even in off mode."""
    from backend.common.capabilities import check_capability_for

    monkeypatch.setenv("SAMUS_AUTHZ_MODE", "off")
    with pytest.raises(HTTPException) as ei:
        check_capability_for(None, "crm", "not_a_capability")
    assert ei.value.status_code == 403


def test_check_capability_for_enforces_caller_in_enforce_mode(monkeypatch):
    """In enforce mode, an unknown caller is denied even for a valid capability."""
    from backend.common.capabilities import check_capability_for

    monkeypatch.setenv("SAMUS_AUTHZ_MODE", "enforce")
    with pytest.raises(HTTPException) as ei:
        # None request -> 'unknown' caller -> deny in enforce.
        check_capability_for(None, "crm", "read_prospects")
    assert ei.value.status_code == 403


# ---------------------------------------------------------------------------
# 6. Middleware end-to-end — caller identity attached + verified
# ---------------------------------------------------------------------------


@pytest.fixture
def hmac_env(monkeypatch):
    """Un-set the test escape hatch + give a non-signer workcell a shared key."""
    monkeypatch.delenv("SAMUS_DISABLE_HMAC_MIDDLEWARE", raising=False)
    monkeypatch.delenv("SAMUS_AUTHZ_MODE", raising=False)
    monkeypatch.setenv("SAMUS_SHARED_HMAC_KEY", "z" * 32)
    monkeypatch.setenv("SAMUS_SERVICE", "crm")  # non-signer callee workcell
    from backend.common.settings import reload_settings

    reload_settings()
    yield monkeypatch
    monkeypatch.setenv("SAMUS_DISABLE_HMAC_MIDDLEWARE", "1")
    reload_settings()


def _signed_headers(secret, method, path, body, caller):
    from backend.common import security

    ts = security.generate_timestamp()
    nonce = security.generate_nonce()
    sig = security.sign_request(secret, method, path, ts, nonce, body, caller=caller)
    return {
        "X-Samus-Timestamp": ts,
        "X-Samus-Nonce": nonce,
        "X-Samus-Signature": sig,
        "X-Samus-Caller": caller,
        "Content-Type": "application/json",
    }


def test_middleware_attaches_caller_identity(hmac_env):
    """A correctly-signed request gets request.state.caller_service set."""
    from backend.common.app_factory import create_base_app

    app = create_base_app(service_name="crm")

    @app.post("/probe")
    async def _probe(request: Request):  # pragma: no cover - via TestClient
        return {"caller": request.state.caller_service}

    client = TestClient(app)
    body = b"{}"
    headers = _signed_headers("z" * 32, "POST", "/probe", body, caller="gateway")
    resp = client.post("/probe", content=body, headers=headers)
    assert resp.status_code == 200
    assert resp.json()["caller"] == "gateway"


def test_middleware_rejects_forged_caller_header(hmac_env):
    """Tampering with X-Samus-Caller after signing invalidates the signature."""
    from backend.common.app_factory import create_base_app

    app = create_base_app(service_name="crm")

    @app.post("/probe")
    async def _probe():  # pragma: no cover
        return {"ok": True}

    client = TestClient(app)
    body = b"{}"
    headers = _signed_headers("z" * 32, "POST", "/probe", body, caller="gateway")
    # Attacker swaps the caller header to something with more privilege —
    # the signature was computed over caller='gateway', so this must fail.
    headers["X-Samus-Caller"] = "fulfillment"
    resp = client.post("/probe", content=body, headers=headers)
    assert resp.status_code == 401
    assert resp.json()["error"] == "hmac_signature_mismatch"


def test_middleware_shared_key_fallback_still_verifies(hmac_env):
    """No per-service key configured -> shared key verifies (non-breaking)."""
    hmac_env.delenv("SAMUS_HMAC_KEY_GATEWAY", raising=False)
    from backend.common.app_factory import create_base_app

    app = create_base_app(service_name="crm")

    @app.post("/probe")
    async def _probe():  # pragma: no cover
        return {"ok": True}

    client = TestClient(app)
    body = b'{"a":1}'
    # Signed with the shared key (caller has no dedicated key).
    headers = _signed_headers("z" * 32, "POST", "/probe", body, caller="gateway")
    resp = client.post("/probe", content=body, headers=headers)
    assert resp.status_code == 200


def test_middleware_per_service_key_verifies(hmac_env):
    """A caller with a dedicated key signs with it; the verifier resolves it."""
    hmac_env.setenv("SAMUS_HMAC_KEY_GATEWAY", "gateway-dedicated-secret")
    from backend.common.settings import reload_settings

    reload_settings()
    from backend.common.app_factory import create_base_app

    app = create_base_app(service_name="crm")

    @app.post("/probe")
    async def _probe():  # pragma: no cover
        return {"ok": True}

    client = TestClient(app)
    body = b"{}"
    # Signed with the gateway's dedicated key.
    headers = _signed_headers(
        "gateway-dedicated-secret",
        "POST",
        "/probe",
        body,
        caller="gateway",
    )
    resp = client.post("/probe", content=body, headers=headers)
    assert resp.status_code == 200

    # A request signed with the WRONG key (the shared key) is rejected
    # because the verifier resolves the gateway's dedicated key.
    bad = _signed_headers("z" * 32, "POST", "/probe", body, caller="gateway")
    resp_bad = client.post("/probe", content=body, headers=bad)
    assert resp_bad.status_code == 401


def test_middleware_enforce_mode_denies_ungranted_caller(hmac_env):
    """SAMUS_AUTHZ_MODE=enforce: a caller with no grant to this callee -> 403."""
    hmac_env.setenv("SAMUS_AUTHZ_MODE", "enforce")
    from backend.common.app_factory import create_base_app

    # callee is 'memory'; caller 'outreach' has NO grant to memory.
    hmac_env.setenv("SAMUS_SERVICE", "memory")
    from backend.common.settings import reload_settings

    reload_settings()
    app = create_base_app(service_name="memory")

    @app.post("/probe")
    async def _probe():  # pragma: no cover
        return {"ok": True}

    client = TestClient(app)
    body = b"{}"
    headers = _signed_headers("z" * 32, "POST", "/probe", body, caller="outreach")
    resp = client.post("/probe", content=body, headers=headers)
    assert resp.status_code == 403
    assert resp.json()["error"] == "authz_denied"


def test_middleware_enforce_mode_allows_granted_caller(hmac_env):
    """enforce mode: voice -> memory is a real grant -> request proceeds."""
    hmac_env.setenv("SAMUS_AUTHZ_MODE", "enforce")
    hmac_env.setenv("SAMUS_SERVICE", "memory")
    from backend.common.settings import reload_settings

    reload_settings()
    from backend.common.app_factory import create_base_app

    app = create_base_app(service_name="memory")

    @app.post("/probe")
    async def _probe():  # pragma: no cover
        return {"ok": True}

    client = TestClient(app)
    body = b"{}"
    headers = _signed_headers("z" * 32, "POST", "/probe", body, caller="voice")
    resp = client.post("/probe", content=body, headers=headers)
    assert resp.status_code == 200


def test_middleware_off_mode_skips_authz_gate(hmac_env):
    """Explicit off mode: even an ungranted caller passes (signature still checked)."""
    hmac_env.setenv("SAMUS_AUTHZ_MODE", "off")
    hmac_env.setenv("SAMUS_SERVICE", "memory")
    from backend.common.settings import reload_settings

    reload_settings()
    from backend.common.app_factory import create_base_app

    app = create_base_app(service_name="memory")

    @app.post("/probe")
    async def _probe():  # pragma: no cover
        return {"ok": True}

    client = TestClient(app)
    body = b"{}"
    # 'outreach' has no grant to memory, but off mode does not evaluate it.
    headers = _signed_headers("z" * 32, "POST", "/probe", body, caller="outreach")
    resp = client.post("/probe", content=body, headers=headers)
    assert resp.status_code == 200


def test_middleware_audit_mode_allows_ungranted_caller(hmac_env, caplog):
    """audit mode: ungranted caller is ALLOWED but a would_deny is logged."""
    hmac_env.setenv("SAMUS_AUTHZ_MODE", "audit")
    hmac_env.setenv("SAMUS_SERVICE", "memory")
    from backend.common.settings import reload_settings

    reload_settings()
    from backend.common.app_factory import create_base_app

    app = create_base_app(service_name="memory")

    @app.post("/probe")
    async def _probe():  # pragma: no cover
        return {"ok": True}

    client = TestClient(app)
    body = b"{}"
    headers = _signed_headers("z" * 32, "POST", "/probe", body, caller="outreach")
    with caplog.at_level(logging.WARNING, logger="samus.authz"):
        resp = client.post("/probe", content=body, headers=headers)
    assert resp.status_code == 200
    assert any("would_deny" in r.message for r in caplog.records)


def test_middleware_exempt_path_tagged_external(hmac_env):
    """/health is exempt -> caller tagged 'external', no signature needed."""
    from backend.common.app_factory import create_base_app

    app = create_base_app(service_name="crm")
    client = TestClient(app)
    resp = client.get("/health")
    assert resp.status_code == 200
