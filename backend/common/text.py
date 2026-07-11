"""Small text utilities used across logging, audit, and error reporting."""
from __future__ import annotations

import re

_WS_RE = re.compile(r"\s+")
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")


def truncate(s: str | None, max_len: int, suffix: str = "â¦") -> str:
    """Return ``s`` clipped to ``max_len`` characters, suffix-marking truncation."""
    if s is None:
        return ""
    if len(s) <= max_len:
        return s
    keep = max(0, max_len - len(suffix))
    return s[:keep] + suffix


def snippet(s: str | None, max_len: int = 200) -> str:
    """Single-line snippet: collapse whitespace, strip control chars, truncate."""
    if s is None:
        return ""
    cleaned = _CTRL_RE.sub("", _WS_RE.sub(" ", s)).strip()
    return truncate(cleaned, max_len)


def sanitize_for_log(value: object, max_len: int = 500) -> str:
    """Best-effort sanitization of arbitrary objects for log lines."""
    try:
        as_str = value if isinstance(value, str) else str(value)
    except Exception:
        as_str = f"<unstringable {type(value).__name__}>"
    return snippet(as_str, max_len)


def looks_like_email(s: str) -> bool:
    return bool(s) and "@" in s and "." in s.split("@", 1)[-1] and " " not in s
