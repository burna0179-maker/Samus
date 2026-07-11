"""signed_post_json_sync — sync HMAC-signed POST for sync producers.

Exercises the wrapper used by seo / proposal / finance to dispatch CRM
work through the gateway. The async ``signed_post_json`` is covered by
its own tests; this file pins the sync variant's contract: same headers,
same retry shape, raises on persistent failure.
"""

from __future__ import annotations

import httpx
import pytest

from backend.common import http_client


class _FakeSyncClient:
    """Records every .post() call. Returns a canned response or raises."""

    def __init__(
        self,
        *,
        response: httpx.Response | None = None,
        raise_exc: Exception | None = None,
        fail_first_n: int = 0,
    ):
        self._response = response
        self._raise_exc = raise_exc
        self._fail_first_n = fail_first_n
        self.calls: list[dict] = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, url, *, content=None, headers=None):
        call = {"url": url, "content": content, "headers": dict(headers or {})}
        self.calls.append(call)
        if self._fail_first_n > 0:
            self._fail_first_n -= 1
            raise httpx.ConnectError("simulated transient failure")
        if self._raise_exc is not None:
            raise self._raise_exc
        if self._response is None:
            return httpx.Response(200, content=b'{"ok":true}', request=httpx.Request("POST", url))
        return self._response


def _patch_httpx_client(monkeypatch, fake):
    """Replace httpx.Client used inside signed_post_json_sync with our fake."""

    class _Builder:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return fake

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(http_client.httpx, "Client", _Builder)


def _stub_settings(monkeypatch, *, hmac_key: str = "test-hmac-32"):
    class _S:
        shared_hmac_key = hmac_key

    monkeypatch.setattr(http_client, "get_settings", lambda: _S)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_sync_returns_response_on_success(monkeypatch):
    _stub_settings(monkeypatch)
    fake = _FakeSyncClient()
    _patch_httpx_client(monkeypatch, fake)
    resp = http_client.signed_post_json_sync(
        "http://gateway:8080",
        "/dispatch/crm",
        {"task_id": "t", "payload": {"a": 1}, "metadata": {"action": "create_artifact"}},
    )
    assert resp.status_code == 200
    assert len(fake.calls) == 1


def test_sync_sends_required_hmac_headers(monkeypatch):
    _stub_settings(monkeypatch)
    fake = _FakeSyncClient()
    _patch_httpx_client(monkeypatch, fake)
    http_client.signed_post_json_sync(
        "http://gateway:8080",
        "/dispatch/crm",
        {"task_id": "t1", "payload": {}, "metadata": {"action": "x"}},
    )
    hdrs = fake.calls[0]["headers"]
    assert "X-Samus-Timestamp" in hdrs
    assert "X-Samus-Nonce" in hdrs
    assert "X-Samus-Signature" in hdrs
    assert "X-Samus-Trace-Id" in hdrs
    assert hdrs["Content-Type"] == "application/json"


def test_sync_normalizes_url_join(monkeypatch):
    """Trailing slash on base + leading slash on path normalizes to one slash."""
    _stub_settings(monkeypatch)
    fake = _FakeSyncClient()
    _patch_httpx_client(monkeypatch, fake)
    http_client.signed_post_json_sync(
        "http://gateway:8080/",
        "/dispatch/crm",
        {},
    )
    assert fake.calls[0]["url"] == "http://gateway:8080/dispatch/crm"


# ---------------------------------------------------------------------------
# Auth gate
# ---------------------------------------------------------------------------


def test_sync_raises_when_hmac_key_unset(monkeypatch):
    _stub_settings(monkeypatch, hmac_key="")
    with pytest.raises(RuntimeError) as ei:
        http_client.signed_post_json_sync("http://gateway:8080", "/x", {})
    assert "shared_hmac_key" in str(ei.value).lower() or "SAMUS_SHARED_HMAC_KEY" in str(ei.value)


# ---------------------------------------------------------------------------
# Retry behavior
# ---------------------------------------------------------------------------


def test_sync_retries_on_transient_failure(monkeypatch):
    """retries=2 -> 3 total attempts; recover on the third."""
    _stub_settings(monkeypatch)
    fake = _FakeSyncClient(fail_first_n=2)
    _patch_httpx_client(monkeypatch, fake)
    # Patch time.sleep so the test doesn't actually sleep between retries.
    monkeypatch.setattr("time.sleep", lambda _s: None)
    resp = http_client.signed_post_json_sync(
        "http://gateway:8080",
        "/x",
        {},
        retries=2,
    )
    assert resp.status_code == 200
    assert len(fake.calls) == 3  # 1 first try + 2 retries


def test_sync_raises_after_retries_exhausted(monkeypatch):
    """retries=1 -> 2 total attempts, both fail -> last exception bubbles."""
    _stub_settings(monkeypatch)
    fake = _FakeSyncClient(fail_first_n=5)
    _patch_httpx_client(monkeypatch, fake)
    monkeypatch.setattr("time.sleep", lambda _s: None)
    with pytest.raises(httpx.HTTPError):
        http_client.signed_post_json_sync(
            "http://gateway:8080",
            "/x",
            {},
            retries=1,
        )
    # 1 first try + 1 retry = 2 attempts; the third would have succeeded
    # but we exhausted the budget.
    assert len(fake.calls) == 2


def test_sync_no_retry_when_retries_zero(monkeypatch):
    _stub_settings(monkeypatch)
    fake = _FakeSyncClient(raise_exc=httpx.ConnectError("down"))
    _patch_httpx_client(monkeypatch, fake)
    monkeypatch.setattr("time.sleep", lambda _s: None)
    with pytest.raises(httpx.HTTPError):
        http_client.signed_post_json_sync(
            "http://gateway:8080",
            "/x",
            {},
            retries=0,
        )
    assert len(fake.calls) == 1  # single attempt, no retry
