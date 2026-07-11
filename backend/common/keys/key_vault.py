"""HKDF-SHA256 purpose-specific key derivation (CORE — E5 D15).

The single CORE key vault for Samus. It derives purpose-specific sub-keys
from ONE long-lived master secret so the agent carries one root secret, not
one per consumer. Sub-keys are derived at runtime via HKDF-SHA256 and never
persisted; the keystore on disk holds only generation metadata (an epoch
counter + a creation timestamp).

Absorption, not cloning
-----------------------
This is the Optimus ``core/security/keys/key_vault.py`` capability adapted to
Samus's flat ``backend/common/`` layout:

* The HKDF primitive is Samus's EXISTING :func:`backend.common.kdf.hkdf_sha256`
  (RFC 5869, stdlib) — NOT a re-imported ``cryptography`` HKDF. The vault
  reuses the agent's own derivation primitive rather than duplicating it.
* The master resolves from Samus's existing secret surface (the env-bound
  :class:`~backend.common.config.Settings`):
  ``samus_ledger_secret_key`` -> ``shared_hmac_key`` -> a deterministic
  per-process dev key. Secrets are NEVER embedded here; they flow in via the
  operator's DPAPI ``.cred`` store (``scripts/Samus.Secrets.psm1``) ->
  launcher env -> ``Settings``.
* The keystore path resolves through ``backend.common.state_paths`` so it
  lands on the writable data volume in the container and under
  ``<code root>/state`` on the host — the same convention the flag store
  and ledgers already use.

Graceful degrade (fail-closed-by-construction)
----------------------------------------------
When no configured secret is present the vault falls back to a deterministic
per-process dev key and NEVER raises, so boot is never blocked. Callers that
require a real configured master consult :func:`master_is_configured`
(fail-closed gate) before trusting derived material for production-grade use.

The seven HKDF purposes mirror the ecosystem Security-Layer invariant
(Anita / Optimus): ``hmac, tls, governance, transit, enclave, token,
audit``.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path

from ..kdf import hkdf_sha256
from ..state_paths import state_path

#: The seven HKDF derivation purposes (ecosystem Security-Layer invariant).
PURPOSES: tuple[str, ...] = (
    "hmac",
    "tls",
    "governance",
    "transit",
    "enclave",
    "token",
    "audit",
)

#: Overlap window — old keys remain derivable for this long after a rotate.
ROTATION_GRACE_SEC = 3600

#: HKDF salt namespace for the Samus key vault. Distinct from the audit
#: ledger salt (``SAMUS_LEDGER_SALT_V1``) so a leak in one derivation domain
#: cannot forge the other. Bump only if the derivation scheme changes shape.
SAMUS_VAULT_SALT_V1: bytes = b"samus-keyvault-v1"

# Deterministic dev master: a fixed SHA-256 digest. Stable so a vault built
# without a configured secret still derives reproducible sub-keys within and
# across processes for the dev/test posture.
_DEV_MASTER = hashlib.sha256(b"samus/common/keys/key_vault/dev-master").digest()


def _resolve_master() -> tuple[bytes, bool]:
    """Resolve the HKDF master key + whether it came from real config.

    Resolution order (Samus's existing secret surface):

      1. ``Settings.samus_ledger_secret_key`` — the dedicated ledger-signing
         secret, the strongest configured root.
      2. ``Settings.shared_hmac_key`` — the inter-service HMAC root.
      3. A deterministic per-process dev key (degraded posture).

    A configured value is hex-decoded when it parses as hex, otherwise used
    as raw UTF-8 bytes. Returns ``(master_bytes, configured)`` where
    ``configured`` is ``False`` for the dev-key fallback. NEVER raises.
    """
    configured: str | None = None
    try:
        from ..config import get_settings

        s = get_settings()
        configured = (s.samus_ledger_secret_key or s.shared_hmac_key or "").strip()
    except Exception:  # pragma: no cover — Settings unavailable at boot
        configured = None
    if configured:
        try:
            return bytes.fromhex(configured), True
        except ValueError:
            return configured.encode("utf-8"), True
    return _DEV_MASTER, False


def master_is_configured() -> bool:
    """Return ``True`` only when a real (non-dev) master secret is present.

    Fail-closed gate: a caller that must NOT derive production-trust keys
    from the dev fallback checks this first. NEVER raises.
    """
    return _resolve_master()[1]


class KeyVault:
    """Derives sub-keys via HKDF-SHA256 with purpose-specific context."""

    def __init__(
        self,
        master: bytes,
        generation: int = 0,
        keystore_path: Path | None = None,
        *,
        configured: bool = False,
    ) -> None:
        self._master = master
        self._generation = generation
        self._keystore_path = keystore_path
        self._configured = configured
        self._cache: dict[str, bytes] = {}
        self._lock = threading.Lock()
        self._rotated_at = time.time()

    @classmethod
    def from_settings(cls) -> "KeyVault":
        """Build a vault from the process settings + persisted generation."""
        master, configured = _resolve_master()
        keystore = state_path("security", "vault", "keystore.enc")
        generation = 0
        if keystore.exists():
            try:
                meta = json.loads(keystore.read_text(encoding="utf-8"))
                generation = int(meta.get("generation", 0))
            except (json.JSONDecodeError, OSError, ValueError):
                generation = 0
        else:
            _atomic_write_text(
                keystore,
                json.dumps({"generation": 0, "created_at": time.time()}),
            )
        return cls(
            master=master,
            generation=generation,
            keystore_path=keystore,
            configured=configured,
        )

    @property
    def configured(self) -> bool:
        """``True`` when the master came from a real configured secret."""
        return self._configured

    def derive(self, purpose: str, length: int = 32) -> bytes:
        """Return a purpose-specific sub-key, HKDF-derived + cached.

        Raises :class:`ValueError` for an unknown purpose.
        """
        if purpose not in PURPOSES:
            raise ValueError(f"unknown purpose {purpose!r}; expected one of {PURPOSES}")
        cache_key = f"{purpose}:{self._generation}:{length}"
        with self._lock:
            cached = self._cache.get(cache_key)
            if cached is not None:
                return cached
            info = f"samus/v{self._generation}/{purpose}".encode()
            derived = hkdf_sha256(
                ikm=self._master,
                salt=SAMUS_VAULT_SALT_V1,
                info=info,
                length=length,
            )
            self._cache[cache_key] = derived
            return derived

    def rotate(self) -> None:
        """Bump the generation. Prior-generation keys remain derivable for
        ``ROTATION_GRACE_SEC`` (the caller keeps the old vault reachable).
        """
        with self._lock:
            self._generation += 1
            self._rotated_at = time.time()
            self._cache.clear()
            if self._keystore_path is not None:
                _atomic_write_text(
                    self._keystore_path,
                    json.dumps(
                        {
                            "generation": self._generation,
                            "rotated_at": self._rotated_at,
                        }
                    ),
                )

    @property
    def generation(self) -> int:
        return self._generation


def _atomic_write_text(path: Path, text: str) -> None:
    """Atomic UTF-8 write via temp-file + ``os.replace``.

    Matches the ``os.replace``-based atomic writes elsewhere in
    ``backend/common`` (flags store, persistence, budgets). Samus has no
    ``shared.utils.safe_io`` helper, so this is self-contained.
    """
    import os
    import tempfile

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


_VAULT: KeyVault | None = None
_VAULT_LOCK = threading.Lock()


def get_key_vault() -> KeyVault:
    """Return the process-wide :class:`KeyVault` singleton (lazy build)."""
    global _VAULT
    if _VAULT is None:
        with _VAULT_LOCK:
            if _VAULT is None:
                _VAULT = KeyVault.from_settings()
    return _VAULT


def reset_key_vault_for_tests() -> None:
    """Drop the cached singleton so the next :func:`get_key_vault` rebuilds
    via :meth:`KeyVault.from_settings`.

    Test fixtures use this to rebind the vault after changing the master
    secret; a self-heal recovery hook can use it for the same purpose when
    the vault needs to re-derive sub-keys from current settings.
    """
    global _VAULT
    with _VAULT_LOCK:
        _VAULT = None


__all__ = [
    "PURPOSES",
    "ROTATION_GRACE_SEC",
    "SAMUS_VAULT_SALT_V1",
    "KeyVault",
    "get_key_vault",
    "master_is_configured",
    "reset_key_vault_for_tests",
]
