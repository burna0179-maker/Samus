"""Google Calendar API client — minimal insert/list surface.

Reuses the same OAuth token file the Gmail poller uses (see
:mod:`backend.intake.gmail_oauth`). The token now carries BOTH scopes:

  https://www.googleapis.com/auth/gmail.modify
  https://www.googleapis.com/auth/calendar.events

Any operator running the shipped scope-drift check
(``CalendarApiClient.check_scope_or_raise``) gets a clear
:class:`CalendarApiError` if the token pre-dates the calendar scope
expansion — the fix is to re-run ``scripts/Authorize-Gmail.ps1`` and
re-consent.

Surface:

* :meth:`insert_event` — create one event on ``calendarId=primary`` (Samus's
  own inbox calendar, which shows up in the Google Calendar web UI).
* :meth:`list_events` — small pagination-free list for verification/tests.

Fail-soft downstream: the poller catches :class:`CalendarApiError` so a
Calendar hiccup never breaks a drain.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

import httpx

from .gmail_api_client import (
    GmailOauthToken,
    refresh_access_token,
)

_LOG = logging.getLogger("samus.intake.calendar_api_client")

_API_BASE = "https://www.googleapis.com/calendar/v3"
_CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar.events"
_HTTP_TIMEOUT = 20.0


class CalendarApiError(Exception):
    """Raised on any Google Calendar API failure."""


class CalendarApiClient:
    """Thin insert/list client. One instance per drain pass is fine."""

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        token_path: Path,
        timeout: float = _HTTP_TIMEOUT,
    ) -> None:
        if not (client_id and client_secret):
            raise CalendarApiError(
                "CalendarApiClient requires client_id + client_secret",
            )
        self._client_id = client_id
        self._client_secret = client_secret
        self._token_path = token_path
        self._timeout = timeout
        self._token: GmailOauthToken | None = None

    def __enter__(self) -> "CalendarApiClient":
        self._token = GmailOauthToken.load(self._token_path)
        if self._token.is_access_token_expired():
            self._refresh_now()
        return self

    def __exit__(self, *args: Any) -> None:
        return None

    def check_scope_or_raise(self) -> None:
        """Verify the loaded token carries the calendar.events scope.

        The token file records the space-separated ``scope`` field Google
        returns from the token endpoint. If calendar isn't in there, the
        token predates the scope expansion — surface a clear error naming
        the fix so operators don't chase phantom 403s.
        """
        assert self._token is not None
        if _CALENDAR_SCOPE not in (self._token.scope or ""):
            raise CalendarApiError(
                "calendar_scope_missing: token lacks calendar.events. "
                "Re-run scripts/Authorize-Gmail.ps1 to re-consent with the "
                "expanded scope.",
            )

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
        if body.get("refresh_token"):
            self._token.refresh_token = str(body["refresh_token"])
        self._token.dump(self._token_path)

    def _request(
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
            raise CalendarApiError(f"calendar_transport_error: {exc}") from exc

        if resp.status_code == 401 and retry_on_401:
            self._refresh_now()
            return self._request(
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
            raise CalendarApiError(
                f"calendar_http_{resp.status_code}: {err.get('message') or resp.text[:200]}",
            )
        try:
            return resp.json() if resp.content else {}
        except ValueError as exc:
            raise CalendarApiError(f"calendar_invalid_json: {exc}") from exc

    # --- public surface --------------------------------------------------

    def insert_event(
        self,
        event: dict[str, Any],
        *,
        calendar_id: str = "primary",
    ) -> dict[str, Any]:
        """Create one event on ``calendar_id`` (defaults to Samus's primary).

        ``event`` follows Google's Events resource shape:
        https://developers.google.com/calendar/api/v3/reference/events

        Returns the created event dict (id, htmlLink, ...).
        """
        return self._request(
            "POST",
            f"/calendars/{calendar_id}/events",
            json_body=event,
        )

    def list_events(
        self,
        *,
        calendar_id: str = "primary",
        max_results: int = 50,
        q: str | None = None,
        private_extended_property: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """List events matching a free-text query and/or extended-property filter.

        ``q`` searches only summary/description/location/attendee — Google's
        Calendar API does NOT index ``extendedProperties`` for free-text
        search. When you need to look up an event by the tag we stamped on
        it (e.g. ``source_id`` for idempotent projections), pass
        ``private_extended_property=["source_id=<value>"]`` instead —
        that's the right primitive.

        Multiple ``privateExtendedProperty`` entries AND together.
        """
        params: dict[str, Any] = {"maxResults": max_results}
        if q:
            params["q"] = q
        if private_extended_property:
            # httpx accepts a list for a query key -> repeats the param.
            params["privateExtendedProperty"] = list(private_extended_property)
        body = self._request(
            "GET",
            f"/calendars/{calendar_id}/events",
            params=params,
        )
        return list(body.get("items") or [])

    def list_events_range(
        self,
        *,
        calendar_id: str = "primary",
        time_min: str,
        time_max: str,
        max_results: int = 100,
        single_events: bool = True,
    ) -> list[dict[str, Any]]:
        """List events between two RFC3339 timestamps.

        ``single_events=True`` (default) expands recurring events into
        instances so each occurrence is polled individually — matches the
        two-way sync's per-instance ledger.
        """
        params: dict[str, Any] = {
            "maxResults": max_results,
            "timeMin": time_min,
            "timeMax": time_max,
            "singleEvents": "true" if single_events else "false",
            "orderBy": "startTime" if single_events else "updated",
        }
        body = self._request(
            "GET",
            f"/calendars/{calendar_id}/events",
            params=params,
        )
        return list(body.get("items") or [])


__all__ = [
    "CalendarApiClient",
    "CalendarApiError",
]
