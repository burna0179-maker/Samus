"""Tier-1 immutable charter loaded once at boot (ecosystem-core parity).

``backend/identity/charter.json`` is Samus's persona/charter file — the
first-class identity artifact the other agents carry. It is read once,
frozen into a :class:`Charter` dataclass, sha256-fingerprinted, and never
mutated at runtime. Replacement requires a fresh charter file + a fresh boot
(and, for production trust, a fresh operator signature — see
:mod:`backend.identity.charter_signature`); there is no in-process write path
on this module by design.

On-disk schema (fixed):

* ``agent_id``    — globally-unique slug (``"samus"``)
* ``agent_name``  — human-readable name
* ``lineage``     — origin marker
* ``version``     — charter-instance version
* ``domain``      — the agent's bounded domain (``"revenue / fulfillment"``)
* ``values``      — list[str] of operating principles
* ``invariants``  — list[str] of runtime-immutable constraints

Any missing field, wrong type, or empty required value raises
:class:`CharterError` so the caller can fail deterministically rather than
running with a half-loaded charter. This mirrors Major's
``src/core/identity.py`` (adapted to Samus's container code layout — the
charter lives under ``backend/`` which ships in the read-only image).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping

# backend/identity/charter.py -> parents[0] == backend/identity
_CHARTER_PATH = Path(__file__).resolve().parent / "charter.json"

_REQUIRED_STR_FIELDS = ("agent_id", "agent_name", "lineage", "version", "domain")
_REQUIRED_LIST_FIELDS = ("values", "invariants")


class CharterError(Exception):
    """Raised when the charter file is missing, malformed, or fails validation."""


@dataclass(frozen=True)
class Charter:
    agent_id: str
    agent_name: str
    lineage: str
    version: str
    domain: str
    values: tuple[str, ...] = field(default_factory=tuple)
    invariants: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "agent_name": self.agent_name,
            "lineage": self.lineage,
            "version": self.version,
            "domain": self.domain,
            "values": list(self.values),
            "invariants": list(self.invariants),
        }


def charter_path() -> Path:
    """The on-disk charter path (ships with the code tree, read-only at runtime)."""
    return _CHARTER_PATH


def _coerce_str(raw: Mapping[str, Any], key: str) -> str:
    if key not in raw:
        raise CharterError(f"charter field {key!r} is missing")
    val = raw[key]
    if not isinstance(val, str):
        raise CharterError(f"charter field {key!r} must be a string, got {type(val).__name__}")
    val = val.strip()
    if not val:
        raise CharterError(f"charter field {key!r} must be non-empty")
    return val


def _coerce_str_list(raw: Mapping[str, Any], key: str) -> tuple[str, ...]:
    if key not in raw:
        raise CharterError(f"charter field {key!r} is missing")
    val = raw[key]
    if not isinstance(val, list):
        raise CharterError(f"charter field {key!r} must be a list, got {type(val).__name__}")
    out: list[str] = []
    for idx, item in enumerate(val):
        if not isinstance(item, str):
            raise CharterError(
                f"charter field {key!r}[{idx}] must be a string, got {type(item).__name__}"
            )
        item = item.strip()
        if not item:
            raise CharterError(f"charter field {key!r}[{idx}] must be non-empty")
        out.append(item)
    return tuple(out)


def load_charter(path: Path | None = None) -> Charter:
    """Load + validate the charter. Raises :class:`CharterError` on any defect."""
    target = Path(path) if path is not None else _CHARTER_PATH
    if not target.exists():
        raise CharterError(f"charter file not found: {target}")
    try:
        with target.open("r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except json.JSONDecodeError as exc:
        raise CharterError(f"charter file is not valid JSON: {exc}") from exc
    except OSError as exc:
        raise CharterError(f"charter file unreadable: {exc}") from exc

    if not isinstance(raw, Mapping):
        raise CharterError("charter file must contain a JSON object at the top level")

    fields: dict[str, Any] = {}
    for key in _REQUIRED_STR_FIELDS:
        fields[key] = _coerce_str(raw, key)
    for key in _REQUIRED_LIST_FIELDS:
        fields[key] = _coerce_str_list(raw, key)

    return Charter(**fields)


def charter_hash(charter: Charter) -> str:
    """Deterministic sha256 fingerprint of a loaded charter (canonical JSON)."""
    if not isinstance(charter, Charter):
        raise CharterError(f"charter_hash expected Charter, got {type(charter).__name__}")
    canonical = json.dumps(asdict(charter), sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def charter_bytes_sha256(charter_file_bytes: bytes) -> str:
    """sha256 hex of the EXACT on-disk charter bytes (used by the sig binding)."""
    return hashlib.sha256(charter_file_bytes).hexdigest()


__all__ = [
    "Charter",
    "CharterError",
    "charter_path",
    "load_charter",
    "charter_hash",
    "charter_bytes_sha256",
]
