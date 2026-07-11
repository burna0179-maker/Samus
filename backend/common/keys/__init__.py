"""Samus CORE key/secret management (E5 D15).

The shared-core key-management capability absorbed from the Optimus reference
(``core/security/keys/*``) and adapted to Samus's flat ``backend/common/``
layout. Four cooperating primitives:

* :mod:`backend.common.keys.dpapi_vault` — Windows DPAPI seal/unseal, the
  in-process counterpart of the operator's ``.cred`` store
  (``scripts/Samus.Secrets.psm1``); degrades cleanly off-Windows.
* :mod:`backend.common.keys.key_vault` — HKDF-SHA256 purpose-specific key
  derivation over ONE master (resolved from Samus's existing
  ``Settings`` secret surface), reusing Samus's own
  :func:`backend.common.kdf.hkdf_sha256`.
* :mod:`backend.common.keys.memory_enclave` — Fernet-sealed in-memory KV
  store + a best-effort zeroizing :class:`SecretHandle`.
* :mod:`backend.common.keys.secret_rotation_manager` — flag-gated rotation
  cycle (dormant by default) with an injected audit-ledger seam.

No secret material is embedded here; secrets flow in via the operator's
DPAPI ``.cred`` store -> launcher env -> ``Settings``. The layer is
fail-closed: an absent master degrades to a deterministic dev key (boot is
never blocked) and ``key_vault.master_is_configured()`` is the gate for any
caller that must not trust the dev fallback.
"""

from __future__ import annotations

from .dpapi_vault import (
    DpapiError,
    DpapiUnavailable,
    SCOPE_CURRENT_USER,
    SCOPE_LOCAL_MACHINE,
    dpapi_available,
    seal,
    unseal,
)
from .key_vault import (
    PURPOSES,
    ROTATION_GRACE_SEC,
    SAMUS_VAULT_SALT_V1,
    KeyVault,
    get_key_vault,
    master_is_configured,
    reset_key_vault_for_tests,
)
from .memory_enclave import (
    MemoryEnclave,
    SecretHandle,
    get_memory_enclave,
    reset_memory_enclave_for_tests,
)
from .secret_rotation_manager import (
    SECRET_ROTATION_EVENT,
    SecretRotationManager,
    bind_ledger,
    get_secret_rotation_manager,
    reset_secret_rotation_manager_for_tests,
)

__all__ = [
    # dpapi_vault
    "DpapiError",
    "DpapiUnavailable",
    "SCOPE_CURRENT_USER",
    "SCOPE_LOCAL_MACHINE",
    "dpapi_available",
    "seal",
    "unseal",
    # key_vault
    "PURPOSES",
    "ROTATION_GRACE_SEC",
    "SAMUS_VAULT_SALT_V1",
    "KeyVault",
    "get_key_vault",
    "master_is_configured",
    "reset_key_vault_for_tests",
    # memory_enclave
    "MemoryEnclave",
    "SecretHandle",
    "get_memory_enclave",
    "reset_memory_enclave_for_tests",
    # secret_rotation_manager
    "SECRET_ROTATION_EVENT",
    "SecretRotationManager",
    "bind_ledger",
    "get_secret_rotation_manager",
    "reset_secret_rotation_manager_for_tests",
]
