"""HMAC sign / verify primitives."""

from __future__ import annotations


def test_sign_request_deterministic():
    from backend.common.security import sign_request

    sig1 = sign_request("k", "POST", "/work", "1700000000", "abc", b'{"x":1}')
    sig2 = sign_request("k", "POST", "/work", "1700000000", "abc", b'{"x":1}')
    assert sig1 == sig2
    assert len(sig1) == 64
    assert all(c in "0123456789abcdef" for c in sig1)


def test_sign_request_changes_with_body():
    from backend.common.security import sign_request

    sig1 = sign_request("k", "POST", "/work", "1700000000", "abc", b'{"x":1}')
    sig2 = sign_request("k", "POST", "/work", "1700000000", "abc", b'{"x":2}')
    assert sig1 != sig2


def test_safe_compare():
    from backend.common.security import safe_compare

    assert safe_compare("abc", "abc") is True
    assert safe_compare("abc", "abd") is False


def test_nonce_unique():
    from backend.common.security import generate_nonce

    a = generate_nonce()
    b = generate_nonce()
    assert a != b
    assert len(a) == 32
    assert all(c in "0123456789abcdef" for c in a)


def test_timestamp_is_numeric_string():
    from backend.common.security import generate_timestamp

    ts = generate_timestamp()
    assert ts.isdigit()
    assert int(ts) > 0
