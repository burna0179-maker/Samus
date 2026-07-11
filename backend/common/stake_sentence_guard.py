"""Anti-template + dedup validators for operator stake_sentences.

The stake_sentence is supposed to be the one line the operator wrote
themselves — the non-protocol-derivable "I chose you, here is why."
A pasted cold-email cliche defeats the entire purpose.

Two guards:

  - :func:`validate_stake_sentence` — content rules: length window,
    banned templating phrases, all-lowercase reject (no proper noun
    in the sentence means no real human reference), excessive repeated
    whitespace, ASCII-ratio floor (blocks emoji spam, tolerates the
    occasional curly quote / em-dash).

  - :func:`is_duplicate` — SHA256 over normalized text checked against
    the rolling ledger of the last N hashes stored alongside the
    budget JSON. If the operator types the same sentence twice it's
    not a stake — it's a template.

The hash ledger is co-located with the daily budget JSON because both
fail-close together. Same directory, separate file, so a corrupted
budget bucket doesn't lose the dedup history.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
from typing import Iterable


_LOG = logging.getLogger("samus.common.stake_sentence_guard")


STAKE_SENTENCE_BANNED_PHRASES: tuple[str, ...] = (
    "i hope this finds you well",
    "i came across your",
    "noticed you",
    "saw your business",
    "we help businesses",
    "we work with",
    "our company specializes",
    "synergy",
    "leverage",
    "ecosystem",
    "circle back",
    "touch base",
)

STAKE_SENTENCE_MIN_LEN = 40
STAKE_SENTENCE_MAX_LEN = 280
STAKE_SENTENCE_ASCII_RATIO_FLOOR = 0.95
STAKE_SENTENCE_DEDUP_WINDOW = 100

_DEFAULT_DEDUP_PATH = "/opt/samus/data/stake_sentence_dedup.json"

_WS_RUN_RE = re.compile(r"[ \t]{3,}|\n{3,}")
_MULTISPACE_RE = re.compile(r"\s+")


class StakeSentenceRejected(ValueError):
    """Operator-authored sentence failed a guard rule. Carries the reason."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _normalize(text: str) -> str:
    return _MULTISPACE_RE.sub(" ", (text or "").strip().lower())


def _sha256_normalized(text: str) -> str:
    return hashlib.sha256(_normalize(text).encode("utf-8")).hexdigest()


def _ascii_ratio(text: str) -> float:
    if not text:
        return 1.0
    n = sum(1 for ch in text if ord(ch) < 128)
    return n / len(text)


def validate_stake_sentence(text: str) -> None:
    """Raise :class:`StakeSentenceRejected` on any rule failure. No return value on pass."""
    if text is None:
        raise StakeSentenceRejected("empty: stake_sentence must be a non-empty string")
    raw = str(text)
    stripped = raw.strip()
    if not stripped:
        raise StakeSentenceRejected("empty: stake_sentence must be a non-empty string")

    n = len(stripped)
    if n < STAKE_SENTENCE_MIN_LEN:
        raise StakeSentenceRejected(
            f"too_short: {n} chars < min {STAKE_SENTENCE_MIN_LEN}",
        )
    if n > STAKE_SENTENCE_MAX_LEN:
        raise StakeSentenceRejected(
            f"too_long: {n} chars > max {STAKE_SENTENCE_MAX_LEN}",
        )

    lowered = stripped.lower()
    for phrase in STAKE_SENTENCE_BANNED_PHRASES:
        if phrase in lowered:
            raise StakeSentenceRejected(
                f"banned_phrase: contains template tell {phrase!r}",
            )

    # No upper-case anywhere = no proper noun. The brief: "no real human
    # reference." Single quote/double-quote first chars are fine — we look
    # for any cased letter at all.
    if not any(ch.isupper() for ch in stripped):
        raise StakeSentenceRejected(
            "all_lowercase: stake_sentence has no proper noun (no upper-case letter)",
        )

    if _WS_RUN_RE.search(raw):
        raise StakeSentenceRejected(
            "repeated_whitespace: contains 3+ consecutive spaces, tabs, or newlines",
        )

    ratio = _ascii_ratio(stripped)
    if ratio < STAKE_SENTENCE_ASCII_RATIO_FLOOR:
        raise StakeSentenceRejected(
            f"non_ascii_ratio: ascii ratio {ratio:.3f} < floor {STAKE_SENTENCE_ASCII_RATIO_FLOOR}",
        )


# ---------------------------------------------------------------------------
# Dedup ledger
# ---------------------------------------------------------------------------

_DEDUP_LOCK = threading.Lock()


def _dedup_path() -> str:
    return os.getenv("SAMUS_STAKE_SENTENCE_DEDUP_PATH", "").strip() or _DEFAULT_DEDUP_PATH


def _load_hashes(path: str) -> list[str]:
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
    except (OSError, ValueError) as exc:
        _LOG.warning("stake_sentence_guard dedup load failed: %s", exc)
        return []
    items = data.get("hashes")
    if not isinstance(items, list):
        return []
    return [str(h) for h in items if isinstance(h, str)]


def _save_hashes(path: str, hashes: Iterable[str]) -> None:
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"hashes": list(hashes)}, f, indent=2)
        os.replace(tmp, path)
    except OSError as exc:
        _LOG.warning("stake_sentence_guard dedup save failed: %s", exc)


def is_duplicate(text: str, lookback: int = STAKE_SENTENCE_DEDUP_WINDOW) -> bool:
    """True iff the normalized sentence's SHA256 matches any of the last ``lookback`` hashes."""
    h = _sha256_normalized(text)
    with _DEDUP_LOCK:
        hashes = _load_hashes(_dedup_path())
        recent = hashes[-max(1, int(lookback)) :]
        return h in recent


def record_hash(text: str, lookback: int = STAKE_SENTENCE_DEDUP_WINDOW) -> None:
    """Append the normalized SHA256 to the rolling ledger.

    Must be called AFTER a successful guard + persistence so a refused
    or duplicate sentence doesn't pollute the dedup history.
    """
    h = _sha256_normalized(text)
    path = _dedup_path()
    with _DEDUP_LOCK:
        hashes = _load_hashes(path)
        hashes.append(h)
        # Keep at most 4x the active window so a future widened lookback
        # still has a history without growing unboundedly.
        cap = max(1, int(lookback)) * 4
        if len(hashes) > cap:
            hashes = hashes[-cap:]
        _save_hashes(path, hashes)


def reset_dedup_ledger() -> None:
    """Wipe the dedup ledger. Tests + administrative reset only."""
    path = _dedup_path()
    with _DEDUP_LOCK:
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError as exc:
            _LOG.warning("stake_sentence_guard dedup reset failed: %s", exc)


__all__ = [
    "STAKE_SENTENCE_BANNED_PHRASES",
    "STAKE_SENTENCE_MIN_LEN",
    "STAKE_SENTENCE_MAX_LEN",
    "STAKE_SENTENCE_DEDUP_WINDOW",
    "StakeSentenceRejected",
    "validate_stake_sentence",
    "is_duplicate",
    "record_hash",
    "reset_dedup_ledger",
]
