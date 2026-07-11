"""Windows DPAPI seal/unseal — OS-level at-rest sealing (CORE — E5 D15).

This is the OS sealing layer that sits *under* the Fernet
:class:`~backend.common.keys.memory_enclave.MemoryEnclave`: the enclave
seals secrets with an application key derived by the
:class:`~backend.common.keys.key_vault.KeyVault`; this module can seal that
material a SECOND time with the Windows Data Protection API (DPAPI) via
``win32crypt.CryptProtectData`` / ``CryptUnprotectData`` so a secret blob on
disk is protected by an OS-managed master key as well as by directory ACLs.

It is the in-process counterpart of the operator-side PowerShell credential
store (``scripts/Samus.Secrets.psm1``), which already DPAPI-protects the
``.cred`` files under ``%LOCALAPPDATA%\\Samus\\credentials``. This module does
NOT read or duplicate that store — secrets continue to flow into the process
as ``SAMUS_*`` env vars bound onto ``Settings``. It provides the same DPAPI
primitive to Python callers that need to seal derived/transient material at
rest without leaving a separate copy of the operator's credentials.

Scope matters for autonomy
--------------------------
An agent boots under a Windows service / scheduled task. A ``LogonType=S4U``
task fires WITHOUT a password and therefore has no CurrentUser DPAPI master
key — CurrentUser-scope blobs sealed by an interactive operator session
cannot be decrypted under S4U.

So this module defaults to **LocalMachine** scope
(``CRYPTPROTECT_LOCAL_MACHINE``): any process on the host can unseal, which
is acceptable here because the host itself is the trust boundary and the file
ACL still gates read access. CurrentUser scope remains selectable for callers
that genuinely run interactive (matching the per-user .cred posture of
``Samus.Secrets.psm1``).

Off-Windows degradation
-----------------------
``win32crypt`` is Windows-only (Samus's workcells also run in Linux
containers). Every public function degrades cleanly:

  * :func:`dpapi_available` returns ``False`` (never raises).
  * :func:`seal` / :func:`unseal` raise :class:`DpapiUnavailable` — a clear,
    catchable exception, never a bare ``ImportError``.

Callers are expected to fall back to the unsealed (Fernet-only) enclave path
in that case; the enclave already does exactly this.

This module is self-contained — stdlib + the optional ``win32crypt`` import
only. It performs no filesystem writes and spawns no subprocesses.
"""

from __future__ import annotations

import sys
from typing import Final

__all__ = [
    "DpapiError",
    "DpapiUnavailable",
    "SCOPE_CURRENT_USER",
    "SCOPE_LOCAL_MACHINE",
    "dpapi_available",
    "seal",
    "unseal",
]

# ---- scope identifiers -----------------------------------------------------

SCOPE_LOCAL_MACHINE: Final[str] = "local_machine"
SCOPE_CURRENT_USER: Final[str] = "current_user"

# ``win32crypt.CryptProtectData`` flag — when set the blob is sealed to the
# machine master key rather than the calling user's. Required for S4U
# scheduled-task decryptability. Value is fixed by the Win32 API.
_CRYPTPROTECT_LOCAL_MACHINE: Final[int] = 0x4


# ---- exceptions ------------------------------------------------------------


class DpapiError(RuntimeError):
    """Base class for DPAPI vault failures."""


class DpapiUnavailable(DpapiError):
    """Raised when a seal/unseal is attempted but DPAPI is not usable.

    This is the off-Windows / ``win32crypt``-missing degradation signal. It
    is deliberately a distinct, catchable type so callers can branch to the
    unsealed fallback without swallowing a bare ``ImportError`` (which would
    also mask genuine bugs).
    """


# ---- win32crypt import (guarded) ------------------------------------------
#
# Imported once at module load inside try/except so importing this module
# never crashes off-Windows. ``_win32crypt`` is ``None`` when unavailable;
# ``dpapi_available()`` is the single source of truth.

try:  # pragma: no cover - branch taken depends on platform
    import win32crypt as _win32crypt  # type: ignore[import-not-found]
except Exception:  # noqa: BLE001 - any import failure means "unavailable"
    _win32crypt = None  # type: ignore[assignment]


def dpapi_available() -> bool:
    """Return ``True`` only on Windows with ``win32crypt`` importable.

    Never raises. This is the canonical guard callers should consult before
    attempting :func:`seal` / :func:`unseal`.
    """
    return sys.platform == "win32" and _win32crypt is not None


def _scope_flag(scope: str) -> int:
    """Map a scope identifier to the CryptProtectData flag bitmask."""
    normalised = (scope or "").strip().lower()
    if normalised in ("", SCOPE_LOCAL_MACHINE):
        return _CRYPTPROTECT_LOCAL_MACHINE
    if normalised == SCOPE_CURRENT_USER:
        return 0
    raise DpapiError(
        f"unknown DPAPI scope {scope!r} — expected "
        f"{SCOPE_LOCAL_MACHINE!r} or {SCOPE_CURRENT_USER!r}"
    )


def seal(
    plaintext: bytes,
    *,
    scope: str = SCOPE_LOCAL_MACHINE,
    entropy: bytes = b"",
) -> bytes:
    """Seal ``plaintext`` with DPAPI and return the opaque blob.

    Parameters
    ----------
    plaintext:
        Raw bytes to protect (e.g. a Fernet-enclave-sealed secret to be
        double-sealed at the OS layer).
    scope:
        ``"local_machine"`` (default — S4U-decryptable) or ``"current_user"``.
    entropy:
        Optional secondary entropy. The exact same bytes MUST be supplied to
        :func:`unseal` or decryption fails. ``b""`` means no extra entropy.

    Raises
    ------
    DpapiUnavailable:
        DPAPI is not usable on this platform.
    DpapiError:
        ``scope`` is invalid, or the Win32 call itself failed.
    TypeError:
        ``plaintext`` is not ``bytes``.
    """
    if not isinstance(plaintext, (bytes, bytearray)):
        raise TypeError(f"plaintext must be bytes, got {type(plaintext).__name__}")
    if not dpapi_available():
        raise DpapiUnavailable(
            f"DPAPI sealing unavailable: win32crypt not importable (platform={sys.platform!r})"
        )
    flag = _scope_flag(scope)
    try:
        # CryptProtectData(data, description, optional_entropy,
        #                  reserved, prompt_struct, flags)
        blob = _win32crypt.CryptProtectData(
            bytes(plaintext),
            None,
            bytes(entropy),
            None,
            None,
            flag,
        )
    except Exception as exc:  # noqa: BLE001 - normalise to DpapiError
        raise DpapiError(f"CryptProtectData failed: {exc!r}") from exc
    return bytes(blob)


def unseal(blob: bytes, *, entropy: bytes = b"") -> bytes:
    """Unseal a DPAPI ``blob`` produced by :func:`seal`.

    Parameters
    ----------
    blob:
        The opaque bytes returned by :func:`seal`.
    entropy:
        Must be byte-identical to the ``entropy`` passed to :func:`seal`. A
        mismatch causes the Win32 call to fail and is surfaced as
        :class:`DpapiError`.

    Notes
    -----
    Scope is encoded in the blob itself, so it is not a parameter here — a
    LocalMachine-sealed blob unseals on any process on the host; a
    CurrentUser-sealed blob unseals only for the sealing user (and so is NOT
    S4U-decryptable).

    Raises
    ------
    DpapiUnavailable:
        DPAPI is not usable on this platform.
    DpapiError:
        The Win32 call failed (corrupt blob, wrong entropy, or a CurrentUser
        blob being opened by a different principal).
    TypeError:
        ``blob`` is not ``bytes``.
    """
    if not isinstance(blob, (bytes, bytearray)):
        raise TypeError(f"blob must be bytes, got {type(blob).__name__}")
    if not dpapi_available():
        raise DpapiUnavailable(
            f"DPAPI unsealing unavailable: win32crypt not importable (platform={sys.platform!r})"
        )
    try:
        # CryptUnprotectData(data, optional_entropy, reserved,
        #                    prompt_struct, flags) -> (description, plaintext)
        _description, plaintext = _win32crypt.CryptUnprotectData(
            bytes(blob),
            bytes(entropy),
            None,
            None,
            0,
        )
    except Exception as exc:  # noqa: BLE001 - normalise to DpapiError
        raise DpapiError(
            f"CryptUnprotectData failed (corrupt blob or entropy mismatch): {exc!r}"
        ) from exc
    return bytes(plaintext)
