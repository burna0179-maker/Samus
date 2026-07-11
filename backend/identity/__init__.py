"""Samus first-class signed identity + charter (ecosystem-core skeleton).

Samus historically had only a bare ``samus_agent_id`` string in
``backend/common/config.py`` — no first-class, operator-verifiable identity
artifact like the other agents carry (Major's ``data/identity/values.json``,
Darwin's ``backend/core/identity``). This package closes that gap:

* :mod:`backend.identity.charter` — loads + validates the immutable charter
  (``charter.json``: agent_id, values, invariants), frozen into a
  :class:`Charter` dataclass and sha256-fingerprinted at load.
* :mod:`backend.identity.charter_signature` — the operator-Ed25519 trust
  anchor over the charter bytes, REUSING the shared
  ``_shared.security.operator_signed_envelope`` primitive (no second crypto
  path). An on-box attacker who can rewrite ``charter.json`` cannot forge a
  matching operator signature without the off-box operator private key.

The charter ships WITH the code tree (immutable at runtime, part of the
signed-bundle identity boundary), mirroring how Major's identity ships under
``data/identity``. Verification is operator-verifiable: the same operator
root pubkey (``_shared/security/operator_root_pubkey.json``) that blesses the
peer-quorum roster and the autonomy attestation.
"""

from __future__ import annotations

from .charter import (
    Charter,
    CharterError,
    charter_hash,
    load_charter,
)

__all__ = [
    "Charter",
    "CharterError",
    "charter_hash",
    "load_charter",
]
