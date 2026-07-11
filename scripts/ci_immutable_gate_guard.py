"""CI guard: fail the build if the immutable baseline drifted from its signature.

Backstop for the failure that shipped stale through the 2026-07-06 arch-completion
merges: the 12 manifested source files hashed to a new baseline, but the operator
Ed25519 sidecar (``immutable_baseline.json.ed25519.sig``) still blessed the OLD
hash, so ``SAMUS_IMMUTABLE_GATE_MODE=enforce`` booted as TAMPER in production while
every dev/test checkout stayed green (the gate only fails closed when SAMUS_ENV is
production). Nothing in the normal test suite catches that window. This does.

Two drift modes, both caught with the Python STANDARD LIBRARY ONLY — no
``_shared`` tree, no ``cryptography`` — so it runs on a bare CI runner that has
neither the ecosystem repo nor the operator key material:

  A. RESEED-MISSING — a manifested source file changed but
     ``immutable_baseline.json`` was not reseeded: a recorded per-file hash no
     longer matches the file on disk.
  B. RESIGN-MISSING — the baseline WAS reseeded (so A passes) but the sidecar
     was not re-signed: the signature's recorded ``baseline_sha256`` no longer
     equals the current ``immutable_baseline.json`` hash. This is the exact
     gap that shipped stale.

When ``_shared.security`` + ``cryptography`` happen to be importable (a full
ecosystem checkout), it ALSO runs the canonical Ed25519 verification via
``backend.identity.baseline_signature.check_baseline_signature`` — a real
signature check, not just a hash compare — and fails on any tamper posture.
That path is best-effort: its absence never turns a clean tree red.

Exit 0 = clean. Exit 1 = drift (prints the mode + the exact remediation).
Exit 2 = the guard could not run (missing baseline / sig / malformed JSON).
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_BASELINE = _REPO_ROOT / "backend" / "identity" / "immutable_baseline.json"
_SIG = _REPO_ROOT / "backend" / "identity" / "immutable_baseline.json.ed25519.sig"

_RESIGN_CMD = (
    "  cd Samus\n"
    "  .venv\\Scripts\\python.exe scripts/sign_immutable_manifest.py --emit-request\n"
    "  # operator signs on the break-glass box, then:\n"
    "  .venv\\Scripts\\python.exe scripts/sign_immutable_manifest.py --yes "
    "--signature-hex <128hex> --signed-at <epoch>\n"
    "  # (or, on the operator console with the DPAPI key: ... --yes)"
)
_RESEED_CMD = (
    "  # a manifested file changed intentionally? reseed THEN re-sign:\n"
    "  .venv\\Scripts\\python.exe scripts/seed_immutable_manifest.py\n"
    + _RESIGN_CMD
)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _fail(mode: str, detail: str, remediation: str) -> int:
    print(f"IMMUTABLE-GATE-GUARD: DRIFT ({mode})", file=sys.stderr)
    print(f"  {detail}", file=sys.stderr)
    print("  remediation:", file=sys.stderr)
    print(remediation, file=sys.stderr)
    return 1


def main() -> int:
    if not _BASELINE.exists():
        print(f"IMMUTABLE-GATE-GUARD: baseline missing: {_BASELINE}", file=sys.stderr)
        return 2
    if not _SIG.exists():
        print(
            f"IMMUTABLE-GATE-GUARD: signature sidecar missing: {_SIG}\n"
            "  the baseline carries no operator signature — it is unsigned "
            "(production_ready=False).",
            file=sys.stderr,
        )
        return 1

    try:
        baseline_bytes = _BASELINE.read_bytes()
        recorded = json.loads(baseline_bytes)["hashes"]
        sig_payload = json.loads(_SIG.read_text(encoding="utf-8"))["payload"]
    except (OSError, json.JSONDecodeError, KeyError) as exc:
        print(f"IMMUTABLE-GATE-GUARD: unreadable baseline/sig: {exc!r}", file=sys.stderr)
        return 2

    # --- Mode A: every manifested file matches its recorded hash -------------
    drifted: list[str] = []
    missing: list[str] = []
    for rel, want in recorded.items():
        fp = _REPO_ROOT / rel
        if not fp.exists():
            missing.append(rel)
            continue
        if _sha256(fp.read_bytes()) != want:
            drifted.append(rel)
    if missing:
        return _fail(
            "MANIFEST-INCOMPLETE",
            f"{len(missing)} manifested file(s) absent: {', '.join(sorted(missing))}",
            _RESEED_CMD,
        )
    if drifted:
        return _fail(
            "RESEED-MISSING",
            f"{len(drifted)} manifested file(s) changed without a baseline reseed: "
            + ", ".join(sorted(drifted)),
            _RESEED_CMD,
        )

    # --- Mode B: the signature blesses the CURRENT baseline -----------------
    current_baseline_hash = _sha256(baseline_bytes)
    signed_hash = str(sig_payload.get("baseline_sha256", ""))
    if signed_hash != current_baseline_hash:
        return _fail(
            "RESIGN-MISSING",
            "signature blesses a stale baseline: sig baseline_sha256="
            f"{signed_hash[:16]}… but current immutable_baseline.json hashes to "
            f"{current_baseline_hash[:16]}… — the baseline was reseeded without a "
            "matching operator re-sign.",
            _RESIGN_CMD,
        )

    # --- Best-effort: real Ed25519 verify when the crypto stack is present ---
    # The canonical verify needs the _shared operator-signed-envelope primitive,
    # which a bare CI runner (hf-samus checked out alone, no ecosystem tree) does
    # NOT have. check_baseline_signature() then fails CLOSED (tamper=True) — the
    # correct call for the production BOOT gate, but WRONG for this guard: the
    # stdlib Mode A+B checks above are authoritative here, and per this module's
    # contract the crypto verify is bonus whose ABSENCE must never turn a clean
    # tree red. So gate it on the same ensure_shared_importable() signal the
    # identity layer itself uses — skip when _shared is absent (CI), enforce the
    # tamper verdict only when it is present (a full ecosystem checkout).
    crypto_note = "skipped (shared crypto/pubkey not importable in this runner)"
    try:
        sys.path.insert(0, str(_REPO_ROOT))
        from backend.identity.shared_bootstrap import ensure_shared_importable

        if not ensure_shared_importable():
            raise ModuleNotFoundError(
                "_shared crypto tree absent — skipping best-effort canonical verify"
            )

        from backend.identity.baseline_signature import check_baseline_signature

        result = check_baseline_signature(_BASELINE)
        if getattr(result, "tamper", False):
            return _fail(
                "SIGNATURE-INVALID",
                f"canonical verify reports tamper: {getattr(result, 'detail', result)}",
                _RESIGN_CMD,
            )
        crypto_note = f"Ed25519 verify OK (posture={getattr(result, 'posture', '?')})"
    except Exception as exc:  # noqa: BLE001 — absence must not fail a clean tree
        crypto_note = f"skipped ({type(exc).__name__})"

    print(
        "IMMUTABLE-GATE-GUARD: OK — "
        f"{len(recorded)} manifested files match; signature blesses current "
        f"baseline ({current_baseline_hash[:16]}…); crypto: {crypto_note}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
