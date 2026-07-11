"""SendGrid backend (httpx-mocked) + email_backend adapter selector tests."""
from __future__ import annotations

from typing import Any

import httpx
import pytest


# ---------------------------------------------------------------------------
# httpx mocking shape — mirrors test_finance_stripe_client.py
# ---------------------------------------------------------------------------

class _FakeHttpx:
    """Per-module httpx stub. Falls through to real httpx for exception classes."""

    def __init__(self, client_cls: Any) -> None:
        self.Client = client_cls

    def __getattr__(self, name: str) -> Any:
        return getattr(httpx, name)


def _make_client(
    *,
    status: int = 202,
    body: dict | None = None,
    headers: dict | None = None,
    raise_exc: Exception | None = None,
    capture: dict | None = None,
) -> type:
    """Build a fake httpx.Client class with controllable response."""

    class _Resp:
        def __init__(self) -> None:
            self.status_code = status
            self._body = body
            self.text = "" if body is None else str(body)
            self.headers = headers or {}

        def json(self) -> Any:
            if self._body is None:
                raise ValueError("no body")
            return self._body

    class _Client:
        def __init__(self, *a: Any, **kw: Any) -> None:
            if capture is not None:
                capture["client_init"] = kw

        def __enter__(self) -> "_Client":
            return self

        def __exit__(self, *a: Any) -> bool:
            return False

        def post(self, url: str, headers: dict | None = None, json: Any = None):
            if capture is not None:
                capture["url"] = url
                capture["headers"] = headers or {}
                capture["json"] = json
            if raise_exc is not None:
                raise raise_exc
            return _Resp()

    return _Client


def _install_fake_httpx(monkeypatch, client_cls: type) -> None:
    """Patch the sendgrid module's httpx reference with a controllable fake."""
    import backend.common.email_backends.sendgrid as mod
    monkeypatch.setattr(mod, "httpx", _FakeHttpx(client_cls))


# ---------------------------------------------------------------------------
# Settings helpers
# ---------------------------------------------------------------------------

def _set_sendgrid_env(
    monkeypatch,
    *,
    api_key: str = "SG.test_key",
    from_email: str = "samus@example.com",
    from_name: str = "HustleForge",
    base_url: str = "https://api.sendgrid.com",
    backend: str = "sendgrid",
) -> None:
    monkeypatch.setenv("SENDGRID_API_KEY", api_key)
    monkeypatch.setenv("SENDGRID_FROM_EMAIL", from_email)
    monkeypatch.setenv("SENDGRID_FROM_NAME", from_name)
    monkeypatch.setenv("SENDGRID_BASE_URL", base_url)
    monkeypatch.setenv("EMAIL_BACKEND", backend)
    from backend.common.settings import reload_settings
    reload_settings()


# ---------------------------------------------------------------------------
# SendGrid backend: happy path
# ---------------------------------------------------------------------------

def test_sendgrid_happy_path_202_with_message_id(monkeypatch):
    _set_sendgrid_env(monkeypatch)
    capture: dict = {}
    cls = _make_client(status=202, headers={"X-Message-Id": "sg_msg_abc"}, capture=capture)
    _install_fake_httpx(monkeypatch, cls)

    from backend.common.email_backends.sendgrid import send_email_via_sendgrid
    out = send_email_via_sendgrid("lead@example.com", "Subject A", "hello body")

    assert out["message_id"] == "sg_msg_abc"
    assert out["channel"] == "email"
    assert out["to"] == "lead@example.com"
    assert out["ts"]  # iso string present

    # URL is base + send path
    assert capture["url"] == "https://api.sendgrid.com/v3/mail/send"
    # Auth header
    assert capture["headers"]["Authorization"] == "Bearer SG.test_key"
    assert capture["headers"]["Content-Type"] == "application/json"
    # Body shape
    body = capture["json"]
    assert body["personalizations"] == [{"to": [{"email": "lead@example.com"}]}]
    assert body["from"] == {"email": "samus@example.com", "name": "HustleForge"}
    assert body["subject"] == "Subject A"
    # No HTML body provided -> only text/plain
    assert body["content"] == [{"type": "text/plain", "value": "hello body"}]


def test_sendgrid_accepts_any_2xx(monkeypatch):
    _set_sendgrid_env(monkeypatch)
    cls = _make_client(status=200, headers={"X-Message-Id": "ok"})
    _install_fake_httpx(monkeypatch, cls)

    from backend.common.email_backends.sendgrid import send_email_via_sendgrid
    out = send_email_via_sendgrid("a@b.com", "s", "b")
    assert out["message_id"] == "ok"


def test_sendgrid_missing_message_id_header_returns_empty_string(monkeypatch):
    _set_sendgrid_env(monkeypatch)
    cls = _make_client(status=202, headers={})  # no X-Message-Id
    _install_fake_httpx(monkeypatch, cls)

    from backend.common.email_backends.sendgrid import send_email_via_sendgrid
    out = send_email_via_sendgrid("a@b.com", "s", "b")
    assert out["message_id"] == ""
    assert out["channel"] == "email"


def test_sendgrid_message_id_header_case_insensitive(monkeypatch):
    _set_sendgrid_env(monkeypatch)
    cls = _make_client(status=202, headers={"x-message-id": "lowercase_ok"})
    _install_fake_httpx(monkeypatch, cls)

    from backend.common.email_backends.sendgrid import send_email_via_sendgrid
    out = send_email_via_sendgrid("a@b.com", "s", "b")
    assert out["message_id"] == "lowercase_ok"


def test_sendgrid_includes_html_body_as_second_content_entry(monkeypatch):
    _set_sendgrid_env(monkeypatch)
    capture: dict = {}
    cls = _make_client(status=202, capture=capture)
    _install_fake_httpx(monkeypatch, cls)

    from backend.common.email_backends.sendgrid import send_email_via_sendgrid
    send_email_via_sendgrid(
        "a@b.com", "s", "plain text version",
        html_body="<p>html version</p>",
    )
    content = capture["json"]["content"]
    # Plain first, HTML second — SendGrid renders the LAST entry preferentially.
    assert content == [
        {"type": "text/plain", "value": "plain text version"},
        {"type": "text/html", "value": "<p>html version</p>"},
    ]


def test_sendgrid_omits_html_when_blank(monkeypatch):
    _set_sendgrid_env(monkeypatch)
    capture: dict = {}
    cls = _make_client(status=202, capture=capture)
    _install_fake_httpx(monkeypatch, cls)

    from backend.common.email_backends.sendgrid import send_email_via_sendgrid
    send_email_via_sendgrid("a@b.com", "s", "body", html_body="   ")  # whitespace only
    assert len(capture["json"]["content"]) == 1
    assert capture["json"]["content"][0]["type"] == "text/plain"


# ---------------------------------------------------------------------------
# SendGrid backend: arg/setting precedence
# ---------------------------------------------------------------------------

def test_sendgrid_explicit_from_addr_overrides_settings(monkeypatch):
    _set_sendgrid_env(monkeypatch, from_email="settings@example.com")
    capture: dict = {}
    cls = _make_client(status=202, capture=capture)
    _install_fake_httpx(monkeypatch, cls)

    from backend.common.email_backends.sendgrid import send_email_via_sendgrid
    send_email_via_sendgrid(
        "a@b.com", "s", "b",
        from_addr="explicit@example.com", from_name="Explicit",
    )
    assert capture["json"]["from"] == {"email": "explicit@example.com", "name": "Explicit"}


def test_sendgrid_explicit_api_key_overrides_settings(monkeypatch):
    _set_sendgrid_env(monkeypatch, api_key="SG.settings_key")
    capture: dict = {}
    cls = _make_client(status=202, capture=capture)
    _install_fake_httpx(monkeypatch, cls)

    from backend.common.email_backends.sendgrid import send_email_via_sendgrid
    send_email_via_sendgrid("a@b.com", "s", "b", api_key="SG.explicit_key")
    assert capture["headers"]["Authorization"] == "Bearer SG.explicit_key"


def test_sendgrid_explicit_base_url_overrides_settings(monkeypatch):
    """EU subusers may need to override to https://api.eu.sendgrid.com."""
    _set_sendgrid_env(monkeypatch, base_url="https://api.sendgrid.com")
    capture: dict = {}
    cls = _make_client(status=202, capture=capture)
    _install_fake_httpx(monkeypatch, cls)

    from backend.common.email_backends.sendgrid import send_email_via_sendgrid
    send_email_via_sendgrid(
        "a@b.com", "s", "b",
        base_url="https://api.eu.sendgrid.com",
    )
    assert capture["url"] == "https://api.eu.sendgrid.com/v3/mail/send"


def test_sendgrid_eu_base_url_via_settings(monkeypatch):
    _set_sendgrid_env(monkeypatch, base_url="https://api.eu.sendgrid.com")
    capture: dict = {}
    cls = _make_client(status=202, capture=capture)
    _install_fake_httpx(monkeypatch, cls)

    from backend.common.email_backends.sendgrid import send_email_via_sendgrid
    send_email_via_sendgrid("a@b.com", "s", "b")
    assert capture["url"] == "https://api.eu.sendgrid.com/v3/mail/send"


def test_sendgrid_from_name_omitted_when_blank(monkeypatch):
    _set_sendgrid_env(monkeypatch, from_name="   ")  # blank
    capture: dict = {}
    cls = _make_client(status=202, capture=capture)
    _install_fake_httpx(monkeypatch, cls)

    from backend.common.email_backends.sendgrid import send_email_via_sendgrid
    send_email_via_sendgrid("a@b.com", "s", "b", from_name=" ")
    # from object should NOT include 'name' when both arg and setting are blank
    assert capture["json"]["from"] == {"email": "samus@example.com"}


# ---------------------------------------------------------------------------
# SendGrid backend: validation / failure modes
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("kwargs,missing", [
    ({"to": "", "subject": "s", "body": "b"}, "to"),
    ({"to": "   ", "subject": "s", "body": "b"}, "to"),
    ({"to": "a@b.com", "subject": "", "body": "b"}, "subject"),
    ({"to": "a@b.com", "subject": "s", "body": ""}, "body"),
    ({"to": "a@b.com", "subject": "s", "body": "   "}, "body"),
])
def test_sendgrid_validates_required_strings(monkeypatch, kwargs, missing):
    _set_sendgrid_env(monkeypatch)
    from backend.common.email_backends.sendgrid import send_email_via_sendgrid
    with pytest.raises(ValueError) as ei:
        send_email_via_sendgrid(**kwargs)
    assert missing in str(ei.value)


def test_sendgrid_raises_when_no_api_key_anywhere(monkeypatch):
    _set_sendgrid_env(monkeypatch, api_key="")
    from backend.common.email_backends.sendgrid import send_email_via_sendgrid
    with pytest.raises(ValueError) as ei:
        send_email_via_sendgrid("a@b.com", "s", "b")
    assert "api_key" in str(ei.value)


def test_sendgrid_raises_when_no_from_addr_anywhere(monkeypatch):
    _set_sendgrid_env(monkeypatch, from_email="")
    from backend.common.email_backends.sendgrid import send_email_via_sendgrid
    with pytest.raises(ValueError) as ei:
        send_email_via_sendgrid("a@b.com", "s", "b")
    assert "from_addr" in str(ei.value)


def test_sendgrid_4xx_with_errors_body_raises_with_parsed_message(monkeypatch):
    _set_sendgrid_env(monkeypatch)
    body = {"errors": [
        {"message": "The from address does not match a verified Sender Identity"},
        {"message": "second error"},
    ]}
    cls = _make_client(status=403, body=body)
    _install_fake_httpx(monkeypatch, cls)

    from backend.common.email_backends.sendgrid import (
        SendGridAdapterError, send_email_via_sendgrid,
    )
    with pytest.raises(SendGridAdapterError) as ei:
        send_email_via_sendgrid("a@b.com", "s", "b")
    msg = str(ei.value)
    assert "sendgrid_http_403" in msg
    assert "verified Sender" in msg
    assert "second error" in msg


def test_sendgrid_4xx_with_malformed_body_falls_back_to_text(monkeypatch):
    _set_sendgrid_env(monkeypatch)
    # body=None -> _Resp.json() raises ValueError, error parser falls through
    cls = _make_client(status=500, body=None)
    _install_fake_httpx(monkeypatch, cls)

    from backend.common.email_backends.sendgrid import (
        SendGridAdapterError, send_email_via_sendgrid,
    )
    with pytest.raises(SendGridAdapterError) as ei:
        send_email_via_sendgrid("a@b.com", "s", "b")
    assert "sendgrid_http_500" in str(ei.value)


def test_sendgrid_transport_error_wraps_to_adapter_error(monkeypatch):
    _set_sendgrid_env(monkeypatch)
    cls = _make_client(raise_exc=httpx.ConnectError("connection refused"))
    _install_fake_httpx(monkeypatch, cls)

    from backend.common.email_backends.sendgrid import (
        SendGridAdapterError, send_email_via_sendgrid,
    )
    with pytest.raises(SendGridAdapterError) as ei:
        send_email_via_sendgrid("a@b.com", "s", "b")
    assert "sendgrid_transport_error" in str(ei.value)


# ---------------------------------------------------------------------------
# Adapter selector (common/email_backend.py)
# ---------------------------------------------------------------------------

def test_selector_defaults_to_sendgrid_per_settings(monkeypatch):
    _set_sendgrid_env(monkeypatch, backend="sendgrid")
    captured: dict = {}

    def _fake(**kw: Any) -> dict[str, str]:
        captured.update(kw)
        return {"message_id": "sel_ok", "channel": "email", "to": kw["to"], "ts": "t"}

    import backend.common.email_backend as adapter
    monkeypatch.setattr(adapter, "send_email_via_sendgrid", _fake)

    out = adapter.send_email("a@b.com", "s", "b", html_body="<p>h</p>")
    assert out["message_id"] == "sel_ok"
    assert captured["to"] == "a@b.com"
    assert captured["subject"] == "s"
    assert captured["body"] == "b"
    assert captured["html_body"] == "<p>h</p>"


def test_selector_explicit_backend_overrides_settings(monkeypatch):
    """backend='sendgrid' arg wins even if settings.email_backend says otherwise."""
    _set_sendgrid_env(monkeypatch, backend="ses")  # settings say ses
    captured: dict = {}

    def _fake(**kw: Any) -> dict[str, str]:
        captured.update(kw)
        return {"message_id": "x", "channel": "email", "to": "a", "ts": "t"}

    import backend.common.email_backend as adapter
    monkeypatch.setattr(adapter, "send_email_via_sendgrid", _fake)

    adapter.send_email("a@b.com", "s", "b", backend="sendgrid")
    assert captured  # sendgrid was called despite settings


def test_selector_ses_branch_routes_to_ses_backend(monkeypatch):
    """The selector now routes ``backend="ses"`` to the common SES backend."""
    _set_sendgrid_env(monkeypatch)
    import backend.common.email_backend as adapter
    captured: dict = {}

    def _fake_ses(**kwargs):
        captured.update(kwargs)
        return {"message_id": "ses-1", "channel": "email", "to": kwargs["to"], "ts": "now"}

    monkeypatch.setattr(adapter, "send_email_via_ses", _fake_ses)
    out = adapter.send_email("a@b.com", "s", "b", backend="ses")
    assert out["message_id"] == "ses-1"
    assert captured["to"] == "a@b.com"


def test_selector_ses_branch_case_insensitive(monkeypatch):
    _set_sendgrid_env(monkeypatch)
    import backend.common.email_backend as adapter
    captured: dict = {}
    monkeypatch.setattr(
        adapter, "send_email_via_ses",
        lambda **kw: captured.update(kw) or {"message_id": "ses-2", "channel": "email",
                                             "to": kw["to"], "ts": "now"},
    )
    out = adapter.send_email("a@b.com", "s", "b", backend="SES")
    assert out["message_id"] == "ses-2"


def test_selector_ses_fail_closed_when_sender_unconfigured(monkeypatch):
    """No SES from-address configured -> ValueError, never a silent no-op."""
    _set_sendgrid_env(monkeypatch)
    monkeypatch.delenv("SES_FROM_EMAIL", raising=False)
    from backend.common.settings import reload_settings
    reload_settings()
    import backend.common.email_backend as adapter
    with pytest.raises(ValueError) as ei:
        adapter.send_email("a@b.com", "s", "b", backend="ses")
    assert "ses_from_email" in str(ei.value).lower() or "from_addr" in str(ei.value).lower()


def test_selector_unknown_backend_raises_value_error(monkeypatch):
    _set_sendgrid_env(monkeypatch)
    import backend.common.email_backend as adapter
    with pytest.raises(ValueError) as ei:
        adapter.send_email("a@b.com", "s", "b", backend="mailgun")
    assert "mailgun" in str(ei.value)


def test_selector_routes_sendgrid_when_settings_have_ses_but_arg_overrides(monkeypatch):
    """Belt-and-suspenders for explicit override."""
    _set_sendgrid_env(monkeypatch, backend="ses")
    capture: dict = {}
    cls = _make_client(status=202, headers={"X-Message-Id": "via_selector"}, capture=capture)
    _install_fake_httpx(monkeypatch, cls)

    from backend.common.email_backend import send_email
    out = send_email("a@b.com", "s", "b", backend="sendgrid")
    assert out["message_id"] == "via_selector"


def test_selector_emailbackenderror_catches_both_providers():
    """``except EmailBackendError`` catches a failure from either backend."""
    from backend.common.email_backend import EmailBackendError
    from backend.common.email_backends.sendgrid import SendGridAdapterError
    from backend.common.email_backends.ses import SesAdapterError
    # EmailBackendError is a tuple of provider error types -> usable in except.
    try:
        raise SendGridAdapterError("boom")
    except EmailBackendError:
        pass
    try:
        raise SesAdapterError("boom")
    except EmailBackendError:
        pass


# ---------------------------------------------------------------------------
# Reply-To header (Win #1 deliverability polish)
# ---------------------------------------------------------------------------

def test_sendgrid_omits_reply_to_when_neither_arg_nor_setting_present(monkeypatch):
    """Default: no Reply-To header -> mail clients fall back to From for replies."""
    _set_sendgrid_env(monkeypatch)
    monkeypatch.delenv("SENDGRID_REPLY_TO", raising=False)
    from backend.common.settings import reload_settings
    reload_settings()
    capture: dict = {}
    cls = _make_client(status=202, headers={"X-Message-Id": "x"}, capture=capture)
    _install_fake_httpx(monkeypatch, cls)

    from backend.common.email_backends.sendgrid import send_email_via_sendgrid
    send_email_via_sendgrid("lead@example.com", "s", "b")

    assert "reply_to" not in capture["json"], \
        "payload should not contain reply_to when neither arg nor setting is set"


def test_sendgrid_includes_reply_to_when_arg_provided(monkeypatch):
    """Explicit reply_to kwarg lands in payload.reply_to.email."""
    _set_sendgrid_env(monkeypatch)
    capture: dict = {}
    cls = _make_client(status=202, headers={"X-Message-Id": "x"}, capture=capture)
    _install_fake_httpx(monkeypatch, cls)

    from backend.common.email_backends.sendgrid import send_email_via_sendgrid
    send_email_via_sendgrid(
        "lead@example.com", "s", "b",
        reply_to="support@hustleforge.tech",
    )

    assert capture["json"]["reply_to"] == {"email": "support@hustleforge.tech"}


def test_sendgrid_includes_reply_to_from_settings_default(monkeypatch):
    """Empty/None reply_to arg -> falls back to settings.sendgrid_reply_to."""
    _set_sendgrid_env(monkeypatch)
    monkeypatch.setenv("SENDGRID_REPLY_TO", "samushustleforge@gmail.com")
    from backend.common.settings import reload_settings
    reload_settings()
    capture: dict = {}
    cls = _make_client(status=202, headers={"X-Message-Id": "x"}, capture=capture)
    _install_fake_httpx(monkeypatch, cls)

    from backend.common.email_backends.sendgrid import send_email_via_sendgrid
    send_email_via_sendgrid("lead@example.com", "s", "b")  # no explicit reply_to

    assert capture["json"]["reply_to"] == {"email": "samushustleforge@gmail.com"}


def test_email_backend_selector_passes_reply_to_through(monkeypatch):
    """The selector's reply_to kwarg reaches the SendGrid backend's payload."""
    _set_sendgrid_env(monkeypatch)
    capture: dict = {}
    cls = _make_client(status=202, headers={"X-Message-Id": "x"}, capture=capture)
    _install_fake_httpx(monkeypatch, cls)

    from backend.common.email_backend import send_email
    send_email(
        "lead@example.com", "s", "b",
        backend="sendgrid", reply_to="support@hustleforge.tech",
    )
    assert capture["json"]["reply_to"] == {"email": "support@hustleforge.tech"}


# ---------------------------------------------------------------------------
# Attachment envelope + adapter passthrough
# ---------------------------------------------------------------------------

class TestAttachmentEnvelope:
    """``_build_attachment_envelope`` is the choke point between callers
    (who pass bytes + a filename + an optional mime) and SendGrid's wire
    format (base64 content + filename + type + disposition).
    """

    def test_minimal_dict_produces_required_keys(self):
        from backend.common.email_backends.sendgrid import _build_attachment_envelope
        env = _build_attachment_envelope(
            {"filename": "x.txt", "content": b"hello"},
        )
        # Required SendGrid v3 keys.
        assert env["filename"] == "x.txt"
        assert env["disposition"] == "attachment"  # default
        assert env["type"] == "application/octet-stream"  # default mime
        # content must be base64-encoded ASCII
        import base64
        assert base64.b64decode(env["content"]) == b"hello"

    def test_explicit_mime_preserved(self):
        from backend.common.email_backends.sendgrid import _build_attachment_envelope
        env = _build_attachment_envelope({
            "filename": "list.txt",
            "content": b"line1\nline2",
            "mime_type": "text/plain",
        })
        assert env["type"] == "text/plain"

    def test_bytearray_content_accepted(self):
        from backend.common.email_backends.sendgrid import _build_attachment_envelope
        env = _build_attachment_envelope({
            "filename": "x.bin",
            "content": bytearray(b"\x01\x02\x03"),
        })
        import base64
        assert base64.b64decode(env["content"]) == b"\x01\x02\x03"

    def test_optional_content_id_propagated(self):
        from backend.common.email_backends.sendgrid import _build_attachment_envelope
        env = _build_attachment_envelope({
            "filename": "inline.png",
            "content": b"fakeimg",
            "mime_type": "image/png",
            "content_id": "logo",
        })
        assert env["content_id"] == "logo"

    def test_missing_filename_rejected(self):
        from backend.common.email_backends.sendgrid import _build_attachment_envelope
        with pytest.raises(ValueError, match="filename"):
            _build_attachment_envelope({"content": b"x"})

    def test_str_content_rejected(self):
        from backend.common.email_backends.sendgrid import _build_attachment_envelope
        with pytest.raises(ValueError, match="bytes"):
            _build_attachment_envelope({"filename": "x", "content": "not bytes"})


class TestSendgridAttachmentsPayload:
    """Verify the ``attachments`` kwarg makes it into the POST JSON payload
    as a properly-formed list of envelopes, and that absence means no
    ``attachments`` key in the payload (don't send a noisy empty array).
    """

    def test_attachment_included_in_payload(self, monkeypatch):
        _set_sendgrid_env(monkeypatch)
        capture: dict = {}
        cls = _make_client(status=202, headers={"X-Message-Id": "abc"}, capture=capture)
        _install_fake_httpx(monkeypatch, cls)

        from backend.common.email_backends.sendgrid import send_email_via_sendgrid
        send_email_via_sendgrid(
            to="alex@example.com",
            subject="s",
            body="b",
            attachments=[
                {"filename": "calls.txt", "content": b"a,b,c", "mime_type": "text/plain"},
            ],
        )

        payload = capture["json"]
        assert "attachments" in payload
        assert len(payload["attachments"]) == 1
        att = payload["attachments"][0]
        assert att["filename"] == "calls.txt"
        assert att["type"] == "text/plain"
        assert att["disposition"] == "attachment"
        import base64
        assert base64.b64decode(att["content"]) == b"a,b,c"

    def test_no_attachments_kwarg_means_no_payload_key(self, monkeypatch):
        _set_sendgrid_env(monkeypatch)
        capture: dict = {}
        cls = _make_client(status=202, headers={"X-Message-Id": "abc"}, capture=capture)
        _install_fake_httpx(monkeypatch, cls)

        from backend.common.email_backends.sendgrid import send_email_via_sendgrid
        send_email_via_sendgrid(to="alex@example.com", subject="s", body="b")
        assert "attachments" not in capture["json"]

    def test_empty_attachments_list_is_skipped(self, monkeypatch):
        _set_sendgrid_env(monkeypatch)
        capture: dict = {}
        cls = _make_client(status=202, headers={"X-Message-Id": "abc"}, capture=capture)
        _install_fake_httpx(monkeypatch, cls)

        from backend.common.email_backends.sendgrid import send_email_via_sendgrid
        send_email_via_sendgrid(
            to="alex@example.com", subject="s", body="b", attachments=[],
        )
        # Empty list is falsy in Python; the adapter should NOT add an empty
        # attachments array (SendGrid accepts it but it's noisier than no key).
        assert "attachments" not in capture["json"]

    def test_multiple_attachments_preserve_order(self, monkeypatch):
        _set_sendgrid_env(monkeypatch)
        capture: dict = {}
        cls = _make_client(status=202, headers={"X-Message-Id": "abc"}, capture=capture)
        _install_fake_httpx(monkeypatch, cls)

        from backend.common.email_backends.sendgrid import send_email_via_sendgrid
        send_email_via_sendgrid(
            to="alex@example.com", subject="s", body="b",
            attachments=[
                {"filename": "a.txt", "content": b"AAA"},
                {"filename": "b.txt", "content": b"BBB"},
            ],
        )
        attachments = capture["json"]["attachments"]
        assert [a["filename"] for a in attachments] == ["a.txt", "b.txt"]


class TestSelectorAttachmentsPassthrough:
    """The ``send_email`` selector must thread attachments through to the
    underlying adapter unchanged."""

    def test_selector_passes_attachments_to_sendgrid(self, monkeypatch):
        _set_sendgrid_env(monkeypatch)
        capture: dict = {}
        cls = _make_client(status=202, headers={"X-Message-Id": "abc"}, capture=capture)
        _install_fake_httpx(monkeypatch, cls)

        from backend.common.email_backend import send_email
        send_email(
            "alex@example.com", "s", "b",
            backend="sendgrid",
            attachments=[{"filename": "x.txt", "content": b"data"}],
        )
        payload = capture["json"]
        assert payload["attachments"][0]["filename"] == "x.txt"