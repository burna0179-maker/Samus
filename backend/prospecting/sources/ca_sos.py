"""California Secretary of State business-search ingester (deterministic).

Queries the public CA SOS business-search HTML page (no API key required)
and parses the entity row matching the input business name. Returns a
single high-confidence ``public_registry`` LegitimacySignal when a row is
found; ``None`` on miss OR on any network/parse failure (fail-OPEN per
G8 design — see :mod:`backend.prospecting.sources` docstring).

Two-second HTTP timeout. No retries: this is a pre-flight check, not a
recovery path.

The CA SOS search page is HTML; we parse with regex on a small set of
well-known anchors rather than dragging BeautifulSoup into the runtime
image. If CA SOS substantially restructures the page the parser returns
None (miss) and we degrade to "no signal" — which is the safe direction.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

from ..legitimacy import LegitimacySignal

_LOG = logging.getLogger("samus.prospecting.sources.ca_sos")

_SEARCH_URL = "https://bizfileonline.sos.ca.gov/search/business"
_TIMEOUT = 2.0

# Deterministic anchors. Each regex MUST match its own field independently
# so a missing optional field (e.g., filing date) does not invalidate the
# whole row.
_RE_SOS_NUMBER = re.compile(
    r'(?:Entity\s*Number|SOS\s*Number)[^A-Za-z0-9-]{0,8}([A-Z0-9-]{6,16})',
    re.IGNORECASE,
)
_RE_STATUS = re.compile(
    r'(?:Entity\s*Status|Status)[^A-Za-z]{0,8}([A-Za-z][A-Za-z /-]{2,40})',
    re.IGNORECASE,
)
_RE_FILING_DATE = re.compile(
    r'(?:Registration\s*Date|Initial\s*Filing\s*Date|Filing\s*Date)[^0-9]{0,8}'
    r'(\d{1,2}/\d{1,2}/\d{2,4}|\d{4}-\d{2}-\d{2})',
    re.IGNORECASE,
)


def _fetch(http: Any | None, business_name: str) -> str | None:
    """Return raw HTML for the search; None on any network error."""
    name = (business_name or "").strip()
    if not name:
        return None
    try:
        if http is not None:
            resp = http.get(_SEARCH_URL, params={"q": name}, timeout=_TIMEOUT)
        else:
            import httpx
            resp = httpx.get(
                _SEARCH_URL, params={"q": name}, timeout=_TIMEOUT,
            )
        if getattr(resp, "status_code", 0) != 200:
            return None
        return getattr(resp, "text", "") or ""
    except Exception as exc:  # noqa: BLE001 — fail-OPEN
        _LOG.info("ca_sos fetch failed for %r: %s", name, exc)
        return None


def _parse(html: str) -> dict[str, str] | None:
    """Extract the first matching entity's fields. None when no row found."""
    if not html:
        return None
    m_num = _RE_SOS_NUMBER.search(html)
    if not m_num:
        return None
    m_status = _RE_STATUS.search(html)
    m_filing = _RE_FILING_DATE.search(html)
    return {
        "sos_number": m_num.group(1).strip(),
        "entity_status": (m_status.group(1).strip() if m_status else ""),
        "filing_date": (m_filing.group(1).strip() if m_filing else ""),
    }


def lookup_ca_sos(
    business_name: str,
    *,
    http: Any | None = None,
) -> LegitimacySignal | None:
    """Return a high-confidence public_registry signal, or None on miss/error.

    ``http`` is an optional injected ``httpx.Client``-like object (tests
    supply a stub). When omitted we construct a one-shot httpx GET with a
    2-second timeout.
    """
    html = _fetch(http, business_name)
    if html is None:
        return None
    parsed = _parse(html)
    if parsed is None:
        return None
    return LegitimacySignal(
        kind="public_registry",
        source=_SEARCH_URL,
        discovered_at=datetime.now(timezone.utc),
        evidence={
            "registry": "ca_sos",
            "business_name_query": (business_name or "").strip(),
            **parsed,
        },
        confidence="high",
    )


__all__ = ["lookup_ca_sos"]
