"""AI mention monitor for Hustleforge.

Tracks when Hustleforge gets mentioned or cited by AI platforms via inbound
referrer traffic.  Ledger is a JSONL file at data/marketing/ai_mentions.jsonl.

Functions are pure/fast by default; I/O is isolated to log_ai_referral and
get_mention_stats so callers can mock them trivially.

ASCII-only output.  No new external dependencies.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_LOG = logging.getLogger("samus.marketing.brand_monitor")

# ---------------------------------------------------------------------------
# Ledger path
# ---------------------------------------------------------------------------

_DEFAULT_LEDGER_SUBPATH = "marketing/ai_mentions.jsonl"


def _ledger_path() -> Path:
    """Resolve the ledger path from env or storage root."""
    try:
        from backend.common import storage

        base = storage.root()
    except Exception:  # noqa: BLE001
        base = Path(os.getenv("SAMUS_DATA_ROOT", "data"))
    p = base / _DEFAULT_LEDGER_SUBPATH
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


# ---------------------------------------------------------------------------
# AI-referrer detection
# ---------------------------------------------------------------------------

# Ordered list of (pattern, platform_name) pairs.
# Checked against the lowercased referrer URL in order; first match wins.
_REFERRER_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"perplexity\.ai"), "perplexity"),
    (re.compile(r"chatgpt\.com"), "chatgpt"),
    (re.compile(r"chat\.openai\.com"), "chatgpt"),
    (re.compile(r"claude\.ai"), "claude"),
    (re.compile(r"bing\.com/chat"), "bing_copilot"),
    (re.compile(r"copilot\.microsoft\.com"), "bing_copilot"),
    (re.compile(r"you\.com"), "you"),
    (re.compile(r"phind\.com"), "phind"),
    (re.compile(r"kagi\.com"), "kagi"),
    (re.compile(r"poe\.com"), "poe"),
    (re.compile(r"gemini\.google\.com"), "gemini"),
    (re.compile(r"bard\.google\.com"), "gemini"),
    (re.compile(r"pi\.ai"), "pi"),
]


def detect_ai_referrer(referrer_url: str) -> str | None:
    """Parse a referrer URL and return the AI platform name, or None.

    Returns a lowercase string identifier like "perplexity" or "chatgpt",
    or None when the referrer is not a known AI platform.
    """
    if not referrer_url:
        return None
    lowered = referrer_url.lower()
    for pattern, name in _REFERRER_PATTERNS:
        if pattern.search(lowered):
            return name
    return None


# ---------------------------------------------------------------------------
# Ledger I/O
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log_ai_referral(
    source: str,
    query: str,
    url: str,
    *,
    ledger_path: Path | None = None,
) -> None:
    """Append one AI-referral event to the JSONL ledger.

    Parameters
    ----------
    source:
        AI platform name, e.g. "perplexity".  Use detect_ai_referrer() to
        derive this from a raw referrer URL.
    query:
        The search query or question the user typed (if known; empty string
        when not available).
    url:
        The Hustleforge page URL the user landed on.
    ledger_path:
        Override the default ledger path (useful in tests).
    """
    record: dict[str, Any] = {
        "source": source,
        "query": query,
        "url": url,
        "ts": _now_iso(),
    }
    path = ledger_path or _ledger_path()
    try:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=True) + "\n")
    except OSError as exc:
        _LOG.warning("brand_monitor: failed to write ledger: %s", exc)


def get_mention_stats(
    days: int = 30,
    *,
    ledger_path: Path | None = None,
) -> dict[str, Any]:
    """Summarise AI referral events from the last ``days`` calendar days.

    Returns a dict with:
      total          -- total referral events in window
      by_platform    -- {platform: count}
      by_page        -- {url: count} (top landing pages)
      top_queries    -- list of (query, count) tuples sorted descending
      window_days    -- the requested window
    """
    path = ledger_path or _ledger_path()
    if not path.exists():
        return {
            "total": 0,
            "by_platform": {},
            "by_page": {},
            "top_queries": [],
            "window_days": days,
        }

    cutoff_ts = _cutoff_ts(days)
    total = 0
    platform_counts: Counter[str] = Counter()
    page_counts: Counter[str] = Counter()
    query_counts: Counter[str] = Counter()

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        _LOG.warning("brand_monitor: failed to read ledger: %s", exc)
        lines = []

    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        ts = record.get("ts", "")
        if ts < cutoff_ts:
            continue
        total += 1
        source = record.get("source") or "unknown"
        url = record.get("url") or ""
        query = record.get("query") or ""
        platform_counts[source] += 1
        if url:
            page_counts[url] += 1
        if query:
            query_counts[query] += 1

    return {
        "total": total,
        "by_platform": dict(platform_counts.most_common()),
        "by_page": dict(page_counts.most_common(10)),
        "top_queries": query_counts.most_common(10),
        "window_days": days,
    }


def _cutoff_ts(days: int) -> str:
    """ISO timestamp string for ``days`` ago (UTC). Used for string comparison
    against ledger ts fields stored in the same format."""
    from datetime import timedelta

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    return cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")


__all__ = [
    "detect_ai_referrer",
    "log_ai_referral",
    "get_mention_stats",
]
