"""OAuth 2.0 helper functions for LinkedIn, Facebook, and Instagram social posting.

CODE-IN-PLACE, NOT WIRED INTO A ROUTE
-------------------------------------
These are *pure helper functions* only. There is deliberately **no FastAPI
callback route** here — the interactive browser-consent step is an operator
task run from a console later (mirroring the Gmail
``scripts/Authorize-Gmail.ps1`` + ``backend.intake.gmail_oauth`` pattern).

The flow an operator wires later:

  1. ``build_authorize_url(...)`` -> open in a browser, log in, consent.
  2. The provider redirects to ``redirect_uri?code=...&state=...``.
  3. ``exchange_code(...)`` -> swap the code for ``{access_token, ...}``.
  4. (LinkedIn) ``refresh_linkedin_token(...)`` -> renew before expiry.
  5. Operator persists the token into the secret store and sets
     ``LINKEDIN_ACCESS_TOKEN`` / ``FACEBOOK_PAGE_TOKEN`` for the adapter.

All secrets (client_id / client_secret / tokens) come from the caller
(env / secret store). NOTHING is hardcoded. Network calls are made with the
same ``httpx`` client the rest of the codebase uses, and they only fire when
the helper is actually invoked — importing this module performs no I/O.

TOS NOTE: LinkedIn forbids unapproved automation and bans accounts for it.
Obtaining + using a LinkedIn token for automated posting is an explicit
operator decision; see ``social_adapter`` module docstring.
"""
from __future__ import annotations

import logging
import urllib.parse
from typing import Any

import httpx

_LOG = logging.getLogger("samus.outreach.social_oauth")

# ---------------------------------------------------------------------------
# Provider endpoints (public, non-secret)
# ---------------------------------------------------------------------------

_LINKEDIN_AUTHORIZE_URL = "https://www.linkedin.com/oauth/v2/authorization"
_LINKEDIN_TOKEN_URL = "https://www.linkedin.com/oauth/v2/accessToken"
# w_member_social is the UGC-share scope; r_liteprofile identifies the author.
_LINKEDIN_DEFAULT_SCOPE = "w_member_social r_liteprofile"

_FACEBOOK_GRAPH_VERSION = "v19.0"
_FACEBOOK_AUTHORIZE_URL = f"https://www.facebook.com/{_FACEBOOK_GRAPH_VERSION}/dialog/oauth"
_FACEBOOK_TOKEN_URL = f"https://graph.facebook.com/{_FACEBOOK_GRAPH_VERSION}/oauth/access_token"
# pages_manage_posts is required to POST to a Page feed.
_FACEBOOK_DEFAULT_SCOPE = "pages_manage_posts,pages_read_engagement"

# Instagram uses Facebook Login but needs content-publish + pages scopes.
_INSTAGRAM_DEFAULT_SCOPE = "instagram_basic,instagram_content_publish,pages_show_list"

_DEFAULT_TIMEOUT = httpx.Timeout(connect=5.0, read=15.0, write=10.0, pool=5.0)


class SocialOauthError(RuntimeError):
    """Raised when a token exchange / refresh returns a non-2xx or malformed
    response. Carries a short reason string."""


# ---------------------------------------------------------------------------
# Step 1 — build the consent (authorize) URL
# ---------------------------------------------------------------------------


def build_authorize_url(
    platform: str,
    client_id: str,
    redirect_uri: str,
    state: str,
    *,
    scope: str | None = None,
) -> str:
    """Build the provider consent URL the operator opens in a browser.

    Pure string construction — no network, no secrets beyond the public
    ``client_id``. ``state`` is the caller-supplied CSRF token to verify on
    the redirect.
    """
    if platform not in ("linkedin", "facebook", "instagram"):
        raise ValueError(f"unsupported platform: {platform!r}")
    if not (client_id and redirect_uri and state):
        raise ValueError("client_id, redirect_uri and state are all required")

    if platform == "linkedin":
        base = _LINKEDIN_AUTHORIZE_URL
        params = {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "state": state,
            "scope": scope or _LINKEDIN_DEFAULT_SCOPE,
        }
    elif platform == "instagram":
        # Instagram uses Facebook Login with instagram-specific scopes.
        base = _FACEBOOK_AUTHORIZE_URL
        params = {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "state": state,
            "scope": scope or _INSTAGRAM_DEFAULT_SCOPE,
        }
    else:  # facebook
        base = _FACEBOOK_AUTHORIZE_URL
        params = {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "state": state,
            "scope": scope or _FACEBOOK_DEFAULT_SCOPE,
        }
    return f"{base}?{urllib.parse.urlencode(params)}"


# ---------------------------------------------------------------------------
# Step 2 — exchange the authorization code for tokens
# ---------------------------------------------------------------------------


def exchange_code(
    platform: str,
    code: str,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    *,
    timeout: float | httpx.Timeout | None = None,
) -> dict[str, Any]:
    """Swap an authorization ``code`` for tokens. Returns the provider JSON
    (typically ``{access_token, expires_in, ...}``; LinkedIn also returns
    ``refresh_token`` when the app is configured for it).

    Raises :class:`SocialOauthError` on non-2xx or malformed JSON. The caller
    persists whatever it needs.
    """
    if platform not in ("linkedin", "facebook", "instagram"):
        raise ValueError(f"unsupported platform: {platform!r}")
    if not (code and client_id and client_secret and redirect_uri):
        raise ValueError(
            "code, client_id, client_secret and redirect_uri are all required"
        )

    if platform == "linkedin":
        token_url = _LINKEDIN_TOKEN_URL
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
        }
    else:  # facebook and instagram share the same token endpoint
        token_url = _FACEBOOK_TOKEN_URL
        data = {
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
        }

    return _post_token(token_url, data, timeout=timeout, op="exchange_code")


# ---------------------------------------------------------------------------
# Step 3 — refresh a LinkedIn access token
# ---------------------------------------------------------------------------


def refresh_linkedin_token(
    refresh_token: str,
    client_id: str,
    client_secret: str,
    *,
    timeout: float | httpx.Timeout | None = None,
) -> dict[str, Any]:
    """Renew a LinkedIn access token using a stored ``refresh_token``.

    LinkedIn access tokens expire in ~60 days; refresh tokens in ~1 year.
    Returns the provider JSON (``{access_token, expires_in, ...}``). Raises
    :class:`SocialOauthError` on failure.

    Facebook page tokens (long-lived) are renewed via a different
    ``grant_type=fb_exchange_token`` flow against the Graph API rather than a
    refresh_token; that helper is intentionally omitted until needed.
    """
    if not (refresh_token and client_id and client_secret):
        raise ValueError(
            "refresh_token, client_id and client_secret are all required"
        )
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
        "client_secret": client_secret,
    }
    return _post_token(
        _LINKEDIN_TOKEN_URL, data, timeout=timeout, op="refresh_linkedin_token"
    )


# ---------------------------------------------------------------------------
# Shared token POST + response parse
# ---------------------------------------------------------------------------


def _post_token(
    token_url: str,
    data: dict[str, str],
    *,
    timeout: float | httpx.Timeout | None,
    op: str,
) -> dict[str, Any]:
    """POST a form-encoded token request and parse the JSON response.

    Raises :class:`SocialOauthError` on transport error, non-2xx, or a body
    that lacks an ``access_token``.
    """
    try:
        with httpx.Client(timeout=timeout or _DEFAULT_TIMEOUT) as client:
            resp = client.post(
                token_url,
                data=data,
                headers={"Accept": "application/json"},
            )
    except httpx.HTTPError as exc:
        raise SocialOauthError(f"{op}_transport_error:{type(exc).__name__}") from exc

    if resp.status_code >= 400:
        detail = ""
        try:
            body = resp.json()
            detail = str(body.get("error") or body.get("error_description") or "")[:120]
        except ValueError:
            detail = resp.text[:120]
        raise SocialOauthError(f"{op}_http_{resp.status_code}:{detail}")

    try:
        body = resp.json()
    except ValueError as exc:
        raise SocialOauthError(f"{op}_malformed_json") from exc

    if not isinstance(body, dict) or not body.get("access_token"):
        raise SocialOauthError(f"{op}_no_access_token")
    return body


# ---------------------------------------------------------------------------
# Instagram — discover business account from a page access token
# ---------------------------------------------------------------------------


def discover_instagram_account_id(
    page_access_token: str,
    *,
    timeout: float | httpx.Timeout | None = None,
) -> dict[str, str]:
    """Find the Instagram Business Account linked to a Facebook Page.

    After completing the OAuth flow for Instagram, call this once with the
    page access token to discover the ``instagram_business_account`` ID that
    the posting adapter needs.

    Returns ``{"page_id": ..., "page_name": ...,
    "instagram_business_account_id": ...}`` for the first page that has a
    linked Instagram business account.

    Raises :class:`SocialOauthError` if no linked account is found, or on
    transport / API errors.
    """
    if not page_access_token:
        raise ValueError("page_access_token is required")

    base = f"https://graph.facebook.com/{_FACEBOOK_GRAPH_VERSION}"
    effective_timeout = timeout or _DEFAULT_TIMEOUT

    # Step 1 — list all Facebook Pages the token has access to.
    try:
        with httpx.Client(timeout=effective_timeout) as client:
            pages_resp = client.get(
                f"{base}/me/accounts",
                params={"access_token": page_access_token},
            )
    except httpx.HTTPError as exc:
        raise SocialOauthError(
            f"discover_ig_pages_transport_error:{type(exc).__name__}"
        ) from exc

    if pages_resp.status_code >= 400:
        _raise_graph_error(pages_resp, "discover_ig_pages")

    try:
        pages_body = pages_resp.json()
    except ValueError as exc:
        raise SocialOauthError("discover_ig_pages_malformed_json") from exc

    pages = pages_body.get("data") or []
    if not pages:
        raise SocialOauthError("discover_ig_no_pages_found")

    # Step 2 — for each page, check for a linked instagram_business_account.
    try:
        with httpx.Client(timeout=effective_timeout) as client:
            for page in pages:
                page_id = page.get("id", "")
                page_name = page.get("name", "")
                ig_resp = client.get(
                    f"{base}/{page_id}",
                    params={
                        "fields": "instagram_business_account",
                        "access_token": page_access_token,
                    },
                )
                if ig_resp.status_code >= 400:
                    _LOG.warning(
                        "discover_ig: page %s returned %s, skipping",
                        page_id,
                        ig_resp.status_code,
                    )
                    continue
                try:
                    ig_body = ig_resp.json()
                except ValueError:
                    continue
                ig_account = ig_body.get("instagram_business_account", {})
                ig_id = ig_account.get("id") if isinstance(ig_account, dict) else None
                if ig_id:
                    return {
                        "page_id": page_id,
                        "page_name": page_name,
                        "instagram_business_account_id": ig_id,
                    }
    except httpx.HTTPError as exc:
        raise SocialOauthError(
            f"discover_ig_detail_transport_error:{type(exc).__name__}"
        ) from exc

    raise SocialOauthError("discover_ig_no_business_account_linked")


# ---------------------------------------------------------------------------
# Facebook / Instagram — exchange short-lived token for long-lived token
# ---------------------------------------------------------------------------


def exchange_for_long_lived_token(
    short_lived_token: str,
    client_id: str,
    client_secret: str,
    *,
    timeout: float | httpx.Timeout | None = None,
) -> dict[str, Any]:
    """Exchange a short-lived Facebook/Instagram token (~1 h) for a
    long-lived one (~60 days).

    Calls the ``fb_exchange_token`` grant type against the Graph API.
    Returns the provider JSON (``{access_token, token_type, expires_in}``).
    Raises :class:`SocialOauthError` on failure.
    """
    if not (short_lived_token and client_id and client_secret):
        raise ValueError(
            "short_lived_token, client_id and client_secret are all required"
        )

    url = f"https://graph.facebook.com/{_FACEBOOK_GRAPH_VERSION}/oauth/access_token"
    params = {
        "grant_type": "fb_exchange_token",
        "client_id": client_id,
        "client_secret": client_secret,
        "fb_exchange_token": short_lived_token,
    }

    try:
        with httpx.Client(timeout=timeout or _DEFAULT_TIMEOUT) as client:
            resp = client.get(url, params=params)
    except httpx.HTTPError as exc:
        raise SocialOauthError(
            f"exchange_long_lived_transport_error:{type(exc).__name__}"
        ) from exc

    if resp.status_code >= 400:
        _raise_graph_error(resp, "exchange_long_lived")

    try:
        body = resp.json()
    except ValueError as exc:
        raise SocialOauthError("exchange_long_lived_malformed_json") from exc

    if not isinstance(body, dict) or not body.get("access_token"):
        raise SocialOauthError("exchange_long_lived_no_access_token")
    return body


# ---------------------------------------------------------------------------
# Internal — Graph API error extraction
# ---------------------------------------------------------------------------


def _raise_graph_error(resp: httpx.Response, op: str) -> None:
    """Extract an error message from a Facebook Graph API response and raise
    :class:`SocialOauthError`."""
    detail = ""
    try:
        body = resp.json()
        err = body.get("error", {})
        detail = str(err.get("message") or err.get("type") or "")[:120]
    except ValueError:
        detail = resp.text[:120]
    raise SocialOauthError(f"{op}_http_{resp.status_code}:{detail}")


__all__ = [
    "SocialOauthError",
    "build_authorize_url",
    "discover_instagram_account_id",
    "exchange_code",
    "exchange_for_long_lived_token",
    "refresh_linkedin_token",
]
