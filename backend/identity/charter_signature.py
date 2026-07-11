"""Operator-Ed25519 trust anchor for Samus's ``charter.json``.

The charter (``backend/identity/charter.json``) is, by itself, an UNSIGNED
JSON file. An on-box attacker who can rewrite it can change Samus's stated
values/invariants and the loader would happily accept the new bytes. This
module is the missing signature layer: it establishes WHETHER the
operator vouches for the on-disk charter, so the boot path can refuse to
treat an unsigned (or tampered) charter as production-trusted.

REUSE, NOT A SECOND CRYPTO PATH: the envelope shape, canonical-JSON encoding,
and verify routine all come from
:mod:`_shared.security.operator_signed_envelope` — the SAME operator root
Ed25519 key (``_shared/security/operator_root_pubkey.json``) that signs the
peer-quorum roster, the autonomy attestation, and the other agents' immutable
baselines. The private half is operator-only (off-box, DPAPI scope
``MehOperatorBreakGlass``); an on-box attacker CANNOT forge a signature over
the charter without it.

SIGNED BODY — bound to the exact on-disk bytes::

    payload = {
        "purpose": "samus_charter",
        "charter_sha256": "<sha256 hex of the EXACT charter.json bytes>",
    }

The operator signs over ``canonical_bytes({"payload": payload,
"signed_at_ts": <ts>, "key_id": "operator_root"})`` (the shared envelope's
own signing bytes). Signing the sha256 keeps the envelope small while binding
bit-for-bit to the on-disk file.

SIDECAR FILE: ``charter.json.ed25519.sig`` — a sibling of the charter, so the
operator signature travels with the deployment identity it blesses.

Verdict posture mirrors Major's ``immutable_baseline_signature``:
operator-Ed25519-first → UNSIGNED, failing CLOSED (``tamper=True``) on a
present-but-broken signature. There is no forgeable-HMAC fallback here — the
charter is a tiny, code-tree artifact, so the only meaningful anchor is the
operator root key.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional

from .shared_bootstrap import ensure_shared_importable, operator_pubkey_path

log = logging.getLogger("samus.identity.charter_signature")

ED25519_SIG_SUFFIX = ".ed25519.sig"
CHARTER_PAYLOAD_PURPOSE = "samus_charter"


class CharterSignaturePosture(str, Enum):
    """Which trust anchor signed the verified charter."""

    OPERATOR_ED25519 = "operator_ed25519"
    NONE = "none"

    @property
    def production_ready(self) -> bool:
        return self is CharterSignaturePosture.OPERATOR_ED25519


@dataclass(frozen=True)
class CharterSignatureResult:
    posture: CharterSignaturePosture
    tamper: bool
    signed: bool
    detail: str
    key_id: str = ""
    signed_at_ts: float = 0.0

    @property
    def production_ready(self) -> bool:
        return self.posture.production_ready

    def to_dict(self) -> dict[str, object]:
        return {
            "signature_posture": self.posture.value,
            "production_ready": self.production_ready,
            "tamper": self.tamper,
            "signed": self.signed,
            "detail": self.detail,
            "key_id": self.key_id,
            "signed_at_ts": self.signed_at_ts,
        }


def ed25519_sig_path_for(charter_path: Path) -> Path:
    """The sibling Ed25519 signature path for a given charter path."""
    return charter_path.with_suffix(charter_path.suffix + ED25519_SIG_SUFFIX)


def charter_sha256_hex(charter_bytes: bytes) -> str:
    """sha256 hex of the EXACT charter bytes (no re-canonicalization)."""
    return hashlib.sha256(charter_bytes).hexdigest()


def build_signing_payload(charter_bytes: bytes) -> dict:
    """The inner signed body bound to the exact on-disk charter bytes."""
    return {
        "purpose": CHARTER_PAYLOAD_PURPOSE,
        "charter_sha256": charter_sha256_hex(charter_bytes),
    }


# Cap the charter size before reading it for verify — a hash-bound JSON is
# well under 1 MB; an attacker with file-write could otherwise swap a giant
# blob to OOM boot before any check fires.
_CHARTER_MAX_BYTES = 1 * 1024 * 1024


def check_charter_signature(
    charter_path: Path,
    *,
    operator_pubkey_path_override: Optional[Path] = None,
) -> CharterSignatureResult:
    """Establish the trust anchor for ``charter_path`` (Ed25519-or-unsigned).

    Fails CLOSED (``tamper=True``) on a present-but-broken signature, on a
    sha256 binding mismatch, or when the operator pubkey itself is
    unavailable (we cannot establish trust — never default-green, I-14).
    """
    charter_path = Path(charter_path)
    if not charter_path.exists():
        return CharterSignatureResult(
            posture=CharterSignaturePosture.NONE,
            tamper=False,
            signed=False,
            detail=f"charter {charter_path} absent (no signature to check)",
        )

    try:
        if charter_path.stat().st_size > _CHARTER_MAX_BYTES:
            return CharterSignatureResult(
                posture=CharterSignaturePosture.NONE,
                tamper=True,
                signed=True,
                detail=(
                    f"charter exceeds {_CHARTER_MAX_BYTES} bytes — refusing to "
                    "load for signature verify (possible tamper)"
                ),
            )
        charter_bytes = charter_path.read_bytes()
    except OSError as exc:
        return CharterSignatureResult(
            posture=CharterSignaturePosture.NONE,
            tamper=True,
            signed=False,
            detail=f"charter read failed: {exc!r}",
        )

    sig_path = ed25519_sig_path_for(charter_path)
    if not sig_path.exists():
        return CharterSignatureResult(
            posture=CharterSignaturePosture.NONE,
            tamper=False,
            signed=False,
            detail=(
                f"charter {charter_path.name} carries no operator-Ed25519 "
                f"signature ({sig_path.name}). Trust anchor = NONE "
                "(production_ready=False). Run "
                "scripts/sign_charter.py --ed25519 --yes."
            ),
        )

    # The signature exists — we must be able to import the shared primitive +
    # resolve the operator pubkey, or we fail closed (cannot establish trust).
    if not ensure_shared_importable():
        return CharterSignatureResult(
            posture=CharterSignaturePosture.NONE,
            tamper=True,
            signed=True,
            detail=(
                "operator-signed-envelope primitive (_shared/security) not "
                "importable — cannot verify charter signature; failing closed."
            ),
        )

    pubkey_path = (
        Path(operator_pubkey_path_override)
        if operator_pubkey_path_override is not None
        else operator_pubkey_path()
    )
    if pubkey_path is None:
        return CharterSignatureResult(
            posture=CharterSignaturePosture.NONE,
            tamper=True,
            signed=True,
            detail="operator root pubkey path unresolved — failing closed.",
        )

    try:
        from _shared.security.operator_signed_envelope import (  # noqa: PLC0415
            OperatorPubkeyLoader,
            OperatorPubkeyUnavailable,
            OperatorSignatureInvalid,
            OperatorSignedEnvelope,
            verify_envelope,
        )
    except Exception as exc:  # noqa: BLE001 — import failure = cannot establish trust
        return CharterSignatureResult(
            posture=CharterSignaturePosture.NONE,
            tamper=True,
            signed=True,
            detail=f"shared crypto import failed: {exc!r} — failing closed.",
        )

    try:
        raw = json.loads(sig_path.read_text(encoding="utf-8"))
        envelope = OperatorSignedEnvelope.from_wire(raw)
    except (OSError, json.JSONDecodeError, OperatorSignatureInvalid) as exc:
        return CharterSignatureResult(
            posture=CharterSignaturePosture.NONE,
            tamper=True,
            signed=True,
            detail=f"charter sig sidecar malformed: {exc!r}",
        )

    expected = build_signing_payload(charter_bytes)
    got_sha = str(envelope.payload.get("charter_sha256", ""))
    got_purpose = str(envelope.payload.get("purpose", ""))
    if got_purpose != CHARTER_PAYLOAD_PURPOSE:
        return CharterSignatureResult(
            posture=CharterSignaturePosture.NONE,
            tamper=True,
            signed=True,
            detail=(
                f"charter sig purpose mismatch: got {got_purpose!r}, "
                f"want {CHARTER_PAYLOAD_PURPOSE!r}"
            ),
        )
    if got_sha != expected["charter_sha256"]:
        return CharterSignatureResult(
            posture=CharterSignaturePosture.NONE,
            tamper=True,
            signed=True,
            detail=(
                "charter sig charter_sha256 does not match on-disk charter "
                "bytes — the charter changed without a corresponding operator "
                "re-sign (or the sig is for a different charter)."
            ),
        )

    loader = OperatorPubkeyLoader(path=pubkey_path)
    try:
        verify_envelope(envelope, pubkey_loader=loader)
    except OperatorPubkeyUnavailable as exc:
        log.error("samus charter ed25519: pubkey unavailable: %s", exc)
        return CharterSignatureResult(
            posture=CharterSignaturePosture.NONE,
            tamper=True,
            signed=True,
            detail=f"operator pubkey unavailable: {exc}",
        )
    except OperatorSignatureInvalid as exc:
        log.error("samus charter ed25519: INVALID/tamper: %s", exc)
        return CharterSignatureResult(
            posture=CharterSignaturePosture.NONE,
            tamper=True,
            signed=True,
            detail=f"operator ed25519 signature invalid: {exc}",
        )

    log.info(
        "samus charter: operator-Ed25519 signature VERIFIED "
        "(production trust anchor; key_id=%s signed_at=%s)",
        envelope.key_id,
        envelope.signed_at_ts,
    )
    return CharterSignatureResult(
        posture=CharterSignaturePosture.OPERATOR_ED25519,
        tamper=False,
        signed=True,
        detail=(
            f"operator-Ed25519 charter signature verified (key_id="
            f"{envelope.key_id}, signed_at={envelope.signed_at_ts})"
        ),
        key_id=str(envelope.key_id),
        signed_at_ts=float(envelope.signed_at_ts),
    )


__all__ = [
    "CharterSignaturePosture",
    "CharterSignatureResult",
    "ED25519_SIG_SUFFIX",
    "CHARTER_PAYLOAD_PURPOSE",
    "ed25519_sig_path_for",
    "charter_sha256_hex",
    "build_signing_payload",
    "check_charter_signature",
]
