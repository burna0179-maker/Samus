"""Phone/address deep-verification of candidate websites — the precision layer
that closes the generic-name blind spot in ``presence_check``.

A name+city web search alone cannot tell "Webtech Solutions" (Yuba City, whose
real site is webtechsolution.org) apart from a dozen same-named companies
(webtechsolutionsllc.com, webtechsolutions.ca, webtechsolutions.us ...). Gemini's
own text verdict is conservative for exactly this reason, so real sites get
missed and a business that HAS a site can be wrongly pitched "you have no site."

This module resolves the ambiguity with the prospect's OWN contact data:

  1. Gather candidate domains from Gemini grounded Google search — over the
     business NAME (+city/state) AND, as a second pass, over the prospect's
     PHONE NUMBER (a phone uniquely identifies the business, so a phone search
     tends to surface the real homepage that a name search buries).
  2. FETCH each candidate and confirm ownership by matching the prospect's phone
     (and/or street-number + ZIP) on the page. A phone match is near-definitive:
     only the real business puts its own number on its site, so same-name
     impostor domains are rejected automatically.

Returns the confirmed URL (high confidence) or NOT-found. It never guesses from
a bare name/search result — attributing a site without a contact match is what
mis-labels same-name companies, in BOTH directions (wrongly "you have no site",
or wrongly "here's your site" pointing at an impostor). The conservative
name-search fallback (presence_check.web_search_finds_site) supplies the safe
low-confidence signal separately, so recall never regresses. Fail-soft: any
error returns not-found.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

_LOG = logging.getLogger("samus.website.deep_verify")

_MAX_CANDIDATES = 8  # cap total fetches per prospect (cost/latency guard)
_FETCH_TIMEOUT = 8.0
_GEMINI_TIMEOUT = 30.0
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)


@dataclass
class DeepVerdict:
    found: bool
    url: str = ""
    matched_on: str = ""  # "phone" | "address" | ""
    confidence: str = ""  # "high" (contact match) — only value set on found
    reason: str = ""
    candidates: list[str] = field(default_factory=list)


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s or "")


def _phone10(s: str) -> str:
    """Last 10 digits — US number comparison, formatting-agnostic."""
    d = _digits(s)
    return d[-10:] if len(d) >= 10 else d


def _host(url: str) -> str:
    try:
        h = (urlparse(url if "://" in url else "http://" + url).hostname or "").lower()
    except Exception:  # noqa: BLE001
        return ""
    return h[4:] if h.startswith("www.") else h


def _visible_text(html: str) -> str:
    t = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html or "", flags=re.S | re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    return re.sub(r"\s+", " ", t)


def _page_has_phone(html: str, phone10: str) -> bool:
    """True if the prospect's 10-digit number appears in the page's digit stream
    (robust to (555) 555-5555 / 555-555-5555 / 5555555555 / tel:+15005550006)."""
    if not phone10 or len(phone10) < 10:
        return False
    return phone10 in _digits(html)


def _address_tokens(address: str) -> tuple[str, str]:
    """(street_number, zip5) parsed from a formatted address; '' when absent."""
    a = address or ""
    m = re.match(r"\s*(\d{1,6})\b", a)
    sn = m.group(1) if m else ""
    z = re.search(r"\b(\d{5})(?:-\d{4})?\b", a)
    zc = z.group(1) if z else ""
    return sn, zc


def _page_has_address(text: str, address: str) -> bool:
    """Both the street number AND the ZIP present — strong corroboration when a
    phone isn't on the page. Needs both to avoid coincidental number matches."""
    sn, zc = _address_tokens(address)
    if not (sn and zc):
        return False
    return bool(
        re.search(rf"\b{re.escape(zc)}\b", text) and re.search(rf"\b{re.escape(sn)}\b", text)
    )


def _gemini_sources(
    query: str,
    *,
    api_key: str,
    http_client=None,
    timeout: float = _GEMINI_TIMEOUT,
    model: str = "gemini-2.5-flash",
) -> list[str]:
    """Grounded Google search -> candidate source DOMAINS (raw results).

    A search-oriented prompt maximizes the grounding SOURCES returned. We use the
    raw result domains — NOT Gemini's conservative prose verdict — because the
    domains are exactly the candidate set the phone/address match needs to
    confirm. (Gemini's abstaining prose is the SAFE low-confidence signal and is
    provided separately by presence_check.web_search_finds_site; the raw source
    list must NOT be used as a fallback — its first entry is often a same-name
    impostor, which only a contact match can rule in or out.)"""
    try:
        import httpx

        prompt = (
            f"Search Google for the official website of this business: {query}. "
            f"Look at the actual search results and their source websites."
        )
        body = {"contents": [{"parts": [{"text": prompt}]}], "tools": [{"google_search": {}}]}
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        own = http_client is None
        client = http_client or httpx.Client(timeout=timeout)
        try:
            r = client.post(url, headers={"x-goog-api-key": api_key}, json=body)
            r.raise_for_status()
            data = r.json() or {}
        finally:
            if own:
                client.close()
        cand = (data.get("candidates") or [{}])[0]
        gm = cand.get("groundingMetadata", {}) or {}
        hosts: list[str] = []
        for ch in gm.get("groundingChunks") or []:
            title = ((ch.get("web") or {}).get("title") or "").strip().lower()
            # grounded-search chunk titles carry the source DOMAIN (e.g.
            # "webtechsolution.org"); a real title with spaces is a page title,
            # not a domain — skip those.
            if "." in title and " " not in title:
                hosts.append(title)
        # plus any explicit URLs Gemini cited in prose
        text = "".join(p.get("text", "") for p in (cand.get("content", {}) or {}).get("parts", []))
        for m in re.findall(r"https?://[^\s)>\]\"']+", text):
            h = _host(m)
            if h:
                hosts.append(h)
        return hosts
    except Exception as exc:  # noqa: BLE001 — best-effort, never blocks
        _LOG.warning("gemini sources failed for %r: %s", query, exc)
        return []


def _fetch(url: str, *, client) -> str:
    try:
        r = client.get(url)
        if r.status_code >= 400:
            return ""
        return r.text or ""
    except Exception:  # noqa: BLE001
        return ""


def _check_hosts(hosts, *, phone10, address, client, seen, candidates) -> DeepVerdict | None:
    """Fetch each fresh, non-directory host and test for a contact match. Returns
    a high-confidence DeepVerdict on the first phone/address hit, else None."""
    from backend.website.presence_check import _is_directory

    for h in hosts:
        host = _host(h)
        if not host or host in seen:
            continue
        seen.add(host)
        u = "https://" + host
        if _is_directory(u):
            continue
        if len(candidates) >= _MAX_CANDIDATES:
            break
        candidates.append(u)
        home = _fetch(u, client=client)
        if not home:
            continue
        # Homepage first; if it doesn't carry the contact info, try /contact
        # (small-biz sites often keep the phone/address only on the contact page).
        pages = [home]
        home_hit = _page_has_phone(home, phone10) or (
            address and _page_has_address(_visible_text(home), address)
        )
        if not home_hit:
            c = _fetch(u + "/contact", client=client)
            if c:
                pages.append(c)
        for html in pages:
            if _page_has_phone(html, phone10):
                return DeepVerdict(
                    True,
                    url=u,
                    matched_on="phone",
                    confidence="high",
                    reason=f"prospect phone found on {u}",
                )
            if address and _page_has_address(_visible_text(html), address):
                return DeepVerdict(
                    True,
                    url=u,
                    matched_on="address",
                    confidence="high",
                    reason=f"prospect address (street#+ZIP) found on {u}",
                )
    return None


def deep_verify_site(
    company_name: str,
    *,
    city: str = "",
    state: str = "",
    known_phone: str = "",
    known_address: str = "",
    api_key: str | None = None,
    seed_url: str = "",
) -> DeepVerdict:
    """HIGH-CONFIDENCE ownership confirmation: does ``company_name`` own a live
    site whose page carries the prospect's phone/address? Returns found=True ONLY
    on a contact match (never on a bare name/search guess — that stays the job of
    the conservative web_search fallback in the caller, which won't attribute a
    same-name impostor). ``seed_url`` = a candidate the caller already found (the
    web_search result); it is verified first so a hit UPGRADES it to high
    confidence with the confirmed URL. See module docstring."""
    from backend.common.config import get_settings

    s = get_settings()
    gkey = ((api_key if api_key is not None else getattr(s, "gemini_api_key", "")) or "").strip()
    phone10 = _phone10(known_phone)
    if not (phone10 or known_address):
        return DeepVerdict(False, reason="no phone/address anchor — cannot deep-verify")

    seen: set[str] = set()
    candidates: list[str] = []
    try:
        import httpx

        with httpx.Client(
            timeout=_FETCH_TIMEOUT, follow_redirects=True, headers={"User-Agent": _UA}
        ) as client:
            # Verify the caller's already-found site FIRST (cheap upgrade path).
            if seed_url:
                v = _check_hosts(
                    [seed_url],
                    phone10=phone10,
                    address=known_address,
                    client=client,
                    seen=seen,
                    candidates=candidates,
                )
                if v:
                    v.candidates = candidates
                    return v
            # Discover + verify more candidates via grounded search: the business
            # NAME first, then the PHONE NUMBER (uniquely identifies the business,
            # surfaces a real homepage a generic-name search buries). Phone round
            # runs only if the name round yields no contact match.
            if gkey:
                queries = [" ".join(x for x in (company_name, city, state) if x).strip()]
                if phone10:
                    queries.append(known_phone)
                for q in queries:
                    hosts = _gemini_sources(q, api_key=gkey)
                    v = _check_hosts(
                        hosts,
                        phone10=phone10,
                        address=known_address,
                        client=client,
                        seen=seen,
                        candidates=candidates,
                    )
                    if v:
                        v.candidates = candidates
                        return v
    except Exception as exc:  # noqa: BLE001 — best-effort insurance, never blocks
        _LOG.warning("deep verify failed for %r: %s", company_name, exc)
        return DeepVerdict(
            False, reason=f"deep verify error ({type(exc).__name__})", candidates=candidates
        )

    return DeepVerdict(
        False, reason="no candidate carried the prospect's phone/address", candidates=candidates
    )
