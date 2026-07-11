"""Gmail REST API client — pure httpx, no google-api-python-client dependency.

Three endpoints cover the inbox poller's full needs:

  - GET  https://gmail.googleapis.com/gmail/v1/users/me/messages
        ?q=is:unread&maxResults=N     — list unread message ids
  - GET  https://gmail.googleapis.com/gmail/v1/users/me/messages/{id}
        ?format=raw                    — fetch one message as base64url
                                         RFC822 bytes (which our existing
                                         parse_rfc822 already handles)
  - POST https://gmail.googleapis.com/gmail/v1/users/me/messages/{id}/modify
        body={"removeLabelIds":["UNREAD"]}
                                       — mark as read (Gmail-side dedup)

OAuth: gmail.modify scope (read + label modify; no send, no delete).
Access tokens last ~1 hour; this client transparently refreshes them
using the persisted refresh token. The persisted token JSON shape:

    {
      "refresh_token": "1//0e...",
      "access_token":  "ya29...",     # may be present + expired
      "expires_at":    1747426800,    # unix epoch seconds, UTC
      "scope":         "https://www.googleapis.com/auth/gmail.modify"
    }

The OAuth client_id + client_secret come from settings (DPAPI-stored on
the host). The refresh_token is set during the one-time consent flow in
gmail_oauth.py and rotated automatically when Google issues a new one.

Why pure httpx: mirrors the StripeClient pattern in finance/. The Google
SDK pulls ~10MB and adds a threading/retry machinery we don't need for
three GET/POSTs per drain pass.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx


_LOG = logging.getLogger("samus.intake.gmail_api_client")

_API_BASE = "https://gmail.googleapis.com/gmail/v1"
_TOKEN_URL = "https://oauth2.googleapis.com/token"
_HTTP_TIMEOUT = 20.0

# Buffer applied to access-token expiry — refresh slightly before Google's
# stated expires_at so a long-running drain doesn't fail mid-pass.
_REFRESH_LEEWAY_SECONDS = 60


class GmailApiError(Exception):
    """Raised when Gmail returns a non-2xx response or the network fails."""


# ---------------------------------------------------------------------------
# Persisted-token JSON
# ---------------------------------------------------------------------------


@dataclass
class GmailOauthToken:
    """The shape persisted at gmail_oauth_token_path."""

    refresh_token: str
    access_token: str = ""
    expires_at: int = 0  # unix epoch seconds
    scope: str = ""

    @classmethod
    def load(cls, path: Path) -> "GmailOauthToken":
        if not path.exists():
            raise GmailApiError(f"gmail_oauth_token_missing: {path}")
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or not raw.get("refresh_token"):
            raise GmailApiError(f"gmail_oauth_token_malformed: {path}")
        return cls(
            refresh_token=str(raw["refresh_token"]),
            access_token=str(raw.get("access_token") or ""),
            expires_at=int(raw.get("expires_at") or 0),
            scope=str(raw.get("scope") or ""),
        )

    def dump(self, path: Path) -> Path | None:
        """Write the token JSON back to ``path``.

        Returns the path on success, ``None`` if the filesystem is read-only
        (which is the normal case for Cloud Run secret-volume mounts:
        gmail-oauth-token mounts at /secrets/... read-only). In that case
        the in-memory token still works for the rest of the pass — only the
        on-disk cache of access_token/expires_at is lost, which costs a
        fresh refresh on the next pass but doesn't break correctness.
        Refresh-token rotation events would also be lost (rare; if it
        happens you'll see auth errors and need to re-mint via
        Authorize-Gmail.ps1 + re-upload to Secret Manager).
        """
        body = json.dumps(
            {
                "refresh_token": self.refresh_token,
                "access_token": self.access_token,
                "expires_at": int(self.expires_at),
                "scope": self.scope,
            },
            indent=2,
        )
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            # The file holds a long-lived Gmail refresh_token — create it
            # owner-only (0600) so other local users/processes cannot read it
            # (CWE-276). The 0o600 mode arg is honoured on POSIX; on Windows
            # os.open ignores it harmlessly (NTFS ACLs govern access there).
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(body)
        except OSError as exc:
            _LOG.info(
                "gmail oauth token persist skipped (read-only fs?): %s",
                exc,
            )
            return None
        return path

    def is_access_token_expired(self, *, now: float | None = None) -> bool:
        """True if we have no access token, or it expires within the leeway."""
        if not self.access_token:
            return True
        current = now if now is not None else time.time()
        return current + _REFRESH_LEEWAY_SECONDS >= self.expires_at


# ---------------------------------------------------------------------------
# Low-level OAuth refresh (POST /token)
# ---------------------------------------------------------------------------


def refresh_access_token(
    *,
    client_id: str,
    client_secret: str,
    refresh_token: str,
    timeout: float = _HTTP_TIMEOUT,
) -> dict[str, Any]:
    """Exchange a refresh_token for a fresh access_token.

    Returns Google's raw response body: {access_token, expires_in, scope,
    token_type, ...}. The caller persists what it needs into the token
    JSON via :meth:`GmailOauthToken.dump`.
    """
    if not (client_id and client_secret and refresh_token):
        raise GmailApiError(
            "refresh_access_token requires client_id, client_secret, refresh_token",
        )
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(
                _TOKEN_URL,
                data={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "refresh_token": refresh_token,
                    "grant_type": "refresh_token",
                },
            )
    except httpx.HTTPError as exc:
        raise GmailApiError(f"oauth_transport_error: {exc}") from exc
    if resp.status_code >= 400:
        # Google returns {error, error_description} on auth failures —
        # the refresh_token may have been revoked, the client may have been
        # deleted, etc. Re-running the one-time consent flow is the fix.
        try:
            err = resp.json()
        except ValueError:
            err = {"error_description": resp.text[:200]}
        raise GmailApiError(
            f"oauth_http_{resp.status_code}: {err.get('error')}: {err.get('error_description')}"
        )
    try:
        body = resp.json()
    except ValueError as exc:
        raise GmailApiError(f"oauth_invalid_json: {exc}") from exc
    if not body.get("access_token"):
        raise GmailApiError(f"oauth_no_access_token_in_response: {body}")
    return body


# ---------------------------------------------------------------------------
# Gmail API client (drop-in replacement for the IMAP client surface)
# ---------------------------------------------------------------------------


class GmailApiClient:
    """One-pass Gmail API client built around a persisted refresh token.

    Mirrors the (now-removed) GmailImapClient surface so :func:`drain_once`
    only had to swap the factory: ``__enter__`` ensures we have a valid
    access token, ``list_unread_message_ids`` + ``fetch_raw`` + ``mark_read``
    are the three methods the poller calls per message.

    No persistent state besides the token file; reasonable to construct
    one instance per drain pass and discard it.
    """

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        token_path: Path,
        user: str = "me",
        timeout: float = _HTTP_TIMEOUT,
    ) -> None:
        if not (client_id and client_secret):
            raise GmailApiError(
                "GmailApiClient requires client_id + client_secret",
            )
        self._client_id = client_id
        self._client_secret = client_secret
        self._token_path = token_path
        self._user = user
        self._timeout = timeout
        self._token: GmailOauthToken | None = None

    # --- context-manager surface (matches IMAP client) -------------------

    def __enter__(self) -> "GmailApiClient":
        self._token = GmailOauthToken.load(self._token_path)
        if self._token.is_access_token_expired():
            self._refresh_now()
        return self

    def __exit__(self, *args: Any) -> None:
        # Nothing to close — httpx clients are per-call short-lived. Keep
        # the method so callers can use ``with GmailApiClient(...) as c:``.
        return None

    # --- internal: refresh + persist ------------------------------------

    def _refresh_now(self) -> None:
        assert self._token is not None
        body = refresh_access_token(
            client_id=self._client_id,
            client_secret=self._client_secret,
            refresh_token=self._token.refresh_token,
            timeout=self._timeout,
        )
        self._token.access_token = str(body["access_token"])
        self._token.expires_at = int(time.time() + int(body.get("expires_in", 3600)))
        if body.get("scope"):
            self._token.scope = str(body["scope"])
        # Google sometimes rotates refresh_tokens on refresh; respect it.
        if body.get("refresh_token"):
            self._token.refresh_token = str(body["refresh_token"])
        self._token.dump(self._token_path)

    def _authed_get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._authed_request("GET", path, params=params)

    def _authed_post(self, path: str, json_body: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._authed_request("POST", path, json_body=json_body)

    def _authed_request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        retry_on_401: bool = True,
    ) -> dict[str, Any]:
        assert self._token is not None
        if self._token.is_access_token_expired():
            self._refresh_now()
        url = f"{_API_BASE}{path}"
        headers = {
            "Authorization": f"Bearer {self._token.access_token}",
            "Accept": "application/json",
        }
        try:
            with httpx.Client(timeout=self._timeout) as client:
                resp = client.request(
                    method,
                    url,
                    headers=headers,
                    params=params or {},
                    json=json_body,
                )
        except httpx.HTTPError as exc:
            raise GmailApiError(f"gmail_transport_error: {exc}") from exc

        if resp.status_code == 401 and retry_on_401:
            # Access token rejected; force a refresh + one retry.
            self._refresh_now()
            return self._authed_request(
                method,
                path,
                params=params,
                json_body=json_body,
                retry_on_401=False,
            )
        if resp.status_code >= 400:
            try:
                err = resp.json().get("error", {})
            except ValueError:
                err = {}
            raise GmailApiError(
                f"gmail_http_{resp.status_code}: {err.get('message') or resp.text[:200]}",
            )
        try:
            return resp.json() if resp.content else {}
        except ValueError as exc:
            raise GmailApiError(f"gmail_invalid_json: {exc}") from exc

    # --- public surface (mirrors the old IMAP client) -------------------

    def list_unread_message_ids(self, *, max_results: int = 25) -> list[str]:
        """Return up to ``max_results`` unread Gmail message ids (newest first).

        Uses the ``q=is:unread`` search query so the poller sees only what
        the operator hasn't acknowledged yet. Empty result is a normal
        steady-state — no errors raised.
        """
        body = self._authed_get(
            f"/users/{self._user}/messages",
            params={
                "q": "is:unread",
                "maxResults": max(1, min(500, int(max_results))),
            },
        )
        msgs = body.get("messages") or []
        return [str(m.get("id")) for m in msgs if m.get("id")]

    def search_message_ids(self, *, query: str, max_results: int = 100) -> list[str]:
        """Return up to ``max_results`` message ids matching an arbitrary ``q=`` search.

        General-purpose sibling of :meth:`list_unread_message_ids` (which
        hardcodes ``q=is:unread``) — callers that need Gmail's full search
        grammar (``from:``, ``subject:``, ``newer_than:``, boolean OR, ...)
        pass their query straight through. Read-only: a GET against the
        ``messages.list`` endpoint, same as the unread variant. Gmail caps a
        single page at 500; pass >500 and this clamps rather than paginating
        (callers needing more should narrow the query window instead).
        """
        body = self._authed_get(
            f"/users/{self._user}/messages",
            params={
                "q": query,
                "maxResults": max(1, min(500, int(max_results))),
            },
        )
        msgs = body.get("messages") or []
        return [str(m.get("id")) for m in msgs if m.get("id")]

    def fetch_raw(self, message_id: str) -> bytes:
        """Fetch one message's RFC822 raw bytes (Gmail returns it base64url).

        The ``format=raw`` flag asks Gmail to return the original message
        envelope verbatim — exactly what our :func:`parse_rfc822` already
        accepts, so the existing parser is unchanged.
        """
        body = self._authed_get(
            f"/users/{self._user}/messages/{message_id}",
            params={"format": "raw"},
        )
        raw = body.get("raw")
        if not raw:
            raise GmailApiError(
                f"gmail_no_raw_payload: id={message_id}",
            )
        # Gmail uses URL-safe base64 without padding; b64decode tolerates
        # the missing '=' when given a multiple-of-4 input, so pad manually.
        padded = raw + "=" * (-len(raw) % 4)
        try:
            return base64.urlsafe_b64decode(padded)
        except (ValueError, base64.binascii.Error) as exc:
            raise GmailApiError(
                f"gmail_raw_b64_decode_failed: id={message_id}: {exc}",
            ) from exc

    def mark_read(self, message_id: str) -> None:
        """Strip the UNREAD label so the next drain doesn't re-see this message."""
        self._authed_post(
            f"/users/{self._user}/messages/{message_id}/modify",
            json_body={"removeLabelIds": ["UNREAD"]},
        )

    def send_raw(self, raw_bytes: bytes) -> str:
        """Send an RFC822 message. Returns the sent message id.

        Gmail's ``messages/send`` accepts a base64url-encoded RFC822 envelope
        in the ``raw`` field. The envelope must include From/To/Subject at
        minimum; the ``forward.build_forward_mime`` helper composes one from
        the original message plus a category prefix.

        Requires ``gmail.send`` OAuth scope. Samus's OAuth uses
        ``gmail.modify`` which INCLUDES send capability (modify > send +
        readonly + labels), so no new consent flow is needed.
        """
        encoded = base64.urlsafe_b64encode(raw_bytes).decode("ascii").rstrip("=")
        body = self._authed_post(
            f"/users/{self._user}/messages/send",
            json_body={"raw": encoded},
        )
        return str(body.get("id") or "")

    def trash(self, message_id: str) -> None:
        """Move a message to Gmail's Trash (recoverable for 30 days).

        Used by the post-drain forwarder to keep the polled inbox tidy —
        every processed email lives in CRM (artifact + task) as the
        durable record; the Gmail copy is redundant after forwarding.
        """
        self._authed_post(
            f"/users/{self._user}/messages/{message_id}/trash",
            json_body={},
        )

    def modify_labels(
        self,
        message_id: str,
        *,
        add: list[str] | None = None,
        remove: list[str] | None = None,
    ) -> None:
        """Add/remove labels on a message (idempotent on both sides)."""
        payload: dict[str, list[str]] = {}
        if add:
            payload["addLabelIds"] = list(add)
        if remove:
            payload["removeLabelIds"] = list(remove)
        if not payload:
            return
        self._authed_post(
            f"/users/{self._user}/messages/{message_id}/modify",
            json_body=payload,
        )
