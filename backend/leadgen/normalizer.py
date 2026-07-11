"""Domain normalization for the leadgen workcell."""

from __future__ import annotations

from urllib.parse import urlparse


def normalize_domain(value: str) -> str:
    """Return a canonical lowercase hostname.

    Steps: strip whitespace, prepend ``https://`` if no scheme, urlparse,
    lowercase the host, strip the trailing slash, strip a leading ``www.``.
    """
    cleaned = (value or "").strip()
    if not cleaned:
        return ""
    if "://" not in cleaned:
        cleaned = f"https://{cleaned}"
    parsed = urlparse(cleaned)
    host = (parsed.hostname or parsed.netloc or "").lower().strip()
    host = host.rstrip("/")
    if host.startswith("www."):
        host = host[4:]
    return host
