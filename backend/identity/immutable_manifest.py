"""Boot-time integrity gate for Samus's runtime-immutable source set.

Samus historically had NO sealed-manifest / boot-abort-on-drift (the other
agents all do: Major's ``immutable_subsystems`` + ``immutable_baselines``,
Darwin's ``immutable_hashes``). This module adds one, adapted to Samus's
container layout:

* ``backend/identity/immutable_manifest.json`` — a PATH manifest: the list of
  security-critical Samus source files (under ``backend/``) that must not
  drift at runtime. Ships with the read-only image.
* ``backend/identity/immutable_baseline.json`` — the recorded sha256 baseline
  for those paths (seeded by ``scripts/seed_immutable_manifest.py``).
* ``backend/identity/immutable_baseline.json.ed25519.sig`` — the operator-
  Ed25519 signature over the baseline bytes (the production trust anchor;
  same key + shared primitive as the charter / the other agents' baselines).

At boot the gateway lifespan calls :func:`verify_manifest`:

  1. Read the manifest -> list of relative paths under the code root.
  2. Compute sha256 of each path's current bytes.
  3. Diff against the recorded baseline. A missing file or a changed hash is
     DRIFT.
  4. Separately, :func:`check_baseline_trust` establishes whether the baseline
     is operator-signed (production) or merely present (non-production).

FAIL-CLOSED, FLAG-GATED. The gate is wired into the gateway but DORMANT by
default: ``SAMUS_IMMUTABLE_GATE_MODE`` selects ``off`` (default — no work),
``audit`` (verify + log drift, never abort), or ``enforce`` (drift or a
tamper-posture baseline in production ABORTS boot). The default ``off`` means
the running Docker/AWS stack is byte-for-byte unaffected until the operator
arms it — exactly the dormant-build rule the ecosystem follows.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

log = logging.getLogger("samus.identity.immutable_manifest")

# backend/identity/immutable_manifest.py -> parents[1] == backend,
# parents[2] == the Samus code root (/opt/samus in the container).
_IDENTITY_DIR = Path(__file__).resolve().parent
_CODE_ROOT = _IDENTITY_DIR.parents[1]

MANIFEST_PATH = _IDENTITY_DIR / "immutable_manifest.json"
BASELINE_PATH = _IDENTITY_DIR / "immutable_baseline.json"

MISSING_HASH_MARKER = "<missing>"
_BASELINE_KEY = "hashes"

ENV_GATE_MODE = "SAMUS_IMMUTABLE_GATE_MODE"


class ImmutableManifestError(Exception):
    """Raised when the manifest is missing or structurally invalid."""


def gate_mode(env: Mapping[str, str] | None = None) -> str:
    """Resolve the gate posture: off | audit | enforce (default).

    ARMED to ``enforce`` by default (HOTL Tranche 1 operator decision
    2026-07-05). The abort consequence still requires env=production +
    detected drift (see backend/identity/boot.py) — dev boots proceed and
    report posture. Set SAMUS_IMMUTABLE_GATE_MODE=off|audit to stand down.
    """
    env = env if env is not None else os.environ
    raw = (env.get(ENV_GATE_MODE) or "").strip().lower()
    return raw if raw in ("off", "audit", "enforce") else "enforce"


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    drift: list[tuple[str, str, str]] = field(default_factory=list)
    checked: int = 0
    baseline_recorded: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "checked": self.checked,
            "baseline_recorded": self.baseline_recorded,
            "drift": [
                {"path": rel, "expected": exp, "actual": act}
                for rel, exp, act in self.drift
            ],
        }


def code_root() -> Path:
    return _CODE_ROOT


def _validate_relative_path(item: str, label: str) -> Path:
    candidate = Path(item)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ImmutableManifestError(
            f"manifest {label} must be a relative path inside the code root"
        )
    return candidate


def load_manifest(manifest_path: Path | None = None) -> list[str]:
    """Read the path manifest. Returns the list of relative path strings."""
    target = Path(manifest_path) if manifest_path is not None else MANIFEST_PATH
    if not target.exists():
        raise ImmutableManifestError(f"manifest not found: {target}")
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ImmutableManifestError(f"manifest is not valid JSON: {exc}") from exc
    except OSError as exc:
        raise ImmutableManifestError(f"manifest unreadable: {exc}") from exc

    if not isinstance(raw, Mapping):
        raise ImmutableManifestError("manifest must be a JSON object at the top level")
    subsystems = raw.get("subsystems")
    if not isinstance(subsystems, list):
        raise ImmutableManifestError("manifest 'subsystems' must be a list")

    out: list[str] = []
    for idx, item in enumerate(subsystems):
        if not isinstance(item, str):
            raise ImmutableManifestError(
                f"manifest 'subsystems'[{idx}] must be a string"
            )
        _validate_relative_path(item, f"'subsystems'[{idx}]")
        out.append(item)
    return out


def compute_hashes(
    rel_paths: list[str], *, root: Path | None = None
) -> dict[str, str]:
    """sha256 each rel path under ``root``; missing files get the marker."""
    base = Path(root) if root is not None else _CODE_ROOT
    hashes: dict[str, str] = {}
    for rel in rel_paths:
        target = base / rel
        try:
            data = target.read_bytes()
        except OSError:
            hashes[rel] = MISSING_HASH_MARKER
            continue
        hashes[rel] = hashlib.sha256(data).hexdigest()
    return hashes


def record_baseline(
    *, manifest_path: Path | None = None,
    baseline_path: Path | None = None,
    root: Path | None = None,
) -> dict[str, str]:
    """Seed the baseline from the current source bytes. Returns the hash map."""
    rels = load_manifest(manifest_path)
    hashes = compute_hashes(rels, root=root)
    out_path = Path(baseline_path) if baseline_path is not None else BASELINE_PATH
    out_path.write_text(
        json.dumps({_BASELINE_KEY: hashes}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return hashes


def load_baseline(baseline_path: Path | None = None) -> dict[str, str] | None:
    """Read the recorded baseline hash map; None if the file is absent."""
    target = Path(baseline_path) if baseline_path is not None else BASELINE_PATH
    if not target.exists():
        return None
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ImmutableManifestError(f"baseline unreadable/invalid: {exc}") from exc
    if not isinstance(raw, Mapping) or not isinstance(raw.get(_BASELINE_KEY), Mapping):
        raise ImmutableManifestError("baseline must carry a 'hashes' object")
    return {str(k): str(v) for k, v in raw[_BASELINE_KEY].items()}


def verify_manifest(
    *, manifest_path: Path | None = None,
    baseline_path: Path | None = None,
    root: Path | None = None,
) -> VerifyResult:
    """Compare current source hashes against the recorded baseline.

    Returns ``ok=False`` with a drift list if any path's hash changed or the
    file disappeared. When NO baseline exists yet (first boot), returns
    ``ok=True, baseline_recorded=False`` (nothing to compare — never a
    false-positive abort).
    """
    rels = load_manifest(manifest_path)
    current = compute_hashes(rels, root=root)
    baseline = load_baseline(baseline_path)
    if baseline is None:
        return VerifyResult(
            ok=True, drift=[], checked=len(current), baseline_recorded=False
        )

    drift: list[tuple[str, str, str]] = []
    for rel in rels:
        expected = baseline.get(rel)
        actual = current.get(rel, MISSING_HASH_MARKER)
        if expected is None:
            # New path in the manifest with no recorded baseline entry — drift
            # (the baseline must be re-sealed to bless it).
            drift.append((rel, MISSING_HASH_MARKER, actual))
        elif expected != actual:
            drift.append((rel, expected, actual))
    return VerifyResult(
        ok=not drift, drift=drift, checked=len(current), baseline_recorded=True
    )


def check_baseline_trust(
    *, baseline_path: Path | None = None,
    operator_pubkey_path_override: Path | None = None,
):
    """Establish whether the baseline is operator-Ed25519 signed (production).

    Thin wrapper over the shared operator-signed-envelope primitive — the SAME
    trust anchor the charter + the other agents' baselines use.
    """
    from .baseline_signature import check_baseline_signature  # noqa: PLC0415

    target = Path(baseline_path) if baseline_path is not None else BASELINE_PATH
    return check_baseline_signature(
        target, operator_pubkey_path_override=operator_pubkey_path_override
    )


__all__ = [
    "ImmutableManifestError",
    "VerifyResult",
    "MANIFEST_PATH",
    "BASELINE_PATH",
    "ENV_GATE_MODE",
    "gate_mode",
    "code_root",
    "load_manifest",
    "compute_hashes",
    "record_baseline",
    "load_baseline",
    "verify_manifest",
    "check_baseline_trust",
]
