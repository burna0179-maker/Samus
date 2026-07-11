#!/usr/bin/env python3
"""
Security Layer Restructure — dependency-minimal foundation
Source: ChatGPT recovery chat 24

Canonical relationship:
- F:\\Samus iteration (memory: project_samus_plane_iteration)
- [EXPANDS §6 security_extended] cold/minimal-dep crypto primitives
- [FIX] decouple from backend.core.__init__ heavy bootstrap chain
- [NEW] backend/security/ replaces backend/core/security/

Module layout (all stdlib + pynacl only — NO container/logging/pydantic):
  backend/security/
    __init__.py           — re-exports public API
    errors.py             — SecurityError + subtypes
    canonical_json.py     — deterministic JSON encoding
    key_registry.py       — in-memory signer→pubkey map
    nacl_signing.py       — Ed25519 keygen/sign/verify
    envelope.py           — signed envelope create/verify

Architectural rule:
  Low-level crypto MUST be safe to import anywhere without triggering
  bootstrap dependencies. No app config, no logging init, no DI container.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional


# ----- errors.py -----
class SecurityError(Exception):
    pass


class SignatureVerificationError(SecurityError):
    pass


class UnknownSignerError(SecurityError):
    pass


class EnvelopeFormatError(SecurityError):
    pass


# ----- canonical_json.py -----
def canonical_json_bytes(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


# ----- key_registry.py -----
@dataclass
class SigningKeyRegistry:
    _keys: Dict[str, str] = field(default_factory=dict)

    def register(self, signer_id: str, public_key_hex: str) -> None:
        self._keys[signer_id] = public_key_hex.lower()

    def get(self, signer_id: str) -> Optional[str]:
        return self._keys.get(signer_id)

    def has(self, signer_id: str) -> bool:
        return signer_id in self._keys


_registry = SigningKeyRegistry()


def get_signing_key_registry() -> SigningKeyRegistry:
    return _registry


# ----- nacl_signing.py -----
def generate_keypair():
    """Returns (private_key_bytes, public_key_bytes). Uses pynacl Ed25519."""
    from nacl.signing import SigningKey   # lazy import — keeps module cold
    sk = SigningKey.generate()
    return bytes(sk), bytes(sk.verify_key)


def sign_bytes(message: bytes, private_key: bytes) -> bytes:
    from nacl.signing import SigningKey
    return SigningKey(private_key).sign(message).signature


def verify_bytes(message: bytes, signature: bytes, public_key: bytes) -> bool:
    from nacl.signing import VerifyKey
    from nacl.exceptions import BadSignatureError
    try:
        VerifyKey(public_key).verify(message, signature)
        return True
    except BadSignatureError:
        return False


# ----- envelope.py -----
def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _unsigned_envelope_payload(envelope: Dict[str, Any]) -> bytes:
    unsigned = {k: envelope[k] for k in ("sender", "action", "payload", "ts")}
    return canonical_json_bytes(unsigned)


def create_envelope(*, sender: str, action: str, payload: Dict[str, Any],
                    private_key_hex: str, ts: Optional[str] = None) -> Dict[str, Any]:
    envelope = {"sender": sender, "action": action, "payload": payload, "ts": ts or _utc_now_iso()}
    sig = sign_bytes(_unsigned_envelope_payload(envelope), bytes.fromhex(private_key_hex))
    envelope["signature"] = sig.hex()
    return envelope


def verify_envelope(envelope: Dict[str, Any], *, registry: Optional[SigningKeyRegistry] = None) -> bool:
    required = {"sender", "action", "payload", "ts", "signature"}
    missing = required - envelope.keys()
    if missing:
        raise EnvelopeFormatError(f"Missing envelope fields: {sorted(missing)}")
    registry = registry or get_signing_key_registry()
    pub = registry.get(envelope["sender"])
    if not pub:
        raise UnknownSignerError(f"Unknown signer: {envelope['sender']}")
    ok = verify_bytes(
        message=_unsigned_envelope_payload(envelope),
        signature=bytes.fromhex(envelope["signature"]),
        public_key=bytes.fromhex(pub),
    )
    if not ok:
        raise SignatureVerificationError(f"Invalid signature for: {envelope['sender']}")
    return True


# Anti-pattern note: ed25519==1.5 pip package fails on Python 3.13+/3.14
# (uses removed configparser.SafeConfigParser). Use `pynacl` instead.
# Both provide Ed25519; pynacl is the canonical choice for Samus.
