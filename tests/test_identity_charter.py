"""Tests for Samus's signed identity & charter (ecosystem-core skeleton)."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from backend.identity.charter import (
    Charter,
    CharterError,
    charter_hash,
    charter_path,
    load_charter,
)


def test_charter_loads_and_validates():
    charter = load_charter()
    assert charter.agent_id == "samus"
    assert charter.agent_name == "Samus"
    assert charter.domain == "revenue / fulfillment"
    assert "audit-everything" in charter.values
    # Outward-gate invariant must be present (Samus's core concession).
    assert any("Stake Sentence" in inv for inv in charter.invariants)
    assert any("$1/day" in inv for inv in charter.invariants)


def test_charter_hash_is_deterministic():
    c = load_charter()
    assert charter_hash(c) == charter_hash(load_charter())
    assert len(charter_hash(c)) == 64


def test_charter_hash_changes_on_value_change():
    c = load_charter()
    mutated = Charter(
        agent_id=c.agent_id, agent_name=c.agent_name, lineage=c.lineage,
        version=c.version, domain=c.domain,
        values=c.values + ("rogue-value",), invariants=c.invariants,
    )
    assert charter_hash(mutated) != charter_hash(c)


def test_charter_missing_field_raises(tmp_path: Path):
    bad = tmp_path / "charter.json"
    bad.write_text(json.dumps({"agent_id": "samus"}), encoding="utf-8")
    with pytest.raises(CharterError):
        load_charter(bad)


def test_charter_wrong_type_raises(tmp_path: Path):
    bad = tmp_path / "charter.json"
    bad.write_text(json.dumps({
        "agent_id": "samus", "agent_name": "Samus", "lineage": "x",
        "version": "1", "domain": "rev", "values": "not-a-list",
        "invariants": [],
    }), encoding="utf-8")
    with pytest.raises(CharterError):
        load_charter(bad)


def test_charter_empty_value_raises(tmp_path: Path):
    bad = tmp_path / "charter.json"
    bad.write_text(json.dumps({
        "agent_id": " ", "agent_name": "Samus", "lineage": "x",
        "version": "1", "domain": "rev", "values": ["a"], "invariants": ["b"],
    }), encoding="utf-8")
    with pytest.raises(CharterError):
        load_charter(bad)


def test_charter_path_points_at_shipped_file():
    p = charter_path()
    assert p.name == "charter.json"
    assert p.exists()


# --- charter signature (operator-Ed25519 over the charter bytes) ----------

from backend.identity import charter_signature as cs  # noqa: E402


def test_unsigned_charter_is_non_production(tmp_path):
    """A charter with no sidecar sig -> NONE/non-production. The shipped
    Samus charter IS sealed (operator-signed at 2026-06-22), so this test
    uses a tmp copy *without* the sig sidecar to exercise the unsigned
    branch."""
    bare = tmp_path / "charter.json"
    bare.write_text(charter_path().read_text(encoding="utf-8"), encoding="utf-8")
    res = cs.check_charter_signature(bare)
    assert res.production_ready is False
    assert res.posture == cs.CharterSignaturePosture.NONE


def test_charter_signing_payload_binds_to_bytes():
    data = b'{"x":1}'
    p1 = cs.build_signing_payload(data)
    p2 = cs.build_signing_payload(b'{"x":2}')
    assert p1["purpose"] == "samus_charter"
    assert p1["charter_sha256"] != p2["charter_sha256"]


def _make_signed_sidecar(tmp_path, artifact_bytes, payload, *, tamper=False):
    """Forge a real operator-Ed25519 envelope using a throwaway key + pubkey."""
    from backend.identity.shared_bootstrap import ensure_shared_importable
    assert ensure_shared_importable()
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from _shared.security.operator_signed_envelope import canonical_bytes

    priv = Ed25519PrivateKey.generate()
    pub = priv.public_key()
    from cryptography.hazmat.primitives import serialization
    pub_hex = pub.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    ).hex()
    pubkey_file = tmp_path / "operator_root_pubkey.json"
    pubkey_file.write_text(
        json.dumps({"operator_root_pubkey_hex": pub_hex}), encoding="utf-8"
    )

    signed_at = time.time()
    signing = canonical_bytes(
        {"payload": payload, "signed_at_ts": signed_at, "key_id": "operator_root"}
    )
    sig = priv.sign(signing)
    sig_hex = sig.hex()
    if tamper:
        sig_hex = ("0" * len(sig_hex))
    return signed_at, sig_hex, pubkey_file


def test_charter_signature_valid_verifies(tmp_path: Path):
    artifact = tmp_path / "charter.json"
    artifact.write_bytes(charter_path().read_bytes())
    payload = cs.build_signing_payload(artifact.read_bytes())
    signed_at, sig_hex, pubkey_file = _make_signed_sidecar(
        tmp_path, artifact.read_bytes(), payload
    )
    sig_path = cs.ed25519_sig_path_for(artifact)
    sig_path.write_text(json.dumps({
        "payload": payload, "signed_at_ts": signed_at,
        "signature_hex": sig_hex, "key_id": "operator_root",
    }), encoding="utf-8")

    res = cs.check_charter_signature(
        artifact, operator_pubkey_path_override=pubkey_file
    )
    assert res.production_ready is True
    assert res.posture == cs.CharterSignaturePosture.OPERATOR_ED25519
    assert res.tamper is False


def test_charter_signature_tamper_fails_closed(tmp_path: Path):
    """A charter mutated after signing fails the sha256 binding -> tamper."""
    artifact = tmp_path / "charter.json"
    artifact.write_bytes(charter_path().read_bytes())
    payload = cs.build_signing_payload(artifact.read_bytes())
    signed_at, sig_hex, pubkey_file = _make_signed_sidecar(
        tmp_path, artifact.read_bytes(), payload
    )
    sig_path = cs.ed25519_sig_path_for(artifact)
    sig_path.write_text(json.dumps({
        "payload": payload, "signed_at_ts": signed_at,
        "signature_hex": sig_hex, "key_id": "operator_root",
    }), encoding="utf-8")
    # Now mutate the charter AFTER signing.
    artifact.write_bytes(artifact.read_bytes() + b"\n// tampered")

    res = cs.check_charter_signature(
        artifact, operator_pubkey_path_override=pubkey_file
    )
    assert res.production_ready is False
    assert res.tamper is True
    assert res.signed is True


def test_charter_signature_bad_sig_fails_closed(tmp_path: Path):
    artifact = tmp_path / "charter.json"
    artifact.write_bytes(charter_path().read_bytes())
    payload = cs.build_signing_payload(artifact.read_bytes())
    signed_at, sig_hex, pubkey_file = _make_signed_sidecar(
        tmp_path, artifact.read_bytes(), payload, tamper=True
    )
    sig_path = cs.ed25519_sig_path_for(artifact)
    sig_path.write_text(json.dumps({
        "payload": payload, "signed_at_ts": signed_at,
        "signature_hex": sig_hex, "key_id": "operator_root",
    }), encoding="utf-8")
    res = cs.check_charter_signature(
        artifact, operator_pubkey_path_override=pubkey_file
    )
    assert res.production_ready is False
    assert res.tamper is True
