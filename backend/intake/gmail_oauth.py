"""One-time Gmail OAuth consent flow — loopback redirect, no external libs.

Google's recommended pattern for installed/desktop apps:

  1. Spin up an HTTP server on http://127.0.0.1:<port>/oauth2/callback
  2. Open the user's browser at the consent URL with redirect_uri =
     that loopback URL
  3. The user authorizes in their browser
  4. Google redirects back to our loopback with ?code=<auth_code>
  5. We exchange the code for refresh_token + access_token at /token
  6. Persist the tokens to gmail_oauth_token_path

The refresh_token is long-lived (until the user revokes consent or the
OAuth client is deleted). All subsequent drain passes just refresh
access tokens via :func:`backend.intake.gmail_api_client.refresh_access_token`.

Run from a console with stdin attached so the user can see the consent
URL if their browser fails to launch automatically (SSH / WSL contexts).
"""
from __future__ import annotations

import argparse
import http.server
import logging
import secrets
import socket
import sys
import threading
import time
import urllib.parse
import webbrowser
from pathlib import Path
from typing import Any

import httpx

from backend.common.config import get_settings

from .gmail_api_client import GmailOauthToken


_LOG = logging.getLogger("samus.intake.gmail_oauth")

_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN_URL = "https://oauth2.googleapis.com/token"
# Multiple scopes joined by space (Google's OAuth 2 syntax). gmail.modify
# = read + send + label modify; calendar.events = read/write events on the
# authenticated user's calendars only. Both are needed so the intake
# poller can project detected calendar events into samushustleforge@'s
# calendar for operator review.
_SCOPE = (
    "https://www.googleapis.com/auth/gmail.modify "
    "https://www.googleapis.com/auth/calendar.events"
)
_LOOPBACK_HOST = "127.0.0.1"


# ---------------------------------------------------------------------------
# Tiny one-shot HTTP server that captures ?code= from the redirect
# ---------------------------------------------------------------------------

class _CallbackServer:
    """Bind a free loopback port; serve exactly one /oauth2/callback request.

    Captures the ``code`` query param into ``self.received_code``. Returns
    a friendly HTML body to the browser so the user knows it worked.
    Times out after the deadline so we don't hang forever if the user
    closes the tab.
    """

    def __init__(self, *, expected_state: str, timeout_seconds: int = 300) -> None:
        self._expected_state = expected_state
        self._timeout = timeout_seconds
        self.received_code: str = ""
        self.received_error: str = ""
        self.port: int = 0
        self._server: http.server.HTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._done = threading.Event()

    def __enter__(self) -> "_CallbackServer":
        # Pick a free port by binding port 0, then read it back.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind((_LOOPBACK_HOST, 0))
            self.port = s.getsockname()[1]

        outer = self  # captured by the handler

        class _Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_a: Any, **_kw: Any) -> None:
                return  # silence default access log

            def do_GET(self) -> None:  # noqa: N802 — stdlib API name
                parsed = urllib.parse.urlparse(self.path)
                if parsed.path != "/oauth2/callback":
                    self.send_response(404)
                    self.end_headers()
                    return
                qs = urllib.parse.parse_qs(parsed.query)
                state = (qs.get("state") or [""])[0]
                if state != outer._expected_state:
                    outer.received_error = "state_mismatch"
                    self._reply(400, "State mismatch — request was not initiated by this poller.")
                    outer._done.set()
                    return
                err = (qs.get("error") or [""])[0]
                if err:
                    outer.received_error = err
                    self._reply(400, f"OAuth consent returned error: {err}")
                    outer._done.set()
                    return
                code = (qs.get("code") or [""])[0]
                if not code:
                    outer.received_error = "missing_code"
                    self._reply(400, "OAuth response missing 'code' parameter.")
                    outer._done.set()
                    return
                outer.received_code = code
                self._reply(
                    200,
                    "Authorization received. You can close this browser tab and "
                    "return to the console.",
                )
                outer._done.set()

            def _reply(self, status: int, message: str) -> None:
                body = (
                    "<!doctype html><html><head><title>Samus Gmail OAuth</title>"
                    "<style>body{font-family:system-ui;margin:4rem;color:#222}</style>"
                    "</head><body><h1>Samus Gmail OAuth</h1>"
                    f"<p>{message}</p></body></html>"
                ).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self._server = http.server.HTTPServer((_LOOPBACK_HOST, self.port), _Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="gmail_oauth_callback",
            daemon=True,
        )
        self._thread.start()
        return self

    def wait_for_code(self) -> None:
        """Block until the callback fires or the timeout elapses."""
        if not self._done.wait(timeout=self._timeout):
            raise TimeoutError(
                f"gmail_oauth_timeout: no callback received within "
                f"{self._timeout}s — consent flow abandoned?",
            )

    @property
    def redirect_uri(self) -> str:
        return f"http://{_LOOPBACK_HOST}:{self.port}/oauth2/callback"

    def __exit__(self, *args: Any) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None


# ---------------------------------------------------------------------------
# Code -> token exchange
# ---------------------------------------------------------------------------

def _exchange_code_for_tokens(
    *,
    client_id: str,
    client_secret: str,
    code: str,
    redirect_uri: str,
    timeout: float = 20.0,
) -> dict[str, Any]:
    """POST the auth_code back to /token to receive refresh + access tokens.

    Returns Google's raw JSON; the caller persists what it needs.
    """
    with httpx.Client(timeout=timeout) as client:
        resp = client.post(
            _TOKEN_URL,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "code": code,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
        )
    if resp.status_code >= 400:
        try:
            err = resp.json()
        except ValueError:
            err = {"error_description": resp.text[:200]}
        raise RuntimeError(
            f"oauth_code_exchange_failed: "
            f"http_{resp.status_code} {err.get('error')}: "
            f"{err.get('error_description')}",
        )
    body = resp.json()
    if not body.get("refresh_token"):
        raise RuntimeError(
            "oauth_no_refresh_token: Google did not return a refresh_token. "
            "This happens when the user has already granted consent and "
            "Google returns only an access_token. Revoke prior consent at "
            "https://myaccount.google.com/permissions then re-run.",
        )
    return body


# ---------------------------------------------------------------------------
# Top-level: run the consent flow + persist the token JSON
# ---------------------------------------------------------------------------

def run_consent_flow(
    *,
    client_id: str | None = None,
    client_secret: str | None = None,
    token_path: Path | None = None,
    open_browser: bool = True,
    timeout_seconds: int = 300,
) -> Path:
    """Walk the user through one OAuth consent and persist the result.

    Returns the path the token was written to. Designed to be called from
    a console (interactive) — prints the consent URL to stdout in case
    the auto-browser launch fails (SSH, WSL without browser, etc).
    """
    settings = get_settings()
    cid = client_id or settings.gmail_oauth_client_id
    csecret = client_secret or settings.gmail_oauth_client_secret
    tpath = token_path or Path(settings.gmail_oauth_token_path)
    inbox_email = (settings.gmail_inbox_email or "").strip()

    if not cid or not csecret:
        raise SystemExit(
            "gmail_oauth: client_id / client_secret unset. Pull them from "
            "DPAPI via Authorize-Gmail.ps1 or set "
            "SAMUS_GMAIL_OAUTH_CLIENT_ID + SAMUS_GMAIL_OAUTH_CLIENT_SECRET.",
        )

    state = secrets.token_urlsafe(24)

    with _CallbackServer(
        expected_state=state, timeout_seconds=timeout_seconds,
    ) as server:
        params = {
            "client_id": cid,
            "redirect_uri": server.redirect_uri,
            "response_type": "code",
            "scope": _SCOPE,
            "state": state,
            "access_type": "offline",        # ask for a refresh_token
            "prompt": "consent select_account",  # force the consent screen
                                                  # AND the account chooser so
                                                  # Google doesn't silently use
                                                  # whatever's currently signed
                                                  # in (the inbox account is
                                                  # usually NOT the user's
                                                  # daily-driver Google account)
            "include_granted_scopes": "true",
        }
        if inbox_email:
            # Steers Google to the right account on the consent screen.
            # Without this Google defaults to whichever Google account the
            # current browser session is signed into, which is rarely the
            # Samus inbox account.
            params["login_hint"] = inbox_email
        auth_url = f"{_AUTH_URL}?{urllib.parse.urlencode(params)}"
        print(
            "\nSamus Gmail OAuth consent\n"
            "-------------------------\n"
            f"Scope:         {_SCOPE}\n"
            f"Redirect URI:  {server.redirect_uri}\n"
            f"Token path:    {tpath}\n"
            "\nOpen this URL in a browser if it doesn't open automatically:\n"
            f"\n  {auth_url}\n",
        )
        if open_browser:
            try:
                webbrowser.open(auth_url)
            except Exception as exc:  # noqa: BLE001 — paste-URL fallback covers it
                _LOG.warning("webbrowser.open failed: %s", exc)

        print(
            f"Waiting up to {timeout_seconds}s for the consent callback... "
            "(complete the flow in your browser)\n",
        )
        try:
            server.wait_for_code()
        except TimeoutError as exc:
            raise SystemExit(str(exc))

        if server.received_error:
            raise SystemExit(
                f"gmail_oauth: consent failed: {server.received_error}",
            )

        body = _exchange_code_for_tokens(
            client_id=cid,
            client_secret=csecret,
            code=server.received_code,
            redirect_uri=server.redirect_uri,
        )

    token = GmailOauthToken(
        refresh_token=str(body["refresh_token"]),
        access_token=str(body.get("access_token") or ""),
        expires_at=int(time.time() + int(body.get("expires_in", 3600))),
        scope=str(body.get("scope") or _SCOPE),
    )
    token.dump(tpath)
    print(f"\nOK — refresh token saved to {tpath}.")
    print(
        "Next: run scripts/Poll-Inbox.ps1 (or "
        "`python -m backend.intake.gmail_poller`) to drain the inbox.\n",
    )
    return tpath


def main() -> int:
    parser = argparse.ArgumentParser(description="Samus Gmail OAuth consent setup")
    parser.add_argument(
        "--no-open-browser", action="store_true",
        help="Don't auto-launch the browser; just print the URL.",
    )
    parser.add_argument(
        "--timeout", type=int, default=300,
        help="Seconds to wait for the consent callback (default 300).",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        run_consent_flow(
            open_browser=not args.no_open_browser,
            timeout_seconds=args.timeout,
        )
    except SystemExit as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
