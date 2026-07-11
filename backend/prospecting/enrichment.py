"""Owner-contact enrichment from prospect website HTML.

Pure regex extraction over html already fetched by
:func:`backend.prospecting.crawler.fetch_homepage`. Zero $ cost, no API keys,
no LLM calls. Adds ~0.5-1.5 sec per prospect when fallback /contact + /about
fetches fire.

Extraction targets and realistic hit rates on small-business sites:
  - owner_email           45-65%  (any non-junk email; first personal-looking
                                   one preferred over info@/hello@/support@)
  - contact_emails        50-70%  (all unique non-junk emails, ``"; "``-joined)
  - social_facebook       60-80%
  - social_instagram      40-60%
  - social_linkedin       20-30%
  - owner_linkedin_url    10-25%  (only linkedin.com/in/* — the personal flavor)
  - owner_name            10-20%  (JSON-LD Person or <meta name=author> only;
                                   brittle text-extraction patterns left out)
  - owner_title           skipped in v1 (no robust signal)

Directory-style "websites" (zillow.com/profile/X, allstate.com/agent/Y) won't
yield much because the prospect's contact isn't on a third-party site. That's
expected; the operator falls back to the phone number for those.
"""
from __future__ import annotations

import logging
import re
from html import unescape
from typing import Any
from urllib.parse import urljoin, urlparse

from .contact_validation import is_valid_email_syntax

_LOG = logging.getLogger("samus.prospecting.enrichment")

# --- email patterns ---------------------------------------------------------

_MAILTO_RE = re.compile(r'mailto:([^"\'?\s>]+)', re.IGNORECASE)
_EMAIL_RE = re.compile(
    r'(?<![A-Za-z0-9._%+-])'
    r'([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})'
    r'(?![A-Za-z0-9])',
)

# Local parts we treat as junk / not-real-owner. These are also useful as the
# "demote me" set when picking owner_email — a personal-looking address beats
# an info@ even when both are present.
_GENERIC_LOCAL_PARTS = frozenset({
    "info", "hello", "contact", "support", "help", "office", "admin",
    "sales", "service", "team", "general", "inquiries", "frontdesk",
    "reception", "billing", "accounts",
})
_BLOCKED_LOCAL_PREFIXES = (
    "noreply", "no-reply", "donotreply", "do-not-reply",
    "postmaster", "abuse", "mailer-daemon", "bounce",
    "privacy", "webmaster", "security",
    "example", "test", "sample", "demo",
    # Third-party-platform telltales: a footer-scraped address whose local
    # part is one of these is the host platform's own engineering/ops alias,
    # never a sales contact for the prospect. Cold-mailing them burns the
    # SendGrid domain. (Real example 2026-06-22: bugreport@moatable.com on
    # the Erik Tejeda call card.)
    "bugreport", "bug-report", "bugs", "bug",
    "ticket", "tickets",
    "issue", "issues",
    "error", "errors",
    "crash", "crashes",
    "alert", "alerts", "monitoring",
    "devops", "sre", "ops",
)
_BLOCKED_DOMAINS = frozenset({
    "example.com", "example.org", "example.net",
    "sentry.io", "sentry-next.wixpress.com",
    "wix.com", "wixpress.com", "wixsite.com",
    "godaddy.com", "domains.google",
    "u.com", "sentry.com",
    # Third-party platform/CMS vendors whose contact addresses turn up in
    # site footers and are never the prospect's own mailbox.
    "moatable.com",
    "bugsnag.com", "rollbar.com", "pagerduty.com", "datadoghq.com",
    "intercom.io", "intercom.com",
})

# --- social patterns --------------------------------------------------------
# Avoid sharer / intent / tag URLs by requiring a path component that looks
# like a profile slug (alphanumerics + ._-) and stopping at the first ?#"' char.

# Negative lookahead excludes FB endpoint paths that are NOT profiles:
# - sharer / share / dialog: share buttons and OAuth dialogs
# - tr / plugins / widgets: tracking pixels + social plugin embeds
# - story / events / photo: single-resource URLs
# - pages/category: FB's own category index
# - help / business / ads / ad_ / login: FB's own properties
# - v\d+\.\d+: FB Graph API version paths like v2.10/dialog/oauth
_FB_RE = re.compile(
    r"https?://(?:www\.|m\.)?facebook\.com/"
    r"(?!sharer|share|dialog|tr|plugins|widgets|story|stories|events?/"
    r"|photo|photos|pages/category|help|business|ads|ad_|login"
    r"|v\d+\.\d+)"
    r"([A-Za-z0-9.\-_/]+)",
    re.IGNORECASE,
)
_IG_RE = re.compile(
    r"https?://(?:www\.)?instagram\.com/(?!p/|reel/|tv/|explore)([A-Za-z0-9._]+)",
    re.IGNORECASE,
)
_LI_RE = re.compile(
    r"https?://(?:www\.)?linkedin\.com/(in|company|pub|school)/([A-Za-z0-9._\-]+)",
    re.IGNORECASE,
)

# --- name patterns ----------------------------------------------------------

_AUTHOR_META_RE = re.compile(
    r"""<meta\b[^>]+\bname=["']author["'][^>]+\bcontent=["']([^"']+)["']""",
    re.IGNORECASE,
)
# Search inside any JSON-LD <script> for "@type":"Person" with a "name".
# Lenient about whitespace, doesn't require a closing brace on the same line.
_SCHEMA_PERSON_NAME_RE = re.compile(
    r'"@type"\s*:\s*"Person"[\s\S]{0,400}?"name"\s*:\s*"([^"]+)"',
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _is_blocked_email(email: str) -> bool:
    local, _, domain = email.lower().partition("@")
    if not local or not domain:
        return True
    if domain in _BLOCKED_DOMAINS:
        return True
    if any(local.startswith(p) for p in _BLOCKED_LOCAL_PREFIXES):
        return True
    return False


def _is_personal_looking(email: str) -> bool:
    """Personal-looking = local part isn't in the GENERIC list and doesn't
    look like a department alias. Used to prefer owner-style addresses."""
    local = email.lower().partition("@")[0]
    return local not in _GENERIC_LOCAL_PARTS


def _extract_emails(html: str) -> list[str]:
    """Return ordered list of unique, non-blocked, syntactically-valid emails.

    mailto: addresses first. ``_EMAIL_RE`` is a permissive *extraction* regex —
    it will happily match a structurally-impossible string like
    ``magnolia-.com`` (a label may not end in a hyphen) — so every candidate is
    passed through :func:`contact_validation.is_valid_email_syntax` before it is
    kept, and a malformed string is dropped rather than stored as a contact.
    """
    seen: set[str] = set()
    out: list[str] = []
    for m in _MAILTO_RE.finditer(html):
        email = unescape(m.group(1).strip().split("?", 1)[0]).lower()
        if (email and email not in seen and not _is_blocked_email(email)
                and is_valid_email_syntax(email)):
            seen.add(email)
            out.append(email)
    for m in _EMAIL_RE.finditer(html):
        email = m.group(1).strip().lower()
        if (email and email not in seen and not _is_blocked_email(email)
                and is_valid_email_syntax(email)):
            seen.add(email)
            out.append(email)
    return out


def _pick_owner_email(emails: list[str]) -> str:
    """Prefer a personal-looking address; fall back to first generic."""
    for e in emails:
        if _is_personal_looking(e):
            return e
    return emails[0] if emails else ""


def _looks_like_business_name(name: str) -> bool:
    """Reject business-name-shaped strings from owner_name extraction.
    'Sutter Buttes Real Estate Group' must not become owner_name."""
    tokens = name.split()
    if len(tokens) > 5:
        return True
    suffixes = {"llc", "inc", "inc.", "corp", "co", "co.", "ltd", "group",
                "services", "realty", "agency", "company", "associates"}
    for t in tokens:
        if t.lower().strip(",.") in suffixes:
            return True
    return False


def _extract_owner_name(html: str) -> str:
    """High-signal sources only: schema.org Person, then <meta name=author>.
    Returns empty string if neither yields a person-shaped name."""
    m = _SCHEMA_PERSON_NAME_RE.search(html)
    if m:
        candidate = unescape(m.group(1)).strip()
        if candidate and not _looks_like_business_name(candidate):
            return candidate
    m = _AUTHOR_META_RE.search(html)
    if m:
        candidate = unescape(m.group(1)).strip()
        # author meta is often the business name on small-biz CMS templates;
        # apply the same guard.
        if candidate and not _looks_like_business_name(candidate):
            return candidate
    return ""


# --- business description ---------------------------------------------------
# The business's own one-line "what we do" blurb — what an operator wants to
# glance at before dialing. og:description and <meta name=description> are
# matched in both attribute orders; JSON-LD `description` is a scoped fallback
# (LocalBusiness schema usually carries a good one).

_OG_DESC_RE = re.compile(
    r'<meta\b[^>]+\bproperty=["\']og:description["\'][^>]+\bcontent=["\']([^"\']+)["\']',
    re.IGNORECASE,
)
_OG_DESC_RE_ALT = re.compile(
    r'<meta\b[^>]+\bcontent=["\']([^"\']+)["\'][^>]+\bproperty=["\']og:description["\']',
    re.IGNORECASE,
)
_META_DESC_RE = re.compile(
    r'<meta\b[^>]+\bname=["\']description["\'][^>]+\bcontent=["\']([^"\']+)["\']',
    re.IGNORECASE,
)
_META_DESC_RE_ALT = re.compile(
    r'<meta\b[^>]+\bcontent=["\']([^"\']+)["\'][^>]+\bname=["\']description["\']',
    re.IGNORECASE,
)
# JSON-LD `description`, scoped to ld+json <script> blocks so a stray
# "description" key elsewhere in the page can't leak in. Require >= 20 chars
# to skip trivially short values.
_JSONLD_BLOCK_RE = re.compile(
    r'<script\b[^>]*\btype=["\']application/ld\+json["\'][^>]*>([\s\S]*?)</script>',
    re.IGNORECASE,
)
_JSONLD_DESC_RE = re.compile(r'"description"\s*:\s*"([^"]{20,})"', re.IGNORECASE)

# Cap so the call list's business-description line stays one readable line.
_DESC_MAX_LEN = 240


def _clean_description(text: str) -> str:
    """Unescape entities, collapse whitespace, truncate to one readable line."""
    cleaned = re.sub(r"\s+", " ", unescape(text)).strip()
    if len(cleaned) > _DESC_MAX_LEN:
        cleaned = cleaned[:_DESC_MAX_LEN].rstrip() + "..."
    return cleaned


def _extract_description(html: str) -> str:
    """Extract the business's own description blurb from page meta tags.

    Priority: og:description -> <meta name=description> -> JSON-LD description.
    Returns an empty string when the page carries none.
    """
    for pattern in (_OG_DESC_RE, _OG_DESC_RE_ALT, _META_DESC_RE, _META_DESC_RE_ALT):
        m = pattern.search(html)
        if m and m.group(1).strip():
            return _clean_description(m.group(1))
    for block in _JSONLD_BLOCK_RE.finditer(html):
        m = _JSONLD_DESC_RE.search(block.group(1))
        if m and m.group(1).strip():
            return _clean_description(m.group(1))
    return ""


def _normalize_social(url: str) -> str:
    """Strip query/fragment + trailing slash for stable comparison."""
    cleaned = url.split("?", 1)[0].split("#", 1)[0].strip().rstrip("/")
    return cleaned


def _is_valid_fb_profile_url(url: str) -> bool:
    """Reject FB URLs that survived the regex but contain endpoint-path
    markers anywhere in the URL (the negative lookahead only checks the
    first path segment)."""
    blacklist = ("/dialog/", "/sharer/", "/share/", "/login/", "/help/",
                 "/business/", "/photo/", "/photos/", "/plugins/")
    lower = url.lower()
    return not any(b in lower for b in blacklist)


def _extract_social(html: str) -> dict[str, str]:
    out = {"facebook": "", "instagram": "", "linkedin": ""}
    # Walk all FB matches and take the first that passes the post-filter so
    # a junk OAuth URL doesn't shadow a real profile URL later in the html.
    for match in _FB_RE.finditer(html):
        candidate = _normalize_social(match.group(0))
        if _is_valid_fb_profile_url(candidate):
            out["facebook"] = candidate
            break
    ig = _IG_RE.search(html)
    if ig:
        out["instagram"] = _normalize_social(ig.group(0))
    li = _LI_RE.search(html)
    if li:
        out["linkedin"] = _normalize_social(li.group(0))
    return out


def _personal_linkedin(social_linkedin: str) -> str:
    """Return social_linkedin only if it's the /in/ flavor (a person profile),
    so owner_linkedin_url stays distinct from social_linkedin (which may be a
    company page)."""
    if "/in/" in social_linkedin.lower():
        return social_linkedin
    return ""


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------

_EMPTY_SIGNALS: dict[str, str] = {
    "owner_name": "",
    "owner_email": "",
    "owner_title": "",
    "owner_linkedin_url": "",
    "contact_emails": "",
    "social_facebook": "",
    "social_instagram": "",
    "social_linkedin": "",
    "business_description": "",
}


def extract_owner_signals(html: str | None, base_url: str = "") -> dict[str, str]:
    """Extract every owner-contact signal we can from a single page's html.

    Returns the same key set every time; missing fields are empty strings so
    callers can merge dicts without ``KeyError``.
    """
    if not html:
        return dict(_EMPTY_SIGNALS)

    emails = _extract_emails(html)
    social = _extract_social(html)
    return {
        "owner_name": _extract_owner_name(html),
        "owner_email": _pick_owner_email(emails),
        "owner_title": "",  # v1 stays empty — no robust homepage signal
        "owner_linkedin_url": _personal_linkedin(social["linkedin"]),
        "contact_emails": "; ".join(emails[:5]),  # cap at 5; longest first by find order
        "social_facebook": social["facebook"],
        "social_instagram": social["instagram"],
        "social_linkedin": social["linkedin"],
        "business_description": _extract_description(html),
    }


def merge_signals(
    primary: dict[str, str], fallback: dict[str, str],
) -> dict[str, str]:
    """Fill empty fields in ``primary`` from ``fallback``.

    ``contact_emails`` is special-cased: union the two lists (dedup,
    preserving primary order), then cap at 5 and rejoin.
    """
    merged: dict[str, str] = {}
    for k, v in primary.items():
        merged[k] = v if v else fallback.get(k, "")
    # union emails
    p_list = [e for e in (primary.get("contact_emails") or "").split("; ") if e]
    f_list = [e for e in (fallback.get("contact_emails") or "").split("; ") if e]
    seen: set[str] = set()
    union: list[str] = []
    for e in p_list + f_list:
        if e and e not in seen:
            seen.add(e)
            union.append(e)
    merged["contact_emails"] = "; ".join(union[:5])
    # owner_email may be empty in primary but found in fallback's emails
    if not merged.get("owner_email") and union:
        merged["owner_email"] = _pick_owner_email(union)
    return merged


# ---------------------------------------------------------------------------
# Facebook About scraper (free-tier, fragile, low-yield by design)
# ---------------------------------------------------------------------------
#
# Strategy: fetch `mbasic.facebook.com/{handle}/about` because the basic-mobile
# surface returns more parseable HTML than the JS-heavy www variant (which
# usually serves a login wall to unauthenticated bots). One attempt per
# prospect, no retries — retrying just accelerates IP-blocks. Failures
# return empty signals so the run continues without it.
#
# What we can realistically pull when FB cooperates:
#   - phone (if business owner listed it on the page)
#   - email (rare on FB, but sometimes present)
#   - "Founded" line / business description
#   - sometimes page admins / managers (very rare, varies by privacy setting)
#
# What FB will NOT give us without login:
#   - full employee list
#   - any data behind the "People who like this" gate
#   - private contact details
#
# When FB blocks (and it will, eventually): we just get an HTML page with
# login prompt + no extractable signals. extract_facebook_signals returns
# all-empty and the prospect record is unaffected.

_FB_HANDLE_RE = re.compile(
    r"https?://(?:m\.|mbasic\.|www\.|business\.)?facebook\.com/"
    r"(?:pages/[^/]+/(\d+)|profile\.php\?id=(\d+)|([A-Za-z0-9.\-_]+))",
    re.IGNORECASE,
)

_FB_USER_AGENT = (
    # Older Android Chrome UA — mbasic.facebook.com accepts this and serves
    # the table-shaped About page instead of the JS-heavy app shell.
    "Mozilla/5.0 (Linux; Android 9; SM-G960F) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/100.0.4896.79 Mobile Safari/537.36"
)


def _facebook_handle(fb_url: str) -> str:
    """Extract the page handle / numeric id from a Facebook URL.

    Handles four shapes:
      facebook.com/SutterButtesRealEstate
      facebook.com/pages/<Name>/<numeric-id>
      facebook.com/profile.php?id=<numeric-id>
      facebook.com/{handle}/posts (and similar suffixes — trimmed)
    """
    m = _FB_HANDLE_RE.search(fb_url or "")
    if not m:
        return ""
    return next((g for g in m.groups() if g), "")


def facebook_about_url(fb_url: str) -> str:
    """Build the mbasic /about URL for a Facebook page. Empty if unparseable."""
    handle = _facebook_handle(fb_url)
    if not handle:
        return ""
    return f"https://mbasic.facebook.com/{handle}/about"


def _is_facebook_login_wall(html: str) -> bool:
    """Cheap heuristic — when FB serves a login wall, the body contains
    multiple login-form markers."""
    if not html:
        return True
    markers = ("login_form", "Forgotten password", "Create new account",
               '"login"', "loginbutton")
    hits = sum(1 for m in markers if m in html)
    return hits >= 2


_FB_PHONE_RE = re.compile(
    r"\(?\d{3}\)?[\s.\-]?\d{3}[\s.\-]?\d{4}",
)


def extract_facebook_signals(html: str | None) -> dict[str, str]:
    """Extract owner-contact signals from a Facebook About HTML body.

    Same key shape as :func:`extract_owner_signals` so :func:`merge_signals`
    can fold the result in. Empty fields when FB served a login wall, an
    empty page, or just didn't contain extractable signals.
    """
    if not html or _is_facebook_login_wall(html):
        return dict(_EMPTY_SIGNALS)

    emails = _extract_emails(html)
    # FB's mbasic surface sometimes carries the page's phone outside the
    # email list; we don't currently store phone separately in signals (the
    # ProspectRecord already has phone from Google Places), so we skip the
    # phone for now and just surface emails + any social URLs FB linked to.
    social = _extract_social(html)
    return {
        "owner_name": _extract_owner_name(html),
        "owner_email": _pick_owner_email(emails),
        "owner_title": "",
        "owner_linkedin_url": _personal_linkedin(social["linkedin"]),
        "contact_emails": "; ".join(emails[:5]),
        "social_facebook": "",  # we came FROM facebook; don't echo it back
        "social_instagram": social["instagram"],
        "social_linkedin": social["linkedin"],
        "business_description": _extract_description(html),
    }


def fetch_facebook_about(fb_url: str) -> str:
    """Fetch the mbasic Facebook About page; return html or empty.

    Single attempt, no retries. Uses a mobile Android Chrome UA which mbasic
    accepts. Caller gets ``""`` on any failure (block, redirect, timeout).
    """
    if not fb_url:
        return ""
    about = facebook_about_url(fb_url)
    if not about:
        return ""
    # Late import — keeps the module import graph clean and lets tests inject
    # a fake fetcher via enrich_from_page_with_fallback's fetcher arg.
    import httpx  # noqa: F401 — preserved so tests can monkeypatch this module's httpx.Client

    from backend.common import safe_fetch
    from backend.common.shared_http import get_shared_client

    headers = {
        "User-Agent": _FB_USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    try:
        # EC2-NEW-02 — the about URL derives from a prospect-supplied facebook
        # URL; validate it (and refuse redirect hops, which bypass the
        # pre-check) so a poisoned/redirecting URL cannot reach IMDS /
        # loopback / RFC-1918. Mirrors the security-audit NET-01 guard.
        safe_fetch.assert_public_http_url(about)
        client = get_shared_client(
            timeout=8.0, follow_redirects=False, headers=headers,
        )
        response = client.get(about)
    except safe_fetch.SsrfBlockedError as exc:
        _LOG.warning("facebook fetch blocked by SSRF guard url=%s err=%s", about, exc)
        return ""
    except Exception:  # noqa: BLE001 — single attempt, never raise out
        return ""
    if response.status_code != 200:
        return ""
    return response.text or ""


def fetch_secondary_pages(base_url: str) -> str:
    """Try /contact then /about under ``base_url``; concat any HTML returned.

    Returns empty string when neither URL yields a 200 with html. Never
    raises — fetch_homepage already swallows transport errors.
    """
    # Late import to avoid a circular import when enrichment is imported by
    # service.py at module-load time (crawler -> httpx is fine, but keep the
    # boundary clean).
    from .crawler import fetch_homepage

    parsed = urlparse(base_url)
    if not parsed.scheme or not parsed.netloc:
        return ""
    root = f"{parsed.scheme}://{parsed.netloc}"

    chunks: list[str] = []
    for path in ("/contact", "/about", "/contact-us", "/about-us"):
        if chunks and any(c for c in chunks):
            # Already have html from /contact; only also fetch /about if we
            # haven't yet — saves the two -us suffix variants from firing
            # when the first attempt worked.
            if path in ("/contact-us", "/about-us"):
                continue
        url = urljoin(root + "/", path.lstrip("/"))
        page = fetch_homepage(url)
        if page.get("status_code") == 200 and page.get("html"):
            chunks.append(page["html"])
        # /about is worth trying even if /contact succeeded -- they often
        # carry different signals (contact = emails, about = owner names).
    return "\n".join(chunks)


def enrich_from_page_with_fallback(
    page: dict[str, Any],
    base_url: str,
    *,
    secondary_fetcher: Any = None,
    facebook_fetcher: Any = None,
    enable_facebook: bool = True,
) -> dict[str, str]:
    """Cascade enrichment from cheapest source to most fragile.

    Stages (each only fires if the previous didn't yield ``owner_email``):
      1. homepage HTML (already fetched, free)
      2. ``/contact`` + ``/about`` on the same domain (1-2 HTTP calls)
      3. ``mbasic.facebook.com/{handle}/about`` if a social_facebook URL
         was found and ``enable_facebook`` is True (1 HTTP call, may be
         blocked by FB anti-bot — that's expected, returns empty gracefully)

    ``secondary_fetcher`` and ``facebook_fetcher`` are injected for tests so
    the cascade stays offline. Production passes None and the module's real
    fetchers are used.
    """
    html = (page or {}).get("html") or ""
    primary = extract_owner_signals(html, base_url=base_url)

    if primary["owner_email"]:
        return primary

    # Stage 2: same-domain secondary pages
    secondary_fn = secondary_fetcher or fetch_secondary_pages
    try:
        secondary_html = secondary_fn(base_url)
    except Exception as exc:  # noqa: BLE001 — defensive boundary
        _LOG.warning("secondary fetch failed url=%s err=%s", base_url, exc)
        secondary_html = ""

    merged = primary
    if secondary_html:
        fallback = extract_owner_signals(secondary_html, base_url=base_url)
        merged = merge_signals(merged, fallback)

    if merged["owner_email"] or not enable_facebook:
        return merged

    # Stage 3: Facebook About via mbasic. Only triggers when we have a FB URL
    # AND still don't have an owner_email. FB blocks aggressively; treat any
    # failure as "no signal found" and move on.
    fb_url = merged["social_facebook"] or primary["social_facebook"]
    if not fb_url:
        return merged

    fb_fn = facebook_fetcher or fetch_facebook_about
    try:
        fb_html = fb_fn(fb_url)
    except Exception as exc:  # noqa: BLE001 — defensive boundary
        _LOG.warning("facebook fetch failed url=%s err=%s", fb_url, exc)
        return merged

    if not fb_html:
        return merged

    fb_signals = extract_facebook_signals(fb_html)
    return merge_signals(merged, fb_signals)


__all__ = [
    "extract_owner_signals",
    "extract_facebook_signals",
    "merge_signals",
    "fetch_secondary_pages",
    "fetch_facebook_about",
    "facebook_about_url",
    "enrich_from_page_with_fallback",
]
