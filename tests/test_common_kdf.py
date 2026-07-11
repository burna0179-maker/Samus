"""HKDF-SHA256 — RFC 5869 vector + namespace constants.

Verifies the stdlib HKDF impl against the published RFC 5869 Test
Case 1, plus the Samus namespace constants are stable.
"""
from __future__ import annotations

import pytest

from backend.common.kdf import SAMUS_LEDGER_SALT_V1, hkdf_sha256


# RFC 5869 §A.1 — Test Case 1
_RFC_IKM = bytes.fromhex("0b" * 22)
_RFC_SALT = bytes.fromhex("000102030405060708090a0b0c")
_RFC_INFO = bytes.fromhex("f0f1f2f3f4f5f6f7f8f9")
_RFC_OKM_42 = bytes.fromhex(
    "3cb25f25faacd57a90434f64d0362f2a"
    "2d2d0a90cf1a5a4c5db02d56ecc4c5bf"
    "34007208d5b887185865"
)


def test_hkdf_matches_rfc5869_case1() -> None:
    out = hkdf_sha256(ikm=_RFC_IKM, salt=_RFC_SALT, info=_RFC_INFO, length=42)
    assert out == _RFC_OKM_42


def test_hkdf_default_length_is_32() -> None:
    out = hkdf_sha256(ikm=b"a-secret-of-some-length", salt=SAMUS_LEDGER_SALT_V1, info=b"epoch-0")
    assert len(out) == 32


def test_hkdf_different_info_yields_independent_keys() -> None:
    a = hkdf_sha256(ikm=b"k", salt=SAMUS_LEDGER_SALT_V1, info=b"epoch-1")
    b = hkdf_sha256(ikm=b"k", salt=SAMUS_LEDGER_SALT_V1, info=b"epoch-2")
    assert a != b
    assert len(a) == len(b) == 32


def test_hkdf_different_salt_yields_independent_keys() -> None:
    a = hkdf_sha256(ikm=b"k", salt=b"samus-ledger-v1", info=b"epoch-1")
    b = hkdf_sha256(ikm=b"k", salt=b"some-other-namespace", info=b"epoch-1")
    assert a != b


def test_hkdf_rejects_zero_length() -> None:
    with pytest.raises(ValueError):
        hkdf_sha256(ikm=b"x", salt=b"s", info=b"i", length=0)


def test_hkdf_rejects_over_max_length() -> None:
    with pytest.raises(ValueError):
        hkdf_sha256(ikm=b"x", salt=b"s", info=b"i", length=255 * 32 + 1)


def test_hkdf_rejects_nonbytes() -> None:
    with pytest.raises(TypeError):
        hkdf_sha256(ikm="not-bytes", salt=b"s", info=b"i")  # type: ignore[arg-type]


def test_samus_ledger_salt_is_namespaced_v1() -> None:
    """Bumping this is a derivation-scheme change — read the kdf docstring first."""
    assert SAMUS_LEDGER_SALT_V1 == b"samus-ledger-v1"
