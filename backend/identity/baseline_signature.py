"""Operator-Ed25519 trust anchor for Samus's ``immutable_baseline.json``.

Samus's :func:`backend.identity.immutable_manifest.verify_manifest` answers
"do the sealed source bytes still match the recorded baseline?" — but NOT "is
the baseline itself trustworthy?". This module is the missing signature layer:
it establishes which trust anchor, if any, vouches for the recorded baseline,
so the boot gate can refuse to treat an unsigned (or tampered) baseline as
production.

REUSE, NOT A SECOND CRYPTO PATH — identical to the charter signature: the
envelope shape, canonical-JSON encoding, and verify routine come from
:mod:`_shared.security.operator_signed_envelope` (the SAME operator root
Ed25519 key as the peer-quorum roster + the other agents' baselines). An
on-box attacker who can rewrite ``immutable_baseline.json`` CANNOT forge a
matching operator signature without the off-box private key.

SIGNED BODY::

    payload = {
        "purpose": "samus_immutable_baseline",
        "baseline_sha256": "<sha256 hex of the EXACT baseline bytes on disk>",
    }

SIDECAR: ``immutable_baseline.json.ed25519.sig``.

Verdict posture: operator-Ed25519-first → UNSIGNED, failing CLOSED
(``tamper=True``) on a present-but-broken signature, a sha256 binding
mismatch, or an unavailable operator pubkey (cannot establish trust — never
default-green, I-14). There is intentionally NO forgeable-HMAC fallback (the
Samus build is greenfield on this gate, so we ship straight to the production
anchor — the other agents kept HMAC only for migration back-compat).
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

log = logging.getLogger("samus.identity.baseline_signature")

ED25519_SIG_SUFFIX = ".ed25519.sig"
BASELINE_PAYLOAD_PURPOSE = "samus_immutable_baseline"

_BASELINE_MAX_BYTES = 8 * 1024 * 1024


class SignaturePosture(str, Enum):
    OPERATOR_ED25519 = "operator_ed25519"
    NONE = "none"

    @property
    def production_ready(self) -> bool:
        return self is SignaturePosture.OPERATOR_ED25519


@dataclass(frozen=True)
class BaselineSignatureResult:
    posture: SignaturePosture
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


def ed25519_sig_path_for(baseline_path: Path) -> Path:
    return baseline_path.with_suffix(baseline_path.suffix + ED25519_SIG_SUFFIX)


def baseline_sha256_hex(baseline_bytes: bytes) -> str:
    return hashlib.sha256(baseline_bytes).hexdigest()


def build_signing_payload(baseline_bytes: bytes) -> dict:
    return {
        "purpose": BASELINE_PAYLOAD_PURPOSE,
        "baseline_sha256": baseline_sha256_hex(baseline_bytes),
    }


def check_baseline_signature(
    baseline_path: Path,
    *,
    operator_pubkey_path_override: Optional[Path] = None,
) -> BaselineSignatureResult:
    """Establish the trust anchor for ``baseline_path`` (Ed25519-or-unsigned)."""
    baseline_path = Path(baseline_path)
    if not baseline_path.exists():
        return BaselineSignatureResult(
            posture=SignaturePosture.NONE,
            tamper=False,
            signed=False,
            detail=f"baseline {baseline_path} absent (no signature to check)",
        )

    try:
        if baseline_path.stat().st_size > _BASELINE_MAX_BYTES:
            return BaselineSignatureResult(
                posture=SignaturePosture.NONE,
                tamper=True,
                signed=True,
                detail=(
                    f"baseline exceeds {_BASELINE_MAX_BYTES} bytes — refusing "
                    "to load for signature verify (possible tamper)"
                ),
            )
        baseline_bytes = baseline_path.read_bytes()
    except OSError as exc:
        return BaselineSignatureResult(
            posture=SignaturePosture.NONE,
            tamper=True,
            signed=False,
            detail=f"baseline read failed: {exc!r}",
        )

    sig_path = ed25519_sig_path_for(baseline_path)
    if not sig_path.exists():
        return BaselineSignatureResult(
            posture=SignaturePosture.NONE,
            tamper=False,
            signed=False,
            detail=(
                f"baseline {baseline_path.name} carries no operator-Ed25519 "
                f"signature ({sig_path.name}). Trust anchor = NONE "
                "(production_ready=False). Run "
                "scripts/sign_immutable_manifest.py --ed25519 --yes."
            ),
        )

    if not ensure_shared_importable():
        return BaselineSignatureResult(
            posture=SignaturePosture.NONE,
            tamper=True,
            signed=True,
            detail=(
                "operator-signed-envelope primitive (_shared/security) not "
                "importable — cannot verify baseline signature; failing closed."
            ),
        )

    pubkey_path = (
        Path(operator_pubkey_path_override)
        if operator_pubkey_path_override is not None
        else operator_pubkey_path()
    )
    if pubkey_path is None:
        return BaselineSignatureResult(
            posture=SignaturePosture.NONE,
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
    except Exception as exc:  # noqa: BLE001
        return BaselineSignatureResult(
            posture=SignaturePosture.NONE,
            tamper=True,
            signed=True,
            detail=f"shared crypto import failed: {exc!r} — failing closed.",
        )

    try:
        raw = json.loads(sig_path.read_text(encoding="utf-8"))
        envelope = OperatorSignedEnvelope.from_wire(raw)
    except (OSError, json.JSONDecodeError, OperatorSignatureInvalid) as exc:
        return BaselineSignatureResult(
            posture=SignaturePosture.NONE,
            tamper=True,
            signed=True,
            detail=f"baseline sig sidecar malformed: {exc!r}",
        )

    expected = build_signing_payload(baseline_bytes)
    got_sha = str(envelope.payload.get("baseline_sha256", ""))
    got_purpose = str(envelope.payload.get("purpose", ""))
    if got_purpose != BASELINE_PAYLOAD_PURPOSE:
        return BaselineSignatureResult(
            posture=SignaturePosture.NONE,
            tamper=True,
            signed=True,
            detail=(
                f"baseline sig purpose mismatch: got {got_purpose!r}, "
                f"want {BASELINE_PAYLOAD_PURPOSE!r}"
            ),
        )
    if got_sha != expected["baseline_sha256"]:
        return BaselineSignatureResult(
            posture=SignaturePosture.NONE,
            tamper=True,
            signed=True,
            detail=(
                "baseline sig baseline_sha256 does not match on-disk baseline "
                "bytes — the baseline changed without a corresponding operator "
                "re-sign (or the sig is for a different baseline)."
            ),
        )

    loader = OperatorPubkeyLoader(path=pubkey_path)
    try:
        verify_envelope(envelope, pubkey_loader=loader)
    except OperatorPubkeyUnavailable as exc:
        log.error("samus immutable_baseline ed25519: pubkey unavailable: %s", exc)
        return BaselineSignatureResult(
            posture=SignaturePosture.NONE,
            tamper=True,
            signed=True,
            detail=f"operator pubkey unavailable: {exc}",
        )
    except OperatorSignatureInvalid as exc:
        log.error("samus immutable_baseline ed25519: INVALID/tamper: %s", exc)
        return BaselineSignatureResult(
            posture=SignaturePosture.NONE,
            tamper=True,
            signed=True,
            detail=f"operator ed25519 signature invalid: {exc}",
        )

    log.info(
        "samus immutable_baseline: operator-Ed25519 signature VERIFIED "
        "(production trust anchor; key_id=%s signed_at=%s)",
        envelope.key_id,
        envelope.signed_at_ts,
    )
    return BaselineSignatureResult(
        posture=SignaturePosture.OPERATOR_ED25519,
        tamper=False,
        signed=True,
        detail=(
            f"operator-Ed25519 baseline signature verified (key_id="
            f"{envelope.key_id}, signed_at={envelope.signed_at_ts})"
        ),
        key_id=str(envelope.key_id),
        signed_at_ts=float(envelope.signed_at_ts),
    )


__all__ = [
    "SignaturePosture",
    "BaselineSignatureResult",
    "ED25519_SIG_SUFFIX",
    "BASELINE_PAYLOAD_PURPOSE",
    "ed25519_sig_path_for",
    "baseline_sha256_hex",
    "build_signing_payload",
    "check_baseline_signature",
]
