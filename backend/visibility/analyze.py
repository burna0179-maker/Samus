"""Pure analysis of an AI answer — brand/competitor mentions + cited domains.

No network, no state: this is the testable heart of the workcell.
"""

from __future__ import annotations

import re
from collections import Counter

# Capture the registrable host out of any http(s) URL (drops a leading www.).
_URL_RE = re.compile(r"https?://(?:www\.)?([a-z0-9.-]+\.[a-z]{2,})", re.IGNORECASE)
# Match a whole URL token, for stripping before the name-mention scan.
_URL_TOKEN_RE = re.compile(r"https?://\S+", re.IGNORECASE)


def _mentions(text: str, term: str) -> int:
    """Count whole-word, case-insensitive occurrences of ``term`` in ``text``."""
    term = term.strip()
    if not term:
        return 0
    pattern = r"\b" + re.escape(term) + r"\b"
    return len(re.findall(pattern, text, flags=re.IGNORECASE))


def extract_domains(text: str) -> list[str]:
    """Return the distinct cited domains (lower-cased, www-stripped), in order
    of first appearance."""
    seen: list[str] = []
    for match in _URL_RE.finditer(text or ""):
        domain = match.group(1).lower().rstrip(".")
        if domain not in seen:
            seen.append(domain)
    return seen


def analyze_answer(text: str, brand_terms: list[str], competitor_terms: list[str]) -> dict:
    """Analyze one answer. Returns ``{brand_cited, competitor_hits (Counter-like
    dict), cited_domains}``.

    ``brand_terms`` are the aliases of the brand (e.g. "Hustleforge", "Samus");
    a hit on any counts as a brand citation. ``competitor_terms`` are competitor
    names; each is tracked separately for the breakdown.
    """
    text = text or ""
    # Count NAME mentions in prose only — strip URLs first so a cited domain
    # (e.g. apollo.io) doesn't inflate the "Apollo" name count. Domains are
    # tracked separately via extract_domains on the original text.
    prose = _URL_TOKEN_RE.sub(" ", text)
    brand_cited = any(_mentions(prose, t) > 0 for t in brand_terms)
    competitor_hits: dict[str, int] = {}
    for term in competitor_terms:
        n = _mentions(prose, term)
        if n:
            competitor_hits[term] = n
    return {
        "brand_cited": brand_cited,
        "competitor_hits": competitor_hits,
        "cited_domains": extract_domains(text),
    }


def aggregate(probes: list[dict]) -> dict:
    """Aggregate a list of per-probe analyses into share-of-voice metrics.

    Each input dict must have ``answered`` (bool), ``brand_cited`` (bool),
    ``competitor_hits`` (dict[str,int]), ``cited_domains`` (list[str]). Returns
    the fields of :class:`~backend.visibility.models.ShareOfVoice` as a dict.
    """
    answered = [p for p in probes if p.get("answered")]
    n = len(answered)
    brand_citing = sum(1 for p in answered if p.get("brand_cited"))
    brand_mentions = sum(1 for p in answered if p.get("brand_cited"))
    competitor_counter: Counter[str] = Counter()
    for p in answered:
        competitor_counter.update(p.get("competitor_hits") or {})
    competitor_mentions = sum(competitor_counter.values())

    domain_counter: Counter[str] = Counter()
    for p in answered:
        domain_counter.update(p.get("cited_domains") or [])

    total_voice = brand_mentions + competitor_mentions
    return {
        "sample_n": n,
        "citation_rate": round(brand_citing / n, 4) if n else 0.0,
        "brand_mentions": brand_mentions,
        "competitor_mentions": competitor_mentions,
        "share_of_voice": round(brand_mentions / total_voice, 4) if total_voice else 0.0,
        "competitor_breakdown": dict(competitor_counter.most_common()),
        "top_cited_domains": domain_counter.most_common(10),
    }
