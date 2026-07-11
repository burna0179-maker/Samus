"""Tests for Samus's immutable integrity gate (manifest + Ed25519 + fail-closed)."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from backend.identity import immutable_manifest as im
from backend.identity import baseline_signature as bs


def test_gate_mode_defaults_enforce():
    """ARMED by default — HOTL Tranche 1 operator decision (2026-07-05)."""
    assert im.gate_mode({}) == "enforce"
    assert im.gate_mode({"SAMUS_IMMUTABLE_GATE_MODE": "off"}) == "off"
    assert im.gate_mode({"SAMUS_IMMUTABLE_GATE_MODE": "audit"}) == "audit"
    assert im.gate_mode({"SAMUS_IMMUTABLE_GATE_MODE": "enforce"}) == "enforce"
    assert im.gate_mode({"SAMUS_IMMUTABLE_GATE_MODE": "garbage"}) == "enforce"


def test_manifest_loads_and_lists_paths():
    rels = im.load_manifest()
    assert "backend/common/security.py" in rels
    assert "backend/identity/charter.json" in rels
    # No absolute / traversal paths.
    for r in rels:
        assert not Path(r).is_absolute()
        assert ".." not in Path(r).parts


def test_shipped_baseline_verifies_clean():
    """The committed baseline must match the committed source (no drift)."""
    result = im.verify_manifest()
    assert result.baseline_recorded is True
    assert result.ok is True, result.to_dict()
    assert not result.drift


def test_verify_detects_drift(tmp_path: Path):
    root = tmp_path / "root"
    (root / "backend").mkdir(parents=True)
    src = root / "backend" / "a.py"
    src.write_text("original", encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"subsystems": ["backend/a.py"]}), encoding="utf-8")
    baseline = tmp_path / "baseline.json"
    im.record_baseline(manifest_path=manifest, baseline_path=baseline, root=root)

    # Clean.
    r = im.verify_manifest(manifest_path=manifest, baseline_path=baseline, root=root)
    assert r.ok is True

    # Drift it.
    src.write_text("MUTATED", encoding="utf-8")
    r2 = im.verify_manifest(manifest_path=manifest, baseline_path=baseline, root=root)
    assert r2.ok is False
    assert r2.drift[0][0] == "backend/a.py"


def test_verify_detects_missing_file(tmp_path: Path):
    root = tmp_path / "root"
    (root / "backend").mkdir(parents=True)
    src = root / "backend" / "a.py"
    src.write_text("x", encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"subsystems": ["backend/a.py"]}), encoding="utf-8")
    baseline = tmp_path / "baseline.json"
    im.record_baseline(manifest_path=manifest, baseline_path=baseline, root=root)

    src.unlink()
    r = im.verify_manifest(manifest_path=manifest, baseline_path=baseline, root=root)
    assert r.ok is False
    assert r.drift[0][2] == im.MISSING_HASH_MARKER


def test_verify_no_baseline_is_first_boot(tmp_path: Path):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"subsystems": []}), encoding="utf-8")
    r = im.verify_manifest(manifest_path=manifest, baseline_path=tmp_path / "nope.json")
    assert r.ok is True
    assert r.baseline_recorded is False


def test_manifest_rejects_traversal(tmp_path: Path):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"subsystems": ["../escape.py"]}), encoding="utf-8")
    with pytest.raises(im.ImmutableManifestError):
        im.load_manifest(manifest)


# --- baseline signature (operator-Ed25519) --------------------------------


def test_unsigned_baseline_is_non_production(tmp_path: Path):
    """A baseline with NO signature sidecar must never be production trust.

    Hermetic on purpose: the ORIGINAL version checked the live shipped
    sidecar, so it broke the moment the operator legitimately Ed25519-signed
    the shipped baseline (commit 712aba6 / 15ba589). Copy the baseline to a
    sidecar-less tmp location so the invariant under test (unsigned ->
    non-production) is exercised regardless of the shipped signing state.
    """
    unsigned = tmp_path / "immutable_baseline.json"
    unsigned.write_bytes(im.BASELINE_PATH.read_bytes())
    res = im.check_baseline_trust(baseline_path=unsigned)
    assert res.production_ready is False
    assert res.tamper is False
    assert res.posture == bs.SignaturePosture.NONE


def _signed_sidecar(tmp_path, payload):
    from backend.identity.shared_bootstrap import ensure_shared_importable

    assert ensure_shared_importable()
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from _shared.security.operator_signed_envelope import canonical_bytes

    priv = Ed25519PrivateKey.generate()
    pub_hex = (
        priv.public_key()
        .public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        .hex()
    )
    pubkey_file = tmp_path / "operator_root_pubkey.json"
    pubkey_file.write_text(json.dumps({"operator_root_pubkey_hex": pub_hex}), encoding="utf-8")
    signed_at = time.time()
    signing = canonical_bytes(
        {"payload": payload, "signed_at_ts": signed_at, "key_id": "operator_root"}
    )
    return signed_at, priv.sign(signing).hex(), pubkey_file


def test_baseline_signature_valid_verifies(tmp_path: Path):
    baseline = tmp_path / "immutable_baseline.json"
    baseline.write_text(json.dumps({"hashes": {"a": "b"}}), encoding="utf-8")
    payload = bs.build_signing_payload(baseline.read_bytes())
    signed_at, sig_hex, pubkey_file = _signed_sidecar(tmp_path, payload)
    bs.ed25519_sig_path_for(baseline).write_text(
        json.dumps(
            {
                "payload": payload,
                "signed_at_ts": signed_at,
                "signature_hex": sig_hex,
                "key_id": "operator_root",
            }
        ),
        encoding="utf-8",
    )

    res = bs.check_baseline_signature(baseline, operator_pubkey_path_override=pubkey_file)
    assert res.production_ready is True
    assert res.tamper is False


def test_baseline_signature_tamper_fails_closed(tmp_path: Path):
    baseline = tmp_path / "immutable_baseline.json"
    baseline.write_text(json.dumps({"hashes": {"a": "b"}}), encoding="utf-8")
    payload = bs.build_signing_payload(baseline.read_bytes())
    signed_at, sig_hex, pubkey_file = _signed_sidecar(tmp_path, payload)
    bs.ed25519_sig_path_for(baseline).write_text(
        json.dumps(
            {
                "payload": payload,
                "signed_at_ts": signed_at,
                "signature_hex": sig_hex,
                "key_id": "operator_root",
            }
        ),
        encoding="utf-8",
    )
    # Mutate baseline after signing.
    baseline.write_text(json.dumps({"hashes": {"a": "EVIL"}}), encoding="utf-8")
    res = bs.check_baseline_signature(baseline, operator_pubkey_path_override=pubkey_file)
    assert res.production_ready is False
    assert res.tamper is True
