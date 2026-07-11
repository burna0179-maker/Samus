"""Operator CLI that ties the social_oauth helpers into a runnable flow.

``backend.outreach.social_oauth`` ships pure helpers (``build_authorize_url`` /
``exchange_code``) but deliberately no route — the interactive consent step is
an operator task (mirroring the Gmail ``backend.intake.gmail_oauth`` precedent).
This module is that operator entry point, kept non-interactive + testable via
two subcommands:

    # 1. Print the consent URL (open it in a browser, log in, consent).
    python -m backend.outreach.social_oauth_cli url \
        --platform linkedin --redirect-uri https://localhost/callback

    # 2. The provider redirects to redirect_uri?code=...&state=...; swap the
    #    code for tokens (persist the printed access_token into the secret
    #    store + set LINKEDIN_ACCESS_TOKEN / FACEBOOK_PAGE_TOKEN afterwards).
    python -m backend.outreach.social_oauth_cli exchange \
        --platform linkedin --code <code> --redirect-uri https://localhost/callback

Client id / secret are read from the environment (never hardcoded, never a CLI
arg so they don't leak into shell history):

    SAMUS_LINKEDIN_CLIENT_ID   / SAMUS_LINKEDIN_CLIENT_SECRET
    SAMUS_FACEBOOK_CLIENT_ID   / SAMUS_FACEBOOK_CLIENT_SECRET
    SAMUS_INSTAGRAM_CLIENT_ID  / SAMUS_INSTAGRAM_CLIENT_SECRET

Live posting stays governed elsewhere (social_adapter); this only completes the
token handshake an operator explicitly chooses to run.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from typing import Any, Mapping

from .social_oauth import build_authorize_url, exchange_code

_PLATFORMS = ("linkedin", "facebook", "instagram")


def _secret(platform: str, kind: str, env: Mapping[str, str]) -> str:
    """Read SAMUS_<PLATFORM>_CLIENT_<KIND> from ``env`` or raise ValueError."""
    var = f"SAMUS_{platform.upper()}_CLIENT_{kind.upper()}"
    value = (env.get(var) or "").strip()
    if not value:
        raise ValueError(f"missing required environment variable {var}")
    return value


def authorize_url(
    platform: str,
    redirect_uri: str,
    *,
    scope: str | None = None,
    state: str | None = None,
    env: Mapping[str, str] | None = None,
) -> tuple[str, str]:
    """Return ``(consent_url, state)`` for ``platform``.

    ``state`` is generated (uuid4 hex) when not supplied so the caller can
    verify it on the redirect. client_id comes from the environment.
    """
    env = env if env is not None else os.environ
    client_id = _secret(platform, "id", env)
    csrf = state or uuid.uuid4().hex
    url = build_authorize_url(platform, client_id, redirect_uri, csrf, scope=scope)
    return url, csrf


def exchange(
    platform: str,
    code: str,
    redirect_uri: str,
    *,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Swap an authorization ``code`` for tokens. client_id/secret from env."""
    env = env if env is not None else os.environ
    client_id = _secret(platform, "id", env)
    client_secret = _secret(platform, "secret", env)
    return exchange_code(platform, code, client_id, client_secret, redirect_uri)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="social_oauth_cli")
    sub = parser.add_subparsers(dest="command", required=True)

    p_url = sub.add_parser("url", help="print the consent URL")
    p_url.add_argument("--platform", required=True, choices=_PLATFORMS)
    p_url.add_argument("--redirect-uri", required=True)
    p_url.add_argument("--scope", default=None)
    p_url.add_argument("--state", default=None)

    p_ex = sub.add_parser("exchange", help="exchange an auth code for tokens")
    p_ex.add_argument("--platform", required=True, choices=_PLATFORMS)
    p_ex.add_argument("--code", required=True)
    p_ex.add_argument("--redirect-uri", required=True)

    args = parser.parse_args(argv)
    try:
        if args.command == "url":
            url, state = authorize_url(
                args.platform,
                args.redirect_uri,
                scope=args.scope,
                state=args.state,
            )
            print(f"state: {state}")
            print(url)
        else:  # exchange
            tokens = exchange(args.platform, args.code, args.redirect_uri)
            print(json.dumps(tokens, indent=2))
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 — surface provider/transport errors cleanly
        print(f"error: {exc.__class__.__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
