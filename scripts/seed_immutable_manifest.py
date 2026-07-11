"""Seed Samus's immutable-baseline hash map from the current source bytes.

Reads ``backend/identity/immutable_manifest.json`` (the path manifest) and
writes ``backend/identity/immutable_baseline.json`` — the sha256 of every
listed source file as it stands RIGHT NOW. Run this after an authorized,
operator-reviewed change to any manifested file, then bless the new baseline
with ``scripts/sign_immutable_manifest.py --ed25519 --yes``.

This is intentionally a separate, deliberate step from signing: the operator
reviews the seeded baseline diff BEFORE blessing it, so an attacker with
file-write cannot get a malicious baseline auto-signed.

Usage (from the Samus repo root, in the agent's venv)::

    .venv\\Scripts\\python.exe scripts/seed_immutable_manifest.py

Exit codes:
  0 -- baseline written
  1 -- manifest missing / unreadable
"""
from __future__ import annotations

import sys
from pathlib import Path

# scripts/ -> repo root; make ``backend`` importable.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def main() -> int:
    try:
        from backend.identity.immutable_manifest import (
            BASELINE_PATH,
            ImmutableManifestError,
            record_baseline,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: import failed: {exc!r}", file=sys.stderr)
        return 1

    try:
        hashes = record_baseline()
    except ImmutableManifestError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(f"wrote baseline: {BASELINE_PATH}")
    print(f"  sealed {len(hashes)} paths")
    missing = [p for p, h in hashes.items() if h == "<missing>"]
    if missing:
        print(f"  WARNING: {len(missing)} manifested path(s) are MISSING on disk:")
        for p in missing:
            print(f"    - {p}")
    print()
    print("Next: review the baseline diff, then sign it:")
    print("  .venv\\Scripts\\python.exe scripts/sign_immutable_manifest.py --ed25519 --yes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
