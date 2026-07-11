"""Domain + company-name normalization for prospecting.

Patterns mirror the doc's leadgen normalizer (§5.normalizer.py).
"""
from __future__ import annotations

from urllib.parse import urlparse


def normalize_domain(value: str) -> str:
    """Strip scheme + leading www., lowercase, strip trailing slash."""
    if not value:
        return ""
    raw = value.strip()
    if "://" not in raw:
        raw = "https://" + raw
    parsed = urlparse(raw)
    host = parsed.netloc.lower().rstrip("/")
    if host.startswith("www."):
        host = host[4:]
    return host


def normalize_company(name: str) -> str:
    """Strip + title-case (preserving acronyms is a future enhancement)."""
    return (name or "").strip().title()
