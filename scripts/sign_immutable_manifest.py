"""Operator-Ed25519 signer for Samus's immutable baseline + charter.

**DO NOT RUN UNLESS YOU JUST INTENTIONALLY RE-SEEDED THE BASELINE / EDITED THE
CHARTER.** Blesses the current on-disk artifact with an operator signature so
the boot-time check records ``production_ready=true``.

REUSES the canonical operator-signed-envelope crypto from ``_shared/security``
(the SAME operator root key that signs the peer-quorum roster + the other
agents' immutable baselines) — no second Ed25519 path. The private half is
operator-only (DPAPI scope ``MehOperatorBreakGlass`` / name ``Ed25519Private``);
an on-box attacker who can write ``backend/identity/`` CANNOT forge this.

Two targets::

    --target baseline   (default) sign backend/identity/immutable_baseline.json
                        -> writes immutable_baseline.json.ed25519.sig
    --target charter    sign backend/identity/charter.json
                        -> writes charter.json.ed25519.sig

Usage (from the Samus repo root, in the agent's venv, in the OPERATOR's own
console so the DPAPI-sealed private key is decryptable)::

    .venv\\Scripts\\python.exe scripts/sign_immutable_manifest.py --ed25519 --yes
    .venv\\Scripts\\python.exe scripts/sign_immutable_manifest.py --target charter --ed25519 --yes

Off-box signing (operator signs on the break-glass box)::

    ...sign_immutable_manifest.py --emit-request
    ...sign_immutable_manifest.py --yes --signature-hex <128hex> --signed-at <epoch>

Exit codes:
  0 -- signature written (or request emitted)
  1 -- artifact missing / unreadable
  2 -- operator did not pass --yes
  3 -- import failed
  4 -- operator private key not accessible (off-box request emitted instead)
  5 -- self-verify against operator_root_pubkey.json failed
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_ECOSYSTEM_ROOT = _REPO_ROOT.parent  # parent of Samus/, where _shared lives
for _p in (str(_REPO_ROOT), str(_ECOSYSTEM_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

OPERATOR_PUBKEY_PATH = (
    _ECOSYSTEM_ROOT / "_shared" / "security" / "operator_root_pubkey.json"
)


def _artifact_and_payload_builder(target: str):
    from backend.identity import baseline_signature, charter_signature, immutable_manifest
    from backend.identity.charter import charter_path

    if target == "charter":
        return charter_path(), charter_signature.build_signing_payload
    return immutable_manifest.BASELINE_PATH, baseline_signature.build_signing_payload


def _sig_path_for(artifact: Path) -> Path:
    return artifact.with_suffix(artifact.suffix + ".ed25519.sig")


def _request_path(artifact: Path) -> Path:
    return artifact.with_suffix(artifact.suffix + ".ed25519.signrequest.json")


def _write_envelope(sig_path: Path, payload: dict, signed_at: float, sig_hex: str) -> None:
    env = {
        "payload": payload,
        "signed_at_ts": float(signed_at),
        "signature_hex": sig_hex,
        "key_id": "operator_root",
    }
    sig_path.write_text(
        json.dumps(env, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Operator-Ed25519 sign Samus baseline/charter (one-shot).",
    )
    parser.add_argument("--target", choices=("baseline", "charter"), default="baseline")
    parser.add_argument("--yes", action="store_true", help="Confirm signing intent.")
    parser.add_argument("--ed25519", action="store_true", help="(default) Ed25519 mode.")
    parser.add_argument("--key-file", default=None,
                        help="operator private key (PEM/PKCS8 or 64-hex). If "
                             "omitted, loaded from DPAPI (operator console only).")
    parser.add_argument("--emit-request", action="store_true",
                        help="Emit off-box signing request, do not sign.")
    parser.add_argument("--signature-hex", default=None,
                        help="Install an off-box-produced signature (128 hex).")
    parser.add_argument("--signed-at", default=None,
                        help="epoch the off-box envelope was stamped with.")
    args = parser.parse_args()

    bar = "=" * 72
    print(bar)
    print(f"  SAMUS {args.target} signer — OPERATOR-Ed25519 (production)")
    print(bar)
    print(f"  Verified at boot against {OPERATOR_PUBKEY_PATH}")
    print("  An on-box attacker who can write backend/identity/ CANNOT forge this.")
    print()

    try:
        artifact, build_payload = _artifact_and_payload_builder(args.target)
        from _shared.security.autonomy_attestation import (
            load_operator_privkey,
            load_operator_pubkey,
            signing_bytes,
        )
        from cryptography.exceptions import InvalidSignature
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: import failed: {exc!r}", file=sys.stderr)
        return 3

    if not artifact.exists():
        print(f"ERROR: {args.target} not found: {artifact}", file=sys.stderr)
        if args.target == "baseline":
            print("  Run scripts/seed_immutable_manifest.py first.", file=sys.stderr)
        return 1

    try:
        artifact_bytes = artifact.read_bytes()
    except OSError as exc:
        print(f"ERROR: read failed: {exc!r}", file=sys.stderr)
        return 1

    payload = build_payload(artifact_bytes)
    sig_path = _sig_path_for(artifact)

    # ---- --emit-request -------------------------------------------------
    if args.emit_request:
        signed_at = float(args.signed_at) if args.signed_at else time.time()
        signed = signing_bytes(payload, signed_at, "operator_root")
        req = {
            "what": f"operator-Ed25519 signature over Samus {args.target}",
            "artifact_path": str(artifact),
            "payload": payload,
            "signed_at_ts": signed_at,
            "key_id": "operator_root",
            "signing_bytes_hex": signed.hex(),
            "instructions": (
                "On the operator break-glass box, sign signing_bytes_hex with "
                "the operator root Ed25519 key, then re-run with --yes "
                f"--signature-hex <sig> --signed-at {signed_at}."
            ),
        }
        rp = _request_path(artifact)
        rp.write_text(json.dumps(req, indent=2) + "\n", encoding="utf-8")
        print(f"EMITTED off-box signing request: {rp}")
        print(f"  signing_bytes_hex: {signed.hex()}")
        return 0

    # ---- install an off-box signature -----------------------------------
    if args.signature_hex:
        if not args.yes:
            print("Pass --yes to confirm. Aborting.", file=sys.stderr)
            return 2
        if not args.signed_at:
            print("ERROR: --signature-hex requires --signed-at.", file=sys.stderr)
            return 4
        signed_at = float(args.signed_at)
        signed = signing_bytes(payload, signed_at, "operator_root")
        try:
            pub = load_operator_pubkey(OPERATOR_PUBKEY_PATH)
            pub.verify(bytes.fromhex(args.signature_hex.strip()), signed)
        except (InvalidSignature, ValueError) as exc:
            print(f"ABORT: off-box sig does NOT verify: {exc}. Nothing written.",
                  file=sys.stderr)
            return 5
        _write_envelope(sig_path, payload, signed_at, args.signature_hex.strip())
        print(f"INSTALLED off-box operator-Ed25519 signature: {sig_path}")
        return 0

    # ---- default: sign on-box -------------------------------------------
    if not args.yes:
        print("Pass --yes to confirm. Aborting.", file=sys.stderr)
        return 2

    signed_at = float(args.signed_at) if args.signed_at else time.time()
    signed = signing_bytes(payload, signed_at, "operator_root")

    try:
        priv = load_operator_privkey(args.key_file)
    except Exception as exc:  # noqa: BLE001
        print("OPERATOR PRIVATE KEY NOT ACCESSIBLE on this host:", file=sys.stderr)
        print(f"  {exc}", file=sys.stderr)
        rp = _request_path(artifact)
        rp.write_text(json.dumps({
            "what": f"operator-Ed25519 signature over Samus {args.target}",
            "artifact_path": str(artifact),
            "payload": payload,
            "signed_at_ts": signed_at,
            "key_id": "operator_root",
            "signing_bytes_hex": signed.hex(),
        }, indent=2) + "\n", encoding="utf-8")
        print(f"  Emitted off-box request instead of silent fallback: {rp}")
        return 4

    sig = priv.sign(signed)
    try:
        pub = load_operator_pubkey(OPERATOR_PUBKEY_PATH)
        pub.verify(sig, signed)
    except (InvalidSignature, OSError, ValueError) as exc:
        print(f"ABORT: signature does NOT verify against {OPERATOR_PUBKEY_PATH} "
              f"({exc}). Nothing written.", file=sys.stderr)
        return 5

    _write_envelope(sig_path, payload, signed_at, sig.hex())
    print(f"SIGNED + self-verified operator-Ed25519: {sig_path}")
    print(f"  artifact bytes: {len(artifact_bytes)}")
    print(f"  sha256:         {list(payload.values())[-1]}")
    print(f"  signed_at_ts:   {signed_at}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
