"""Locate the canonical ``_shared`` tree and make it importable as a package.

The other agents get the ecosystem root prepended to ``PYTHONPATH`` by the
shared launcher (``Launch-Agent.ps1`` / ECO-ADR-0001), so ``import
_shared.security.operator_signed_envelope`` and ``import _shared.autonomy``
resolve against the canonical ``_shared`` namespace package. Samus runs in a
container / its own venv where that env var may not be set, and the existing
Samus helpers (``broker_client``, ``reward_readout_route``) insert the
``_shared`` DIR itself onto ``sys.path`` to import the ``security_client``
SUBpackage — which does NOT make ``import _shared.…`` resolve.

This helper inserts the PARENT of ``_shared`` (the ecosystem root) onto
``sys.path`` so the fully-qualified ``_shared.*`` imports work, walking up
from this file to find a ``_shared`` directory that actually contains the
shared security primitive. Idempotent and best-effort: if no ``_shared`` is
found it returns ``False`` and the caller fails closed (the immutable gate /
charter-signature treats an unavailable primitive as "cannot establish
trust", never default-green).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

_ENSURED = False
_RESOLVED_ROOT: Optional[Path] = None

# A file that unambiguously identifies the canonical _shared tree (the SAME
# operator-Ed25519 primitive the reference agents reuse).
_SENTINEL = Path("security") / "operator_signed_envelope.py"


def ensure_shared_importable() -> bool:
    """Prepend the ecosystem root so ``import _shared.*`` resolves. Idempotent.

    Returns True if the canonical ``_shared`` tree is now importable, else
    False (caller fails closed).
    """
    global _ENSURED, _RESOLVED_ROOT
    if _ENSURED:
        return _RESOLVED_ROOT is not None

    here = Path(__file__).resolve()
    # Walk a generous range of ancestors: the Samus code root sits a few
    # levels up, and the ecosystem root (parent of _shared) is its parent.
    for up in range(1, 9):
        try:
            base = here.parents[up]
        except IndexError:
            break
        cand = base / "_shared"
        if (cand / _SENTINEL).exists():
            root = str(base)
            if root not in sys.path:
                sys.path.insert(0, root)
            _RESOLVED_ROOT = base
            _ENSURED = True
            return True

    _ENSURED = True
    _RESOLVED_ROOT = None
    return False


def shared_security_path() -> Optional[Path]:
    """Absolute path to ``_shared/security`` if resolvable, else None."""
    if ensure_shared_importable() and _RESOLVED_ROOT is not None:
        return _RESOLVED_ROOT / "_shared" / "security"
    return None


def operator_pubkey_path() -> Optional[Path]:
    """Absolute path to the operator root pubkey JSON if resolvable, else None."""
    sec = shared_security_path()
    if sec is None:
        return None
    return sec / "operator_root_pubkey.json"


__all__ = [
    "ensure_shared_importable",
    "shared_security_path",
    "operator_pubkey_path",
]
