"""Tests for backend.common.notify — the shared operator push channel.

Network is fully mocked (httpx.Client is patched inside the notify module).
No live calls. Covers: (a) a successful webhook POST, (b) dedup suppression
within TTL, (c) no-webhook -> False no raise, (d) a POST exception swallowed.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from backend.common import notify


_WEBHOOK = "https://discord.com/api/webhooks/123/abc"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    """Reset the dedup cache + strip the webhook env var before each test."""
    notify._reset_dedup_cache()
    monkeypatch.delenv("SAMUS_BRIEF_DISCORD_WEBHOOK", raising=False)
    monkeypatch.delenv("SAMUS_NOTIFY_DEDUP_TTL_SEC", raising=False)
    yield
    notify._reset_dedup_cache()


def _mock_client(status_code: int = 204):
    """Return a MagicMock usable as ``httpx.Client(...)`` context manager whose
    .post returns a response with ``status_code``. Also returns the inner mock
    so the test can assert on the POST call."""
    resp = MagicMock()
    resp.status_code = status_code
    client = MagicMock()
    client.post.return_value = resp
    ctx = MagicMock()
    ctx.__enter__.return_value = client
    ctx.__exit__.return_value = False
    factory = MagicMock(return_value=ctx)
    return factory, client


# --------------------------------------------------------------------------
# (a) successful POST
# --------------------------------------------------------------------------


def test_notify_success(monkeypatch):
    monkeypatch.setenv("SAMUS_BRIEF_DISCORD_WEBHOOK", _WEBHOOK)
    factory, client = _mock_client(204)
    with patch.object(notify.httpx, "Client", factory):
        ok = notify.notify_operator("Test", "body", severity="critical")
    assert ok is True
    client.post.assert_called_once()
    # Posted to the SAME webhook resolved from morning_send's env var.
    args, kwargs = client.post.call_args
    assert args[0] == _WEBHOOK
    assert "content" in kwargs["json"]
    assert "CRITICAL" in kwargs["json"]["content"]
    assert "Test" in kwargs["json"]["content"]


# --------------------------------------------------------------------------
# (b) dedup suppresses a repeat within TTL
# --------------------------------------------------------------------------


def test_notify_dedup_suppresses_repeat(monkeypatch):
    monkeypatch.setenv("SAMUS_BRIEF_DISCORD_WEBHOOK", _WEBHOOK)
    factory, client = _mock_client(204)
    with patch.object(notify.httpx, "Client", factory):
        first = notify.notify_operator("Flap", "one", dedup_key="k1")
        second = notify.notify_operator("Flap", "two", dedup_key="k1")
    assert first is True
    assert second is True  # suppressed-as-dedup still reports success
    # Only ONE POST hit the wire despite two calls.
    assert client.post.call_count == 1


def test_notify_distinct_dedup_keys_both_send(monkeypatch):
    monkeypatch.setenv("SAMUS_BRIEF_DISCORD_WEBHOOK", _WEBHOOK)
    factory, client = _mock_client(204)
    with patch.object(notify.httpx, "Client", factory):
        notify.notify_operator("A", "a", dedup_key="ka")
        notify.notify_operator("B", "b", dedup_key="kb")
    assert client.post.call_count == 2


def test_notify_no_dedup_key_always_sends(monkeypatch):
    monkeypatch.setenv("SAMUS_BRIEF_DISCORD_WEBHOOK", _WEBHOOK)
    factory, client = _mock_client(204)
    with patch.object(notify.httpx, "Client", factory):
        notify.notify_operator("X", "1")
        notify.notify_operator("X", "2")
    assert client.post.call_count == 2


# --------------------------------------------------------------------------
# (c) no webhook configured -> returns False, no raise
# --------------------------------------------------------------------------


def test_notify_no_webhook_returns_false(monkeypatch):
    # env var stripped by the autouse fixture.
    factory, client = _mock_client(204)
    with patch.object(notify.httpx, "Client", factory):
        ok = notify.notify_operator("Test", "body")
    assert ok is False
    client.post.assert_not_called()


def test_notify_non_http_webhook_returns_false(monkeypatch):
    monkeypatch.setenv("SAMUS_BRIEF_DISCORD_WEBHOOK", "ftp://nope")
    ok = notify.notify_operator("Test", "body")
    assert ok is False


# --------------------------------------------------------------------------
# (d) a POST exception is swallowed -> returns False, no raise
# --------------------------------------------------------------------------


def test_notify_post_exception_swallowed(monkeypatch):
    monkeypatch.setenv("SAMUS_BRIEF_DISCORD_WEBHOOK", _WEBHOOK)

    def _boom(*a, **k):
        raise RuntimeError("transport exploded")

    with patch.object(notify.httpx, "Client", _boom):
        ok = notify.notify_operator("Test", "body")
    assert ok is False  # never raises out


def test_notify_http_4xx_returns_false(monkeypatch):
    monkeypatch.setenv("SAMUS_BRIEF_DISCORD_WEBHOOK", _WEBHOOK)
    factory, client = _mock_client(429)
    with patch.object(notify.httpx, "Client", factory):
        ok = notify.notify_operator("Test", "body")
    assert ok is False


def test_notify_importable_from_backend():
    """The public helper must be importable for scripts (PYTHONPATH=/opt/samus)."""
    from backend.common.notify import notify_operator as _fn

    assert callable(_fn)
