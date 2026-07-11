"""Vapi webhook HMAC-SHA256 signature verification."""
from __future__ import annotations

import hmac
from hashlib import sha256

import pytest

from backend.voice.signature import (
    VapiSignatureError,
    compute_signature,
    verify_vapi_secret_header,
    verify_vapi_signature,
    verify_vapi_webhook,
)


SECRET = "test-secret-32-bytes-of-entropy!"
BODY = b'{"message": {"type": "end-of-call-report", "call": {"id": "c1"}}}'


def _hex_sig(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode("utf-8"), body, sha256).hexdigest()


def test_compute_signature_matches_canonical_hmac():
    assert compute_signature(SECRET, BODY) == _hex_sig(SECRET, BODY)


def test_compute_signature_requires_secret():
    with pytest.raises(ValueError):
        compute_signature("", BODY)


def test_compute_signature_requires_bytes_body():
    with pytest.raises(TypeError):
        compute_signature(SECRET, "not bytes")  # type: ignore[arg-type]


# --- x-vapi-secret header (Vapi's actual server.secret mechanism) -----------

def test_verify_secret_header_accepts_match():
    verify_vapi_secret_header(SECRET, SECRET)  # no raise


def test_verify_secret_header_rejects_mismatch():
    with pytest.raises(VapiSignatureError):
        verify_vapi_secret_header("wrong-secret", SECRET)


def test_verify_secret_header_rejects_missing():
    with pytest.raises(VapiSignatureError, match="missing x-vapi-secret"):
        verify_vapi_secret_header(None, SECRET)


# --- combined verifier: accept EITHER mechanism -----------------------------

def test_webhook_accepts_raw_secret_header():
    # This is the path that was 403'ing before the fix.
    verify_vapi_webhook(BODY, signature=None, secret_header=SECRET, secret=SECRET)


def test_webhook_accepts_hmac_signature():
    verify_vapi_webhook(
        BODY, signature=_hex_sig(SECRET, BODY), secret_header=None, secret=SECRET,
    )


def test_webhook_rejects_when_neither_present():
    with pytest.raises(VapiSignatureError, match="missing x-vapi-secret and x-vapi-signature"):
        verify_vapi_webhook(BODY, signature=None, secret_header=None, secret=SECRET)


def test_webhook_rejects_bad_secret_header_even_if_signature_absent():
    with pytest.raises(VapiSignatureError):
        verify_vapi_webhook(BODY, signature=None, secret_header="nope", secret=SECRET)


def test_verify_accepts_bare_hex_digest():
    sig = _hex_sig(SECRET, BODY)
    verify_vapi_signature(BODY, sig, SECRET)


def test_verify_accepts_sha256_prefix():
    sig = "sha256=" + _hex_sig(SECRET, BODY)
    verify_vapi_signature(BODY, sig, SECRET)


def test_verify_is_case_insensitive_on_hex():
    sig = _hex_sig(SECRET, BODY).upper()
    verify_vapi_signature(BODY, sig, SECRET)


def test_verify_rejects_missing_secret():
    sig = _hex_sig(SECRET, BODY)
    with pytest.raises(VapiSignatureError) as ei:
        verify_vapi_signature(BODY, sig, "")
    assert "not configured" in str(ei.value)


def test_verify_rejects_missing_header():
    with pytest.raises(VapiSignatureError) as ei:
        verify_vapi_signature(BODY, None, SECRET)
    assert "missing" in str(ei.value)
    with pytest.raises(VapiSignatureError):
        verify_vapi_signature(BODY, "", SECRET)


def test_verify_rejects_non_hex():
    with pytest.raises(VapiSignatureError) as ei:
        verify_vapi_signature(BODY, "not-hex-at-all", SECRET)
    assert "not a valid hex digest" in str(ei.value)


def test_verify_rejects_wrong_signature():
    bad_sig = _hex_sig("wrong-secret", BODY)
    with pytest.raises(VapiSignatureError) as ei:
        verify_vapi_signature(BODY, bad_sig, SECRET)
    assert "did not verify" in str(ei.value)


def test_verify_rejects_tampered_body():
    sig = _hex_sig(SECRET, BODY)
    tampered = BODY + b"x"
    with pytest.raises(VapiSignatureError):
        verify_vapi_signature(tampered, sig, SECRET)