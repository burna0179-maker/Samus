"""Fernet-encrypted in-memory software TEE + zeroizing secret handle
(CORE — E5 D15).

Two complementary primitives:

:class:`SecretHandle`
    A best-effort *zeroizing* in-memory handle for a single secret value.
    The plaintext is held in a ``bytearray`` (mutable, so it can be
    overwritten in place) and :meth:`SecretHandle.clear` zeroes those bytes.
    The handle is a context manager — leaving the ``with`` block zeroizes —
    and ``str(handle)`` / ``repr(handle)`` NEVER reveal the secret, so it
    cannot leak into logs or tracebacks. Python gives no hard guarantee that
    every copy is scrubbed (the interpreter may have made transient copies),
    so this is *defence in depth*, not a hardware enclave — it shrinks the
    window a secret sits readable in process memory.

:class:`MemoryEnclave`
    A small encrypted key/value store. Keys are sealed with a
    :class:`~backend.common.keys.key_vault.KeyVault`-derived ``"enclave"``
    sub-key. On :meth:`MemoryEnclave.flush` the store is serialised,
    Fernet-encrypted and atomically written to
    ``<state>/security/enclave/enclave.seal``. On :meth:`MemoryEnclave.load`
    the seal is decrypted; a corrupted or wrong-keyed seal degrades to an
    empty store rather than raising.

Absorption, not cloning
-----------------------
Adapted from Optimus ``core/security/keys/memory_enclave.py`` to Samus's
flat ``backend/common/`` layout: the Fernet key comes from Samus's own
:class:`KeyVault`; the seal path resolves through
``backend.common.state_paths``; the atomic write reuses the package's
``os.replace`` helper. No secret material is embedded.
"""

from __future__ import annotations

import base64
import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from ..state_paths import state_path
from .key_vault import get_key_vault


class SecretHandle:
    """A best-effort zeroizing handle around a single secret value.

    Hold a secret only as long as needed, then :meth:`clear` (or exit the
    ``with`` block) to overwrite the backing bytes. ``str``/``repr`` are
    redacted so the value never lands in a log line or traceback frame.
    """

    __slots__ = ("_buf", "_cleared", "_lock")

    def __init__(self, secret: bytes | bytearray | str) -> None:
        if isinstance(secret, str):
            secret = secret.encode("utf-8")
        if not isinstance(secret, (bytes, bytearray)):
            raise TypeError(f"secret must be bytes/bytearray/str, got {type(secret).__name__}")
        # Copy into a mutable buffer we own so we can overwrite it in place.
        self._buf: bytearray | None = bytearray(secret)
        self._cleared = False
        self._lock = threading.Lock()

    @property
    def cleared(self) -> bool:
        with self._lock:
            return self._cleared

    def bytes(self) -> bytes:
        """Return a *copy* of the secret bytes. Raises if already cleared.

        The copy is the caller's responsibility to scrub; prefer
        :meth:`with_bytes` for a scoped read.
        """
        with self._lock:
            if self._cleared or self._buf is None:
                raise ValueError("SecretHandle already cleared")
            return bytes(self._buf)

    def with_bytes(self, fn):
        """Call ``fn(secret_bytes)`` and return its result.

        A convenience for a scoped read that keeps the plaintext lifetime
        explicit. The bytes passed to ``fn`` are a copy; this does not
        prevent ``fn`` from retaining them.
        """
        return fn(self.bytes())

    def clear(self) -> None:
        """Overwrite the backing bytes with zeros and drop the buffer.

        Idempotent. After this, :meth:`bytes` raises.
        """
        with self._lock:
            if self._buf is not None:
                for i in range(len(self._buf)):
                    self._buf[i] = 0
                self._buf = None
            self._cleared = True

    def __enter__(self) -> "SecretHandle":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.clear()

    def __del__(self) -> None:  # pragma: no cover - GC timing
        try:
            self.clear()
        except Exception:  # noqa: BLE001 - never raise from __del__
            pass

    def __repr__(self) -> str:
        state = "cleared" if self._cleared else "live"
        return f"<SecretHandle {state}>"

    __str__ = __repr__


class MemoryEnclave:
    """An in-memory key/value store sealed with a KeyVault-derived key."""

    def __init__(self, seal_path: Path) -> None:
        self._seal_path = seal_path
        self._store: dict[str, Any] = {}
        self._lock = threading.Lock()
        self._loaded = False
        self._seal_path.parent.mkdir(parents=True, exist_ok=True)

    def _fernet(self) -> Fernet:
        key = get_key_vault().derive("enclave", length=32)
        return Fernet(base64.urlsafe_b64encode(key))

    def load(self) -> None:
        """Decrypt the on-disk seal into memory (idempotent — once only).

        A missing seal is a clean empty store. A corrupted seal, a
        wrong-keyed seal (``InvalidToken``) or malformed JSON also degrade to
        an empty store — the enclave never raises on load.
        """
        with self._lock:
            if self._loaded:
                return
            self._loaded = True
            if not self._seal_path.exists():
                return
            try:
                blob = self._seal_path.read_bytes()
                plaintext = self._fernet().decrypt(blob)
                self._store = json.loads(plaintext)
            except (InvalidToken, json.JSONDecodeError, OSError, ValueError):
                # Corrupted / wrong-keyed seal: start clean.
                self._store = {}

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._store.get(key, default)

    def put(self, key: str, value: Any) -> None:
        with self._lock:
            self._store[key] = value

    def delete(self, key: str) -> None:
        with self._lock:
            self._store.pop(key, None)

    def flush(self) -> None:
        """Serialise + Fernet-encrypt the store, atomically write the seal."""
        with self._lock:
            payload = json.dumps(self._store, default=str).encode("utf-8")
            blob = self._fernet().encrypt(payload)
        _atomic_write_bytes(self._seal_path, blob)


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """Atomic bytes write via temp-file + ``os.replace`` (package convention)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


_ENCLAVE: MemoryEnclave | None = None
_LOCK = threading.Lock()


def get_memory_enclave() -> MemoryEnclave:
    """Return the process-wide :class:`MemoryEnclave` singleton (loaded)."""
    global _ENCLAVE
    if _ENCLAVE is None:
        with _LOCK:
            if _ENCLAVE is None:
                enclave = MemoryEnclave(state_path("security", "enclave", "enclave.seal"))
                enclave.load()
                _ENCLAVE = enclave
    return _ENCLAVE


def reset_memory_enclave_for_tests() -> None:
    """Drop the cached singleton so the next :func:`get_memory_enclave`
    rebuilds from current settings. Test-fixture helper."""
    global _ENCLAVE
    with _LOCK:
        _ENCLAVE = None


__all__ = [
    "MemoryEnclave",
    "SecretHandle",
    "get_memory_enclave",
    "reset_memory_enclave_for_tests",
]
