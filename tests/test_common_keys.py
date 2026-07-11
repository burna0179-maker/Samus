"""Targeted tests for the CORE key/secret-management layer (E5 D15).

Covers: HKDF key vault get/derive + rotation, the zeroizing SecretHandle,
the Fernet MemoryEnclave round-trip + corruption degrade, the flag-gated
SecretRotationManager (fail-closed-when-off + rotate-on-interval), and the
DPAPI vault's off-platform fail-closed behaviour.

State is isolated per test via SAMUS_STATE_ROOT -> tmp_path so nothing
touches the real <code root>/state tree.
"""

from __future__ import annotations


import pytest

from backend.common import config
from backend.common.keys import (
    KeyVault,
    PURPOSES,
    SecretHandle,
    dpapi_available,
    get_key_vault,
    get_memory_enclave,
    get_secret_rotation_manager,
    master_is_configured,
    reset_key_vault_for_tests,
    reset_memory_enclave_for_tests,
    reset_secret_rotation_manager_for_tests,
)
from backend.common.keys import dpapi_vault, secret_rotation_manager


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    """Point all key-mgmt state at a per-test dir; reset every singleton."""
    monkeypatch.setenv("SAMUS_STATE_ROOT", str(tmp_path / "state"))
    # A real configured master so master_is_configured() is True by default.
    monkeypatch.setenv("SAMUS_LEDGER_SECRET_KEY", "a" * 64)  # 32 hex bytes
    monkeypatch.delenv("SAMUS_SECRET_ROTATION_ENABLED", raising=False)
    config.reload_settings()
    reset_key_vault_for_tests()
    reset_memory_enclave_for_tests()
    reset_secret_rotation_manager_for_tests()
    yield
    reset_key_vault_for_tests()
    reset_memory_enclave_for_tests()
    reset_secret_rotation_manager_for_tests()
    config.reload_settings()


# --------------------------------------------------------------------------- #
# KeyVault
# --------------------------------------------------------------------------- #


def test_key_vault_derive_is_deterministic_and_purpose_separated():
    v = get_key_vault()
    assert v.configured is True
    k1 = v.derive("hmac")
    k2 = v.derive("hmac")
    assert k1 == k2  # cached + deterministic
    assert len(k1) == 32
    # Different purposes must give different keys.
    assert v.derive("token") != k1
    # Custom length honoured.
    assert len(v.derive("audit", length=64)) == 64


def test_key_vault_rejects_unknown_purpose():
    v = get_key_vault()
    with pytest.raises(ValueError):
        v.derive("not-a-purpose")
    assert set(PURPOSES) == {
        "hmac",
        "tls",
        "governance",
        "transit",
        "enclave",
        "token",
        "audit",
    }


def test_key_vault_rotate_changes_derived_keys():
    v = get_key_vault()
    before = v.derive("transit")
    gen0 = v.generation
    v.rotate()
    assert v.generation == gen0 + 1
    after = v.derive("transit")
    assert after != before  # generation folded into HKDF info


def test_key_vault_dev_fallback_when_no_secret(monkeypatch):
    # No configured secret -> deterministic dev master, fail-closed gate False.
    monkeypatch.delenv("SAMUS_LEDGER_SECRET_KEY", raising=False)
    monkeypatch.setenv("SAMUS_SHARED_HMAC_KEY", "")
    config.reload_settings()
    reset_key_vault_for_tests()
    assert master_is_configured() is False
    v = get_key_vault()
    assert v.configured is False
    # Still derives (boot is never blocked) and is reproducible.
    assert v.derive("hmac") == KeyVault.from_settings().derive("hmac")


# --------------------------------------------------------------------------- #
# SecretHandle (zeroizing)
# --------------------------------------------------------------------------- #


def test_secret_handle_zeroizes_on_clear():
    h = SecretHandle("super-secret-value")
    assert h.bytes() == b"super-secret-value"
    h.clear()
    assert h.cleared is True
    with pytest.raises(ValueError):
        h.bytes()
    # Idempotent.
    h.clear()


def test_secret_handle_context_manager_clears_on_exit():
    with SecretHandle(b"\x01\x02\x03") as h:
        assert h.bytes() == b"\x01\x02\x03"
    assert h.cleared is True
    with pytest.raises(ValueError):
        h.bytes()


def test_secret_handle_never_reveals_in_repr():
    h = SecretHandle("topsecret")
    assert "topsecret" not in repr(h)
    assert "topsecret" not in str(h)
    h.clear()
    assert "cleared" in repr(h)


def test_secret_handle_with_bytes_scoped_read():
    h = SecretHandle("abc")
    assert h.with_bytes(lambda b: b.upper()) == b"ABC"
    h.clear()


# --------------------------------------------------------------------------- #
# MemoryEnclave
# --------------------------------------------------------------------------- #


def test_memory_enclave_put_get_flush_load_roundtrip():
    enc = get_memory_enclave()
    enc.put("api_key", "value-123")
    enc.put("count", 7)
    enc.flush()
    # New singleton reads the sealed file back.
    reset_memory_enclave_for_tests()
    enc2 = get_memory_enclave()
    assert enc2.get("api_key") == "value-123"
    assert enc2.get("count") == 7
    assert enc2.get("missing", "dflt") == "dflt"


def test_memory_enclave_delete():
    enc = get_memory_enclave()
    enc.put("k", "v")
    enc.delete("k")
    assert enc.get("k") is None


def test_memory_enclave_corrupt_seal_degrades_to_empty(tmp_path):
    enc = get_memory_enclave()
    enc.put("k", "v")
    enc.flush()
    # Corrupt the seal on disk.
    seal_file = enc._seal_path  # noqa: SLF001 - test inspects the path
    seal_file.write_bytes(b"not-a-valid-fernet-token")
    reset_memory_enclave_for_tests()
    enc2 = get_memory_enclave()  # load() must not raise
    assert enc2.get("k") is None


# --------------------------------------------------------------------------- #
# SecretRotationManager (flag-gated)
# --------------------------------------------------------------------------- #


def test_rotation_no_op_when_disabled(monkeypatch):
    monkeypatch.setenv("SAMUS_SECRET_ROTATION_ENABLED", "false")
    config.reload_settings()
    reset_secret_rotation_manager_for_tests()
    mgr = get_secret_rotation_manager()
    assert mgr.tick() is None
    assert mgr.generation == 0


def test_rotation_fires_when_enabled_and_interval_elapsed(monkeypatch):
    monkeypatch.setattr(secret_rotation_manager, "_rotation_enabled", lambda: True)
    monkeypatch.setattr(secret_rotation_manager, "_rotation_interval_sec", lambda: 0)
    reset_secret_rotation_manager_for_tests()
    reset_key_vault_for_tests()
    vault_gen_before = get_key_vault().generation
    mgr = get_secret_rotation_manager()
    assert mgr.tick() == "rotated"
    assert mgr.generation == 1
    assert get_key_vault().generation == vault_gen_before + 1
    # A fingerprint backup was recorded.
    backups = list((mgr._backup_dir).glob("*.fp"))  # noqa: SLF001
    assert len(backups) == 1


def test_rotation_emits_to_injected_ledger(monkeypatch):
    monkeypatch.setattr(secret_rotation_manager, "_rotation_enabled", lambda: True)
    monkeypatch.setattr(secret_rotation_manager, "_rotation_interval_sec", lambda: 0)
    events: list[tuple[str, dict]] = []

    class _Ledger:
        def append(self, event_type, payload):
            events.append((event_type, payload))
            return {"entry_hash": "deadbeef"}

    reset_secret_rotation_manager_for_tests()
    mgr = get_secret_rotation_manager()
    mgr.bind_ledger(_Ledger())
    assert mgr.tick() == "rotated"
    assert len(events) == 1
    assert events[0][0] == secret_rotation_manager.SECRET_ROTATION_EVENT
    assert events[0][1]["generation"] == 1


def test_rotation_interval_not_elapsed_is_noop(monkeypatch):
    monkeypatch.setattr(secret_rotation_manager, "_rotation_enabled", lambda: True)
    monkeypatch.setattr(secret_rotation_manager, "_rotation_interval_sec", lambda: 10_000)
    reset_secret_rotation_manager_for_tests()
    mgr = get_secret_rotation_manager()
    assert mgr.tick() == "rotated"  # first ever fire (last_rotated_at=0)
    assert mgr.tick() is None  # interval not elapsed on the second tick


# --------------------------------------------------------------------------- #
# DPAPI vault
# --------------------------------------------------------------------------- #


def test_dpapi_available_never_raises():
    # Just exercises the guard; value depends on platform.
    assert isinstance(dpapi_available(), bool)


def test_dpapi_seal_fails_closed_when_unavailable(monkeypatch):
    # Force the unavailable branch regardless of platform.
    monkeypatch.setattr(dpapi_vault, "_win32crypt", None)
    monkeypatch.setattr(dpapi_vault.sys, "platform", "linux")
    assert dpapi_vault.dpapi_available() is False
    with pytest.raises(dpapi_vault.DpapiUnavailable):
        dpapi_vault.seal(b"x")
    with pytest.raises(dpapi_vault.DpapiUnavailable):
        dpapi_vault.unseal(b"x")


def test_dpapi_seal_type_check():
    with pytest.raises(TypeError):
        dpapi_vault.seal("not-bytes")  # type: ignore[arg-type]


def test_dpapi_roundtrip_when_available():
    if not dpapi_available():
        pytest.skip("DPAPI not available on this platform")
    blob = dpapi_vault.seal(b"secret-bytes")
    assert blob != b"secret-bytes"
    assert dpapi_vault.unseal(blob) == b"secret-bytes"
