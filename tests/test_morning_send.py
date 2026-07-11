"""Tests for backend.morning_send — orchestration of email + Discord channels.

Network is fully mocked: the SendGrid adapter is patched at the morning_send
module level, and httpx.Client is patched for the Discord path. No live
calls; tests pass in CI / on a clean checkout.
"""

from __future__ import annotations

import json
from datetime import date
from unittest.mock import MagicMock, patch

import pytest

from backend import morning_send


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

_FAKE_BRIEFING = """\
===========================================================================
SAMUS MORNING BRIEFING -- Saturday, May 16, 2026
===========================================================================

CRITICAL -- TODAY (3 actions, 1 critical gaps, 2 overdue)

  ! OVERDUE -- address first
  X  2026-05-15  Pay Yuba Court — $220

CASH
  Available (Stripe):   $    0.00
  Days of runway:         0.0 days
  Cash distress flag:   CRITICAL

DEBT PORTFOLIO
  T1 legal exposure    $  845.43  (5 debts)

OPEN INFO GAPS  (4 total)
===========================================================================
"""


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Strip channel + SendGrid env vars so each test starts from zero."""
    for k in (
        "SAMUS_BRIEF_EMAIL_TO",
        "SAMUS_BRIEF_DISCORD_WEBHOOK",
        "SAMUS_BRIEF_TELEGRAM_TOKEN",
        "SAMUS_BRIEF_TELEGRAM_CHAT_ID",
        "SAMUS_MORNING_NO_COLOR",
        "SENDGRID_API_KEY",
        "SENDGRID_FROM_EMAIL",
    ):
        monkeypatch.delenv(k, raising=False)


# --------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------


class TestExtractHeadline:
    def test_extracts_critical_line(self):
        assert morning_send._extract_headline(_FAKE_BRIEFING) == (
            "CRITICAL -- TODAY (3 actions, 1 critical gaps, 2 overdue)"
        )

    def test_fallback_when_no_critical_line(self):
        text = "SOMETHING ELSE\n\nNo critical here"
        assert morning_send._extract_headline(text) == morning_send._DISCORD_HEADLINE_FALLBACK


class TestHtmlEscape:
    def test_escapes_lt_gt_amp(self):
        assert morning_send._html_escape("<x> & </x>") == "&lt;x&gt; &amp; &lt;/x&gt;"


class TestSubject:
    def test_subject_format(self):
        s = morning_send._build_subject(date(2026, 5, 16))
        assert s == "Samus morning brief — Sat May 16, 2026"


# --------------------------------------------------------------------------
# Email channel
# --------------------------------------------------------------------------


class TestSendEmail:
    def test_calls_send_email_with_subject_and_html(self):
        sent: dict = {}

        def fake_send_email(to, subject, body, *, html_body=None, **kwargs):
            sent["to"] = to
            sent["subject"] = subject
            sent["body"] = body
            sent["html_body"] = html_body
            return {"message_id": "abc123", "channel": "email", "to": to, "ts": "now"}

        with patch.object(morning_send, "send_email", side_effect=fake_send_email):
            result = morning_send._send_email(
                _FAKE_BRIEFING,
                "alex@example.com",
                date(2026, 5, 16),
            )

        assert sent["to"] == "alex@example.com"
        assert sent["subject"] == "Samus morning brief — Sat May 16, 2026"
        assert sent["body"] == _FAKE_BRIEFING
        # HTML body should escape & wrap in <pre>.
        assert "<pre" in sent["html_body"]
        assert "&lt;" not in sent["body"]  # plain body unchanged
        assert result["message_id"] == "abc123"


# --------------------------------------------------------------------------
# Discord channel
# --------------------------------------------------------------------------


class TestSendDiscord:
    def _mk_resp(self, status=204, text=""):
        r = MagicMock()
        r.status_code = status
        r.text = text
        return r

    def test_posts_multipart_with_attachment(self):
        captured: dict = {}

        class _FakeClient:
            def __init__(self_inner, *a, **kw):
                pass

            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *exc):
                return False

            def post(self_inner, url, data=None, files=None):
                captured["url"] = url
                captured["data"] = data
                captured["files"] = files
                return TestSendDiscord()._mk_resp(204)

        with patch.object(morning_send.httpx, "Client", _FakeClient):
            result = morning_send._send_discord(
                _FAKE_BRIEFING,
                "https://discord.com/api/webhooks/123/abctoken",
                date(2026, 5, 16),
            )

        assert captured["url"] == "https://discord.com/api/webhooks/123/abctoken"
        assert "payload_json" in captured["data"]
        # File field name is files[0] per Discord's documented convention.
        assert "files[0]" in captured["files"]
        fname, contents, mime = captured["files"]["files[0]"]
        assert fname == "morning_brief_2026-05-16.txt"
        assert contents == _FAKE_BRIEFING.encode("utf-8")
        assert mime == "text/plain"
        assert result["status_code"] == "204"
        assert result["to"] == "123"  # webhook id

    def test_rejects_non_http_url(self):
        with pytest.raises(ValueError, match="http"):
            morning_send._send_discord(
                _FAKE_BRIEFING,
                "not-a-url",
                date(2026, 5, 16),
            )

    def test_raises_on_4xx(self):
        class _FakeClient:
            def __init__(self_inner, *a, **kw):
                pass

            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *exc):
                return False

            def post(self_inner, url, data=None, files=None):
                r = MagicMock()
                r.status_code = 401
                r.text = '{"message": "Invalid Webhook Token"}'
                return r

        with patch.object(morning_send.httpx, "Client", _FakeClient):
            with pytest.raises(RuntimeError, match="discord_http_401"):
                morning_send._send_discord(
                    _FAKE_BRIEFING,
                    "https://discord.com/api/webhooks/123/abctoken",
                    date(2026, 5, 16),
                )


# --------------------------------------------------------------------------
# Telegram channel
# --------------------------------------------------------------------------


class TestSendTelegram:
    def test_posts_senddocument_with_attachment(self):
        captured: dict = {}

        class _FakeClient:
            def __init__(self_inner, *a, **kw):
                pass

            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *exc):
                return False

            def post(self_inner, url, data=None, files=None):
                captured["url"] = url
                captured["data"] = data
                captured["files"] = files
                r = MagicMock()
                r.status_code = 200
                r.text = ""
                return r

        with patch.object(morning_send.httpx, "Client", _FakeClient):
            result = morning_send._send_telegram(
                _FAKE_BRIEFING,
                "botTOKEN",
                "-100123",
                date(2026, 5, 16),
            )

        assert captured["url"] == "https://api.telegram.org/botbotTOKEN/sendDocument"
        assert captured["data"]["chat_id"] == "-100123"
        assert "caption" in captured["data"]
        fname, contents, mime = captured["files"]["document"]
        assert fname == "morning_brief_2026-05-16.txt"
        assert contents == _FAKE_BRIEFING.encode("utf-8")
        assert result["status_code"] == "200"
        assert result["to"] == "-100123"

    def test_raises_on_4xx(self):
        class _FakeClient:
            def __init__(self_inner, *a, **kw):
                pass

            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *exc):
                return False

            def post(self_inner, url, data=None, files=None):
                r = MagicMock()
                r.status_code = 401
                r.text = '{"description": "Unauthorized"}'
                return r

        with patch.object(morning_send.httpx, "Client", _FakeClient):
            with pytest.raises(RuntimeError, match="telegram_http_401"):
                morning_send._send_telegram(
                    _FAKE_BRIEFING,
                    "botTOKEN",
                    "-100123",
                    date(2026, 5, 16),
                )


# --------------------------------------------------------------------------
# Orchestration (main) — Telegram channel selection
# --------------------------------------------------------------------------


class TestMainTelegram:
    def test_telegram_channel_succeeds(self, monkeypatch, capsys):
        monkeypatch.setenv("SAMUS_BRIEF_TELEGRAM_TOKEN", "botTOKEN")
        monkeypatch.setenv("SAMUS_BRIEF_TELEGRAM_CHAT_ID", "-100123")

        with (
            patch.object(morning_send, "_build_briefing", return_value=_FAKE_BRIEFING),
            patch.object(
                morning_send,
                "_send_telegram",
                return_value={"channel": "telegram", "status_code": "200", "to": "-100123"},
            ),
        ):
            rc = morning_send.main([])

        out = capsys.readouterr()
        assert rc == 0
        assert "telegram -> chat -100123" in out.out
        assert "FAIL:" not in out.err

    def test_telegram_needs_both_token_and_chat_id(self, monkeypatch, capsys):
        # Token without chat id -> telegram not selected -> graceful skip.
        monkeypatch.setenv("SAMUS_BRIEF_TELEGRAM_TOKEN", "botTOKEN")
        rc = morning_send.main([])
        assert rc == 0
        out = capsys.readouterr()
        assert "telegram" not in out.out

    def test_skips_gracefully_when_no_channels_configured(self, capsys):
        # autouse fixture has scrubbed env, so no channels are set.
        rc = morning_send.main([])
        assert rc == 0
        # No render attempt — neither OK nor FAIL line printed.
        out = capsys.readouterr()
        assert "OK:" not in out.out
        assert "FAIL:" not in out.err

    def test_both_channels_succeed(self, monkeypatch, capsys):
        monkeypatch.setenv("SAMUS_BRIEF_EMAIL_TO", "alex@example.com")
        monkeypatch.setenv(
            "SAMUS_BRIEF_DISCORD_WEBHOOK",
            "https://discord.com/api/webhooks/999/tok",
        )

        with (
            patch.object(morning_send, "_build_briefing", return_value=_FAKE_BRIEFING),
            patch.object(
                morning_send,
                "_send_email",
                return_value={"message_id": "m1", "channel": "email", "to": "x", "ts": "t"},
            ),
            patch.object(
                morning_send,
                "_send_discord",
                return_value={"channel": "discord", "status_code": "204", "to": "999"},
            ),
        ):
            rc = morning_send.main([])

        out = capsys.readouterr()
        assert rc == 0
        assert "email -> alex@example.com" in out.out
        assert "discord -> webhook 999" in out.out
        assert "FAIL:" not in out.err

    def test_one_failure_returns_1_other_still_attempted(self, monkeypatch, capsys):
        monkeypatch.setenv("SAMUS_BRIEF_EMAIL_TO", "alex@example.com")
        monkeypatch.setenv(
            "SAMUS_BRIEF_DISCORD_WEBHOOK",
            "https://discord.com/api/webhooks/999/tok",
        )

        with (
            patch.object(morning_send, "_build_briefing", return_value=_FAKE_BRIEFING),
            patch.object(
                morning_send,
                "_send_email",
                side_effect=morning_send.EmailBackendError("sendgrid_http_401: bad key"),
            ),
            patch.object(
                morning_send,
                "_send_discord",
                return_value={"channel": "discord", "status_code": "204", "to": "999"},
            ),
        ):
            rc = morning_send.main([])

        out = capsys.readouterr()
        assert rc == 1
        # Discord still attempted + succeeded
        assert "discord -> webhook 999" in out.out
        # Email failure surfaced
        assert "email -> alex@example.com" in out.err
        assert "sendgrid_http_401" in out.err

    def test_email_only(self, monkeypatch, capsys):
        monkeypatch.setenv("SAMUS_BRIEF_EMAIL_TO", "alex@example.com")
        # No SAMUS_BRIEF_DISCORD_WEBHOOK

        discord_called = {"yes": False}

        def fake_discord(*a, **kw):
            discord_called["yes"] = True
            return {"channel": "discord", "status_code": "204", "to": "999"}

        with (
            patch.object(morning_send, "_build_briefing", return_value=_FAKE_BRIEFING),
            patch.object(
                morning_send,
                "_send_email",
                return_value={"message_id": "m1", "channel": "email", "to": "x", "ts": "t"},
            ),
            patch.object(morning_send, "_send_discord", side_effect=fake_discord),
        ):
            rc = morning_send.main([])

        assert rc == 0
        assert discord_called["yes"] is False  # never reached


# --------------------------------------------------------------------------
# Call-list attachment (today's prospect call list -> email + Discord)
# --------------------------------------------------------------------------


class TestTodayCallList:
    """``_today_call_list`` looks up today's prospect call list under the
    artifact root. Missing file -> None; present -> (Path, bytes).
    """

    def test_returns_none_when_file_missing(self, monkeypatch, tmp_path):
        # Point storage root at an empty tmp dir; no daily_calls/ inside.
        monkeypatch.setenv("SAMUS_ARTIFACT_ROOT", str(tmp_path))
        result = morning_send._today_call_list(date(2026, 5, 16))
        assert result is None

    def test_returns_path_and_bytes_when_file_present(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SAMUS_ARTIFACT_ROOT", str(tmp_path))
        daily = tmp_path / "daily_calls"
        daily.mkdir(parents=True)
        target = daily / "morning_call_list_2026-05-16.txt"
        # Mirror production newline behavior (text_export.py writes with
        # newline="\n"); Windows would otherwise translate to CRLF.
        target.write_text(
            "# header\n1. lead one\n2. lead two\n",
            encoding="utf-8",
            newline="\n",
        )

        result = morning_send._today_call_list(date(2026, 5, 16))
        assert result is not None
        path, contents = result
        assert path.name == "morning_call_list_2026-05-16.txt"
        assert contents.startswith(b"# header\n")

    def test_returns_none_on_unexpected_error(self, monkeypatch):
        """If storage import or root() fails, helper swallows it -- the
        brief must still go out even if the artifact tree is unreadable."""

        def boom_root():
            raise RuntimeError("disk gone")

        # Patch storage.root to blow up; helper should return None.
        from backend.common import storage

        monkeypatch.setattr(storage, "root", boom_root)
        assert morning_send._today_call_list(date(2026, 5, 16)) is None


class TestSendEmailWithCallList:
    """``_send_email`` should attach the call list bytes when provided and
    omit the attachments arg entirely when call_list=None."""

    def test_call_list_present_attaches_to_send_email(self):
        from pathlib import Path

        sent: dict = {}

        def fake_send_email(to, subject, body, *, html_body=None, attachments=None, **kw):
            sent["attachments"] = attachments
            return {"message_id": "m1", "channel": "email", "to": to, "ts": "t"}

        with patch.object(morning_send, "send_email", side_effect=fake_send_email):
            morning_send._send_email(
                _FAKE_BRIEFING,
                "alex@example.com",
                date(2026, 5, 16),
                call_list=(Path("morning_call_list_2026-05-16.txt"), b"call list body"),
            )

        assert sent["attachments"] is not None
        assert len(sent["attachments"]) == 1
        att = sent["attachments"][0]
        assert att["filename"] == "morning_call_list_2026-05-16.txt"
        assert att["content"] == b"call list body"
        assert att["mime_type"] == "text/plain"

    def test_call_list_absent_passes_attachments_none(self):
        sent: dict = {}

        def fake_send_email(to, subject, body, *, html_body=None, attachments=None, **kw):
            sent["attachments"] = attachments
            return {"message_id": "m1", "channel": "email", "to": to, "ts": "t"}

        with patch.object(morning_send, "send_email", side_effect=fake_send_email):
            morning_send._send_email(
                _FAKE_BRIEFING,
                "alex@example.com",
                date(2026, 5, 16),
            )

        # Adapter prefers None over [] when no attachments to send.
        assert sent["attachments"] is None


class TestSendDiscordWithCallList:
    """``_send_discord`` should attach the call list as files[1] and mention
    it in the content; omit both when call_list=None."""

    def _capturing_client_cls(self, captured: dict):
        class _FakeClient:
            def __init__(self_inner, *a, **kw):
                pass

            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *exc):
                return False

            def post(self_inner, url, data=None, files=None):
                captured["data"] = data
                captured["files"] = files
                r = MagicMock()
                r.status_code = 204
                r.text = ""
                return r

        return _FakeClient

    def test_call_list_present_attached_as_files_1(self):
        from pathlib import Path

        captured: dict = {}
        with patch.object(morning_send.httpx, "Client", self._capturing_client_cls(captured)):
            morning_send._send_discord(
                _FAKE_BRIEFING,
                "https://discord.com/api/webhooks/123/abctoken",
                date(2026, 5, 16),
                call_list=(Path("morning_call_list_2026-05-16.txt"), b"call list body"),
            )

        # files[0] is always the brief; files[1] is the call list.
        assert "files[0]" in captured["files"]
        assert "files[1]" in captured["files"]
        fname, content, mime = captured["files"]["files[1]"]
        assert fname == "morning_call_list_2026-05-16.txt"
        assert content == b"call list body"
        assert mime == "text/plain"
        # Discord content should announce both attachments.
        payload = json.loads(captured["data"]["payload_json"])
        assert "Call list attached" in payload["content"]

    def test_call_list_absent_only_files_0(self):
        captured: dict = {}
        with patch.object(morning_send.httpx, "Client", self._capturing_client_cls(captured)):
            morning_send._send_discord(
                _FAKE_BRIEFING,
                "https://discord.com/api/webhooks/123/abctoken",
                date(2026, 5, 16),
            )

        assert "files[0]" in captured["files"]
        assert "files[1]" not in captured["files"]
        payload = json.loads(captured["data"]["payload_json"])
        assert "Call list attached" not in payload["content"]


class TestMainPassesCallListToChannels:
    """``main`` should look up the call list once and pass it to both
    channels (avoid two filesystem hops). When missing, both channels
    receive None."""

    def test_main_passes_call_list_to_both_channels(self, monkeypatch):
        from pathlib import Path

        monkeypatch.setenv("SAMUS_BRIEF_EMAIL_TO", "alex@example.com")
        monkeypatch.setenv(
            "SAMUS_BRIEF_DISCORD_WEBHOOK", "https://discord.com/api/webhooks/999/tok"
        )

        fake_cl = (Path("morning_call_list_2026-05-16.txt"), b"AAA")

        captured: dict = {}

        def fake_email(briefing, to, today, *, call_list=None):
            captured["email_call_list"] = call_list
            return {"message_id": "m1", "channel": "email", "to": to, "ts": "t"}

        def fake_discord(briefing, url, today, *, call_list=None):
            captured["discord_call_list"] = call_list
            return {"channel": "discord", "status_code": "204", "to": "999"}

        with (
            patch.object(morning_send, "_build_briefing", return_value=_FAKE_BRIEFING),
            patch.object(morning_send, "_today_call_list", return_value=fake_cl),
            patch.object(morning_send, "_send_email", side_effect=fake_email),
            patch.object(morning_send, "_send_discord", side_effect=fake_discord),
        ):
            rc = morning_send.main([])

        assert rc == 0
        assert captured["email_call_list"] == fake_cl
        assert captured["discord_call_list"] == fake_cl

    def test_main_passes_none_to_both_when_call_list_missing(self, monkeypatch):
        monkeypatch.setenv("SAMUS_BRIEF_EMAIL_TO", "alex@example.com")
        monkeypatch.setenv(
            "SAMUS_BRIEF_DISCORD_WEBHOOK", "https://discord.com/api/webhooks/999/tok"
        )

        captured: dict = {}

        def fake_email(briefing, to, today, *, call_list=None):
            captured["email_call_list"] = call_list
            return {"message_id": "m1", "channel": "email", "to": to, "ts": "t"}

        def fake_discord(briefing, url, today, *, call_list=None):
            captured["discord_call_list"] = call_list
            return {"channel": "discord", "status_code": "204", "to": "999"}

        with (
            patch.object(morning_send, "_build_briefing", return_value=_FAKE_BRIEFING),
            patch.object(morning_send, "_today_call_list", return_value=None),
            patch.object(morning_send, "_send_email", side_effect=fake_email),
            patch.object(morning_send, "_send_discord", side_effect=fake_discord),
        ):
            rc = morning_send.main([])

        assert rc == 0
        assert captured["email_call_list"] is None
        assert captured["discord_call_list"] is None
