"""Outbound email hygiene — reject undeliverable / garbage recipient addresses
BEFORE they reach the send path.

Prospecting scrapes owner emails off business homepages, which sometimes yields
non-emails that merely contain an ``@`` and a dot: asset filenames
(``logo-dark@2x.png``, ``ima-...@2x.webp``, ``_@astro.xxx.css``), placeholder
stubs (``user@domain.com``), or a website-builder's support address. Sending to
these guarantees a hard bounce (or hits an unrelated third party), which on a
fresh sender reputation is exactly the damage that gets a domain blocklisted.

``is_bad_email`` is a conservative, deterministic reject: it only flags addresses
that are clearly NOT a real business inbox, so legitimate small-business emails
(gmail/yahoo/company domains) always pass.
"""

from __future__ import annotations

import re

# Filenames masquerading as emails: the whole string ends in an asset extension.
_ASSET_EXT: tuple[str, ...] = (
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".svg",
    ".webp",
    ".ico",
    ".bmp",
    ".css",
    ".js",
    ".mjs",
    ".json",
    ".woff",
    ".woff2",
    ".ttf",
    ".eot",
    ".pdf",
    ".mp4",
    ".webm",
    ".mov",
    ".map",
)

# Placeholder / example stubs (exact addresses, local-parts, or domains).
_PLACEHOLDER_ADDRESSES: frozenset[str] = frozenset(
    {
        "user@domain.com",
        "email@domain.com",
        "example@example.com",
        "test@test.com",
        "your@email.com",
        "youremail@example.com",
        "name@example.com",
        "email@example.com",
        "info@example.com",
        "someone@example.com",
        "no-reply@example.com",
    }
)
_PLACEHOLDER_LOCALS: frozenset[str] = frozenset(
    {
        "user",
        "example",
        "test",
        "youremail",
        "yourname",
        "firstname",
        "lastname",
        "sample",
    }
)
_PLACEHOLDER_DOMAINS: frozenset[str] = frozenset(
    {
        "domain.com",
        "example.com",
        "example.org",
        "example.net",
        "email.com",
        "yourdomain.com",
        "test.com",
        "company.com",
        "sentry.io",
        "wix.com",
    }
)

# Website-builder / platform support inboxes — deliverable but NOT the business.
_PLATFORM_DOMAINS: frozenset[str] = frozenset(
    {
        "webador.com",
        "wixsite.com",
        "godaddy.com",
        "squarespace.com",
        "weebly.com",
        "wordpress.com",
        "sentry-next.wixpress.com",
    }
)

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def is_bad_email(email: str) -> bool:
    """True if ``email`` is not a real, deliverable business inbox.

    Conservative: only rejects clearly-invalid addresses (bad structure, asset
    filenames, placeholder stubs, known platform-support inboxes). Real
    small-business emails (freemail or own domain) return False.
    """
    e = (email or "").strip().lower()
    if not e or not _EMAIL_RE.match(e):
        return True
    if any(e.endswith(ext) for ext in _ASSET_EXT):
        return True
    local, _, domain = e.partition("@")
    if e in _PLACEHOLDER_ADDRESSES:
        return True
    if local in _PLACEHOLDER_LOCALS:
        return True
    if domain in _PLACEHOLDER_DOMAINS or domain in _PLATFORM_DOMAINS:
        return True
    # TLD sanity: the final label must be alphabetic and >= 2 chars (kills
    # '2x.png'-style leftovers the extension list somehow missed, and numeric
    # junk domains).
    tld = domain.rsplit(".", 1)[-1]
    if len(tld) < 2 or not tld.isalpha():
        return True
    return False


def is_good_email(email: str) -> bool:
    return not is_bad_email(email)
