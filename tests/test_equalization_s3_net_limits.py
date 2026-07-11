"""S3 — outbound response byte-cap regression (net_limits).

The httpx guard is *post-hoc* (donor/Major pattern): after a normal buffered
httpx call, :func:`check_httpx_size` inspects the already-read ``.content`` and
raises :class:`ResponseTooLarge` on an over-cap body WITHOUT consuming or
replacing the response. Callers keep using ``resp.json()`` / ``resp.text``.
"""

from __future__ import annotations

import io

import httpx
import pytest

from backend.common.net_limits import (
    INTER_WORKCELL_MAX_BYTES,
    ResponseTooLarge,
    check_httpx_size,
    read_capped,
)


class _FakeUrllibResp:
    def __init__(self, body: bytes) -> None:
        self._buf = io.BytesIO(body)

    def read(self, n: int = -1) -> bytes:
        return self._buf.read(n)


def test_read_capped_under_limit() -> None:
    resp = _FakeUrllibResp(b"x" * 100)
    assert read_capped(resp, max_bytes=1024) == b"x" * 100


def test_read_capped_over_limit_raises() -> None:
    resp = _FakeUrllibResp(b"x" * 2048)
    with pytest.raises(ResponseTooLarge):
        read_capped(resp, max_bytes=1024)


def test_read_capped_zero_limit_invalid() -> None:
    with pytest.raises(ValueError):
        read_capped(_FakeUrllibResp(b""), max_bytes=0)


def _httpx_response(body: bytes, *, content_length: int | None = None) -> httpx.Response:
    headers = {}
    if content_length is not None:
        headers["content-length"] = str(content_length)
    return httpx.Response(200, content=body, headers=headers)


def test_check_httpx_size_under_limit_is_noop() -> None:
    # A small body passes (returns None) and leaves the response usable.
    resp = _httpx_response(b"hello world")
    assert check_httpx_size(resp, max_bytes=1024) is None
    # Body is untouched — the caller's normal decode path still works.
    assert resp.content == b"hello world"
    assert resp.text == "hello world"


def test_check_httpx_size_over_limit_raises() -> None:
    resp = _httpx_response(b"y" * 4096)
    with pytest.raises(ResponseTooLarge):
        check_httpx_size(resp, max_bytes=1024)


def test_check_httpx_size_content_length_fast_reject() -> None:
    # Declared CL over the cap is rejected even if the actual body is small.
    resp = httpx.Response(
        200,
        content=b"tiny",
        headers={"content-length": str(10**9)},
    )
    with pytest.raises(ResponseTooLarge):
        check_httpx_size(resp, max_bytes=1024)


def test_check_httpx_size_zero_limit_invalid() -> None:
    with pytest.raises(ValueError):
        check_httpx_size(_httpx_response(b""), max_bytes=0)


class _MinimalRespNoContent:
    """A response double that omits ``.content`` (mirrors the partial mocks the
    unchanged client tests use). The guard must degrade to a no-op, not crash."""


def test_check_httpx_size_minimal_mock_is_noop() -> None:
    # No ``.content`` / ``.headers`` to measure — guard returns None, no raise.
    assert check_httpx_size(_MinimalRespNoContent(), max_bytes=1024) is None


def test_inter_workcell_cap_is_generous() -> None:
    # Sanity: the default inter-workcell ceiling comfortably admits a normal
    # JSON envelope but is finite.
    assert 0 < INTER_WORKCELL_MAX_BYTES <= 64 * 1024 * 1024
    resp = _httpx_response(b'{"ok": true}')
    assert check_httpx_size(resp, max_bytes=INTER_WORKCELL_MAX_BYTES) is None
