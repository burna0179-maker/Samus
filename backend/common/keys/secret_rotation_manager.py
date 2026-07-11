"""Automated key-vault rotation cycle (CORE — E5 D15).

Manages the *rotation cycle* in-memory and persists state to
``<state>/security/rotation_state.json``. It does NOT rewrite the operator's
on-disk DPAPI ``.cred`` store (``scripts/Samus.Secrets.psm1``) or the
``.env`` — the master secret stays the operator's responsibility. What it
provides:

* :meth:`SecretRotationManager.tick` — called by a scheduler to detect when a
  rotation interval has elapsed; bumps the
  :class:`~backend.common.keys.key_vault.KeyVault` generation (so all derived
  sub-keys roll forward) and records a SHA-256 fingerprint backup under
  ``<state>/security/secret_backups/``.
* An audit receipt on every rotation, emitted through an **injected** ledger
  seam.

Flag gate (fail-closed, dormant by default)
-------------------------------------------
``tick`` is a no-op unless ``samus_secret_rotation_enabled`` is on. The gate
is resolved through Samus's runtime flag layer
(:func:`backend.common.flags.runtime.is_enabled`) with the boot-time
``Settings`` value as the eager fallback — so an operator can flip rotation
on without a restart, and an uninitialised flag store falls back to the
(default-OFF) settings value. Behaviour is unchanged until armed.

The ledger seam
---------------
The ledger is an **injected** dependency so this CORE primitive does not hard-
depend on a heavier data-plane module:

* ``SecretRotationManager(ledger=...)`` accepts any object exposing
  ``append(event_type, payload)`` or ``record(event_type, payload)``.
* When ``ledger=None`` the manager still rotates — it simply skips the audit
  receipt (documented degraded-but-functional seam).
* :func:`bind_ledger` lets a later boot phase wire a live ledger into the
  process-wide singleton.

Absorption, not cloning: adapted from Optimus
``core/security/keys/secret_rotation_manager.py`` to Samus's flat
``backend/common/`` layout (state via ``state_paths``, gate via the Samus
flag runtime, master fingerprint over Samus's resolved master).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from ..state_paths import state_path
from .key_vault import _resolve_master, get_key_vault

log = logging.getLogger("samus.common.keys.secret_rotation")

#: Ledger event type emitted on a completed rotation.
SECRET_ROTATION_EVENT = "security.secret_rotation_completed"


def _atomic_write_text(path: Path, text: str) -> None:
    """Atomic UTF-8 write via temp-file + ``os.replace`` (package convention)."""
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


def _emit_to_ledger(ledger: Any, event_type: str, payload: dict[str, Any]) -> str | None:
    """Emit ``payload`` through a duck-typed ledger; return the receipt hash.

    Accepts ``append`` (returns an object with an ``entry_hash`` attr) and
    ``record`` (returns a dict with an ``entry_hash`` key). Returns ``None``
    when no ledger is wired or the emit fails — rotation never blocks on
    audit.
    """
    if ledger is None:
        return None
    try:
        emit = getattr(ledger, "append", None) or getattr(ledger, "record", None)
        if emit is None:
            return None
        receipt = emit(event_type, payload)
    except Exception:  # noqa: BLE001 — audit must never block rotation
        log.exception("ledger emit failed for %s", event_type)
        return None
    if receipt is None:
        return None
    entry_hash = getattr(receipt, "entry_hash", None)
    if entry_hash:
        return str(entry_hash)
    if isinstance(receipt, dict):
        h = receipt.get("entry_hash") or receipt.get("hmac")
        return str(h) if h else None
    return None


def _rotation_enabled() -> bool:
    """Resolve the rotation gate: runtime flag over the boot settings value.

    Fail-closed: any failure to resolve settings leaves rotation OFF.
    """
    try:
        from ..config import get_settings
        from ..flags.runtime import is_enabled

        fallback = bool(get_settings().samus_secret_rotation_enabled)
        return is_enabled("samus_secret_rotation_enabled", fallback)
    except Exception:  # noqa: BLE001 — unknown == off
        return False


def _rotation_interval_sec() -> int:
    try:
        from ..config import get_settings

        return int(get_settings().samus_secret_rotation_interval_sec)
    except Exception:  # noqa: BLE001
        return 86400


def _rotation_overlap_sec() -> int:
    try:
        from ..config import get_settings

        return int(get_settings().samus_secret_rotation_overlap_sec)
    except Exception:  # noqa: BLE001
        return 3600


class SecretRotationManager:
    """Runs the rotation cycle; persists state; emits an audit receipt."""

    def __init__(
        self,
        state_path: Path,
        backup_dir: Path,
        *,
        ledger: Any | None = None,
    ) -> None:
        self._state_path = state_path
        self._backup_dir = backup_dir
        self._ledger = ledger
        self._lock = threading.Lock()
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        self._backup_dir.mkdir(parents=True, exist_ok=True)
        self._state = self._load_state()

    def bind_ledger(self, ledger: Any) -> None:
        """Wire (or re-wire) the audit ledger after construction."""
        with self._lock:
            self._ledger = ledger

    def _load_state(self) -> dict[str, Any]:
        if not self._state_path.exists():
            return {"last_rotated_at": 0.0, "generation": 0}
        try:
            return json.loads(self._state_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {"last_rotated_at": 0.0, "generation": 0}

    def _save_state(self) -> None:
        _atomic_write_text(self._state_path, json.dumps(self._state, default=str))

    def tick(self) -> str | None:
        """Return ``"rotated"`` if a rotation fired this tick, else ``None``.

        A no-op when ``samus_secret_rotation_enabled`` is off (the default)
        or the rotation interval has not yet elapsed.
        """
        if not _rotation_enabled():
            return None
        interval = _rotation_interval_sec()
        with self._lock:
            now = time.time()
            elapsed = now - float(self._state.get("last_rotated_at", 0.0))
            if elapsed < interval:
                return None
            # Fingerprint-backup the current master, bump the vault.
            master_bytes, _configured = _resolve_master()
            fp = hashlib.sha256(master_bytes).hexdigest()[:16]
            backup_file = self._backup_dir / f"key-{int(now)}-{fp}.fp"
            _atomic_write_text(backup_file, fp)
            get_key_vault().rotate()
            self._state["last_rotated_at"] = now
            self._state["generation"] = int(self._state.get("generation", 0)) + 1
            self._save_state()
            _emit_to_ledger(
                self._ledger,
                SECRET_ROTATION_EVENT,
                {
                    "generation": self._state["generation"],
                    "fingerprint": fp,
                    "overlap_window_sec": _rotation_overlap_sec(),
                },
            )
            return "rotated"

    @property
    def generation(self) -> int:
        with self._lock:
            return int(self._state.get("generation", 0))


_MANAGER: SecretRotationManager | None = None
_LOCK = threading.Lock()


def get_secret_rotation_manager() -> SecretRotationManager:
    """Return the process-wide :class:`SecretRotationManager` singleton.

    Built with ``ledger=None`` — the audit-degraded posture. A later boot
    phase calls :func:`bind_ledger` to wire a live ledger.
    """
    global _MANAGER
    if _MANAGER is None:
        with _LOCK:
            if _MANAGER is None:
                _MANAGER = SecretRotationManager(
                    state_path=state_path("security", "rotation_state.json"),
                    backup_dir=state_path("security", "secret_backups"),
                    ledger=None,
                )
    return _MANAGER


def bind_ledger(ledger: Any) -> None:
    """Wire a live ledger into the process-wide rotation-manager singleton.

    Idempotent — builds the singleton first if it does not yet exist.
    """
    get_secret_rotation_manager().bind_ledger(ledger)


def reset_secret_rotation_manager_for_tests() -> None:
    """Drop the cached singleton. Test-fixture helper."""
    global _MANAGER
    with _LOCK:
        _MANAGER = None


__all__ = [
    "SECRET_ROTATION_EVENT",
    "SecretRotationManager",
    "bind_ledger",
    "get_secret_rotation_manager",
    "reset_secret_rotation_manager_for_tests",
]
