"""Heuristic SEO scorer for prospecting homepage snapshots.

Deterministic content checks against the html returned by ``crawler.fetch_homepage``.
The score is a 0-100 integer; issues are short strings describing what's missing.

Checks (5 total, 20 points each):
  - <title> present and non-empty
  - <meta name="description"> present and non-empty
  - <h1> present and non-empty
  - mobile viewport meta tag
  - phone number OR city/location text on page
"""
from __future__ import annotations

import re
from typing import Any

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_META_DESC_RE = re.compile(
    r'<meta[^>]+name=["\']description["\'][^>]*content=["\']([^"\']+)["\']',
    re.IGNORECASE,
)
_META_DESC_RE_ALT = re.compile(
    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]*name=["\']description["\']',
    re.IGNORECASE,
)
_H1_RE = re.compile(r"<h1[^>]*>(.*?)</h1>", re.IGNORECASE | re.DOTALL)
_VIEWPORT_RE = re.compile(
    r'<meta[^>]+name=["\']viewport["\'][^>]*content=["\'][^"\']*width=device-width',
    re.IGNORECASE,
)
_PHONE_RE = re.compile(r"\b\(?\d{3}\)?[\s.\-]?\d{3}[\s.\-]?\d{4}\b")
_LOCATION_HINT_RE = re.compile(
    r"\b(?:address|located in|serving|hours|directions|map|contact us)\b",
    re.IGNORECASE,
)


def _has(pattern: re.Pattern[str], text: str) -> bool:
    match = pattern.search(text)
    if not match:
        return False
    # Capture-group-bearing patterns: insist the group is non-empty.
    if match.groups():
        return bool(match.group(1).strip())
    return True


def score_seo(page: dict[str, Any]) -> tuple[int, list[str]]:
    """Score the page on a 0-100 scale and list missing-signal issues.

    Returns ``(score, issues)``. ``score`` is an int. ``issues`` is a list of
    short human-readable issue strings (deterministic order).
    """
    issues: list[str] = []
    html = ""
    if isinstance(page, dict):
        raw_html = page.get("html")
        if isinstance(raw_html, str):
            html = raw_html

    if not html:
        return 0, ["no_html"]

    checks = [
        ("missing_title", _has(_TITLE_RE, html)),
        (
            "missing_meta_description",
            _has(_META_DESC_RE, html) or _has(_META_DESC_RE_ALT, html),
        ),
        ("missing_h1", _has(_H1_RE, html)),
        ("missing_mobile_viewport", _has(_VIEWPORT_RE, html)),
        (
            "missing_phone_or_location",
            bool(_PHONE_RE.search(html)) or bool(_LOCATION_HINT_RE.search(html)),
        ),
    ]

    score = 0
    weight = 100 // len(checks)
    for label, ok in checks:
        if ok:
            score += weight
        else:
            issues.append(label)

    # Round-up the 100/5=20 case to make max == 100 exactly.
    if all(ok for _, ok in checks):
        score = 100

    return score, issues
