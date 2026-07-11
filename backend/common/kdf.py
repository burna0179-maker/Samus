"""HKDF-SHA256 key derivation (RFC 5869) — stdlib only.

Port of Anita's :mod:`backend.core.security.kdf` (round-7 final polish,
2026-05-17). Lives in ``backend/common`` here because Samus's shell uses
``common/`` for shared primitives.

Used by :mod:`backend.common.audit_ledger` to derive per-epoch HMAC keys
from a single long-lived secret so rotating session / replay / CSRF keys
does NOT invalidate the audit chain.

Salt namespacing WHY: different ledgers / signers in the Hustleforge
ecosystem MUST get distinct derivation domains. The constant
``SAMUS_LEDGER_SALT_V1`` below is the Samus audit ledger's namespace; new
consumers should declare their own ``*_SALT_V1`` constants here. Note the
Samus salt is namespaced separately from Anita's so a leak in one cannot
forge the other.

Tested against RFC 5869 Test Case 1.
"""

from __future__ import annotations

import hashlib
import hmac

__all__ = ["hkdf_sha256", "SAMUS_LEDGER_SALT_V1"]

# Samus EvidenceLedger derivation namespace. Bump the suffix only if the
# derivation scheme itself changes shape (e.g. switching the underlying
# hash) — NOT for secret rotation, which is handled by changing the IKM.
SAMUS_LEDGER_SALT_V1: bytes = b"samus-ledger-v1"

# SHA-256 output size in bytes; HKDF spec calls this ``HashLen``.
_HASH_LEN = 32

# RFC 5869 §2.3: ``L`` (output length) must not exceed 255 * HashLen.
_MAX_OUTPUT_LEN = 255 * _HASH_LEN


def hkdf_sha256(
    ikm: bytes,
    salt: bytes,
    info: bytes,
    length: int = _HASH_LEN,
) -> bytes:
    """Derive ``length`` bytes via HKDF-SHA256 (RFC 5869).

    Parameters mirror the RFC:
      * ``ikm`` — input keying material (the long-lived secret).
      * ``salt`` — non-secret, ideally per-application constant. Empty
        salt is permitted (the RFC defaults it to ``HashLen`` zero bytes)
        but discouraged.
      * ``info`` — context/application-specific info that binds the
        derived key to its purpose (e.g. epoch number, ledger id).
      * ``length`` — desired output bytes. Default 32 (one HMAC-SHA256
        key).

    Raises ``ValueError`` if ``length`` exceeds ``255 * HashLen`` (1020
    bytes) — that's the hard ceiling of HKDF-SHA256.
    """
    if length <= 0:
        raise ValueError(f"hkdf_sha256: length must be > 0; got {length}")
    if length > _MAX_OUTPUT_LEN:
        raise ValueError(
            f"hkdf_sha256: length {length} exceeds max {_MAX_OUTPUT_LEN} "
            f"(255 * HashLen for SHA-256)"
        )
    if not isinstance(ikm, (bytes, bytearray)):
        raise TypeError("hkdf_sha256: ikm must be bytes")
    if not isinstance(salt, (bytes, bytearray)):
        raise TypeError("hkdf_sha256: salt must be bytes")
    if not isinstance(info, (bytes, bytearray)):
        raise TypeError("hkdf_sha256: info must be bytes")

    # Extract: PRK = HMAC(salt, IKM). Per RFC §2.2, an empty salt is
    # replaced by HashLen zero bytes so HMAC has a defined key.
    if len(salt) == 0:
        salt = b"\x00" * _HASH_LEN
    prk = hmac.new(bytes(salt), bytes(ikm), hashlib.sha256).digest()

    # Expand: T(0) = empty; T(i) = HMAC(PRK, T(i-1) || info || byte(i))
    n = (length + _HASH_LEN - 1) // _HASH_LEN
    t = b""
    okm = bytearray()
    for i in range(1, n + 1):
        t = hmac.new(prk, t + bytes(info) + bytes([i]), hashlib.sha256).digest()
        okm.extend(t)

    return bytes(okm[:length])
