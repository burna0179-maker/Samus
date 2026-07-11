"""Cloudflare Turnstile CAPTCHA verification for the public intake endpoint.

Activation model
----------------
CAPTCHA is *opt-in*. When ``settings.intake_captcha_secret`` is empty (the
default) ``captcha_required()`` returns False and the onboarding route skips
CAPTCHA entirely — the feature ships complete but dormant until the operator
seals ``SAMUS_INTAKE_CAPTCHA_SECRET`` and the marketing site adds the
Turnstile widget.

Fail-closed contract
--------------------
Unlike the rate limiter (which fails OPEN), CAPTCHA verification fails
CLOSED: once a secret is configured, a missing token, an invalid token, OR a
verification call that cannot complete (network error, non-200) all reject
the request. The reasoning is that a configured CAPTCHA is an explicit
operator decision to demand proof-of-human; silently waving requests through
on a verification outage would defeat that decision. This only triggers when
a secret is set, so it never blocks the default (CAPTCHA-off) deployment.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from backend.common.config import get_settings


_LOG = logging.getLogger("samus.intake.captcha")

# Bounded timeout — the siteverify call sits inline in the onboarding request
# path, so a slow Cloudflare must not stall the lead indefinitely. A timeout
# is treated as a verification failure (fail-closed).
_VERIFY_TIMEOUT = httpx.Timeout(connect=4.0, read=6.0, write=4.0, pool=4.0)


@dataclass(frozen=True)
class CaptchaResult:
    """Outcome of a CAPTCHA verification attempt.

    ``ok`` is True only when Turnstile affirmatively confirmed the token.
    ``detail`` carries a short machine-readable reason on failure for the
    HTTP 400 body + logs.
    """

    ok: bool
    detail: str = ""


def captcha_required() -> bool:
    """True when a CAPTCHA secret is configured (feature activated)."""
    return bool((get_settings().intake_captcha_secret or "").strip())


def verify_captcha(token: str, *, source_ip: str = "") -> CaptchaResult:
    """Server-side-verify a Turnstile token against the siteverify API.

    Returns a failing ``CaptchaResult`` (never raises) on an empty token, a
    non-200 response, a transport error, or a ``success=false`` verdict —
    the fail-closed posture documented in the module docstring. Only called
    by the route when ``captcha_required()`` is True.
    """
    settings = get_settings()
    secret = (settings.intake_captcha_secret or "").strip()
    if not secret:
        # Defensive: the route only calls this when captcha_required() is
        # True, but if it ever does without a secret, treat as not-verified.
        return CaptchaResult(ok=False, detail="captcha_secret_unset")

    cleaned_token = (token or "").strip()
    if not cleaned_token:
        return CaptchaResult(ok=False, detail="captcha_token_missing")

    form: dict[str, str] = {"secret": secret, "response": cleaned_token}
    if source_ip:
        # Turnstile accepts the originating IP as an optional cross-check.
        form["remoteip"] = source_ip

    try:
        with httpx.Client(timeout=_VERIFY_TIMEOUT) as client:
            resp = client.post(settings.intake_captcha_verify_url, data=form)
    except httpx.HTTPError as exc:
        _LOG.warning("intake captcha verify transport error: %s — rejecting", exc)
        return CaptchaResult(ok=False, detail="captcha_verify_unreachable")

    if resp.status_code != 200:
        _LOG.warning(
            "intake captcha verify non-200 (%s) — rejecting",
            resp.status_code,
        )
        return CaptchaResult(
            ok=False,
            detail=f"captcha_verify_http_{resp.status_code}",
        )

    try:
        body = resp.json()
    except ValueError:
        _LOG.warning("intake captcha verify returned non-JSON — rejecting")
        return CaptchaResult(ok=False, detail="captcha_verify_bad_response")

    if not isinstance(body, dict) or not body.get("success"):
        codes = []
        if isinstance(body, dict):
            codes = body.get("error-codes") or []
        _LOG.info("intake captcha verification rejected token: %s", codes)
        return CaptchaResult(
            ok=False,
            detail="captcha_verification_failed: "
            + (",".join(str(c) for c in codes) or "no_success"),
        )

    return CaptchaResult(ok=True)
