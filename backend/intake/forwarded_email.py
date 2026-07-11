"""Parse forwarded email bodies to recover original headers.

When an operator forwards their sent reply to Samus's polled inbox for
archival, the delivered email has:

    From:    <operator address>          (whoever forwarded it)
    To:      samushustleforge@gmail.com  (Samus's inbox)
    Subject: Fwd: <original subject>

...and the *body* begins with a forward preamble carrying the original
headers. Gmail and Titan use the ``---------- Forwarded message ---------``
convention; both are supported. Titan wraps the whole body in HTML soup
so the preamble is only visible after HTML stripping — this module strips
HTML first, then scans.

The recovered ORIGINAL ``To:`` feeds
:func:`backend.intake.email_classifier.classify`'s outbound branch so
operator-forwarded outbound mail routes to the right client's
correspondence thread.

Fail-soft: any input that doesn't match the preamble shape yields ``None``
(the caller falls through to content-based client detection, then to
regular classification).

No LLM, no network. Two compiled regexes + a header scan.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass


# Both Gmail and Titan use "---------- Forwarded message ---------" but
# Titan's HTML-stripped body collapses to a single line with no anchors.
# Match the marker ANYWHERE in the text.
_FWD_MARKER_RE = re.compile(
    r"-{6,}\s*Forwarded message\s*-{6,}",
    re.IGNORECASE,
)

# Inline field extractors — used AFTER the marker to pluck From/To/Subject/
# Date out of a run-on line. Non-greedy: stop at the next header keyword or
# a run of whitespace long enough to signal end-of-header block.
_HEADER_KEYWORDS = "From:|To:|Cc:|Bcc:|Date:|Sent:|Subject:|Reply-To:"
_FROM_INLINE_RE = re.compile(
    rf"From:\s*(?P<val>.*?)(?=\s+(?:{_HEADER_KEYWORDS})|\Z)",
    re.IGNORECASE | re.DOTALL,
)
_TO_INLINE_RE = re.compile(
    rf"To:\s*(?P<val>.*?)(?=\s+(?:{_HEADER_KEYWORDS})|\Z)",
    re.IGNORECASE | re.DOTALL,
)
_SUBJECT_INLINE_RE = re.compile(
    rf"Subject:\s*(?P<val>.*?)(?=\s+(?:{_HEADER_KEYWORDS})|\Z)",
    re.IGNORECASE | re.DOTALL,
)
_DATE_INLINE_RE = re.compile(
    rf"Date:\s*(?P<val>.*?)(?=\s+(?:{_HEADER_KEYWORDS})|\Z)",
    re.IGNORECASE | re.DOTALL,
)


# --- HTML stripping (shared with the classifier's content-detection branch) --
# Titan / Gmail HTML view / rich clients wrap the body in a wall of tags
# and inline styles. Strip aggressively for classification-time analysis;
# preservation of exact original body_text for the artifact happens
# elsewhere.

_SCRIPT_STYLE_RE = re.compile(
    r"<(script|style)[^>]*>.*?</\1>",
    re.IGNORECASE | re.DOTALL,
)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t]+")
_BLANKLINES_RE = re.compile(r"\n\s*\n")


# Real HTML has closing tags, self-closing tags, entities, or common
# block-level tag names. A bare "<addr@dom>" email header is NOT HTML.
_HTML_INDICATOR_RE = re.compile(
    r"</\w+>"  # closing tag
    r"|<[a-z]+[^@>]*/>"  # self-closing (no @ inside)
    r"|<(div|span|html|body|head|table|tr|td|th|tbody|thead|p|br|a\s|img\s|"
    r"script|style|em|strong|b|i|u|ul|ol|li|h[1-6]|meta|link|form|input)\b"
    r"|&(amp|nbsp|lt|gt|quot|#\d+|rsquo|lsquo|rdquo|ldquo|mdash|ndash);",
    re.IGNORECASE,
)


def _looks_like_html(text: str) -> bool:
    """True if the text carries actual HTML markup (not just <email> headers)."""
    if not text or "<" not in text:
        return False
    return bool(_HTML_INDICATOR_RE.search(text))


def strip_html(text: str) -> str:
    """Convert HTML-heavy body_text to a readable plain-text form.

    Removes ``<script>``/``<style>`` blocks entirely, drops all remaining
    tags, decodes HTML entities, and collapses whitespace. Never raises.
    """
    if not text:
        return ""
    try:
        t = _SCRIPT_STYLE_RE.sub(" ", text)
        t = _TAG_RE.sub(" ", t)
        t = html.unescape(t)
        # Common entity-like sequences that html.unescape misses
        for src, dst in (
            ("&nbsp;", " "),
            ("&rsquo;", "'"),
            ("&lsquo;", "'"),
            ("&rdquo;", '"'),
            ("&ldquo;", '"'),
            ("&mdash;", "-"),
            ("&ndash;", "-"),
        ):
            t = t.replace(src, dst)
        t = _WS_RE.sub(" ", t)
        t = _BLANKLINES_RE.sub("\n\n", t)
        return t.strip()
    except Exception:  # noqa: BLE001
        return text


# Address extractor: matches "Name <addr@dom>" OR bare "addr@dom".
_ADDR_RE = re.compile(r"[\w.+\-]+@[\w\-]+(?:\.[\w\-]+)+")


@dataclass(frozen=True)
class ForwardedHeaders:
    """Original headers recovered from a forwarded email body."""

    from_addr: str = ""
    to_addr: str = ""  # FIRST recipient (backward compat)
    all_to_addrs: tuple[str, ...] = ()  # ALL recipients found in To: line
    subject: str = ""
    date: str = ""

    def has_recipient(self) -> bool:
        return bool(self.to_addr or self.all_to_addrs)


def parse_forwarded_body(body_text: str) -> ForwardedHeaders | None:
    """Return the ORIGINAL headers of a forwarded body, or None.

    Strips HTML first so Titan-style forwards (where the entire body is
    wrapped in a wall of tags AND the stripped result is a single run-on
    line) match the same "---------- Forwarded message ---------" regex
    as Gmail plain-text forwards. Header fields are then plucked from
    the stripped run-on text via non-greedy field extractors — no
    dependence on line breaks.

    None means the body has no recognizable forward preamble — the
    caller falls through to content-based client detection.
    """
    if not body_text:
        return None
    text = strip_html(body_text) if _looks_like_html(body_text) else body_text
    m = _FWD_MARKER_RE.search(text)
    if not m:
        return None

    tail = text[m.end() :]

    def _pluck(regex: re.Pattern) -> str:
        hit = regex.search(tail)
        return hit.group("val").strip() if hit else ""

    from_val = _pluck(_FROM_INLINE_RE)
    to_val = _pluck(_TO_INLINE_RE)
    subject = _pluck(_SUBJECT_INLINE_RE)
    date = _pluck(_DATE_INLINE_RE)

    # Address extraction: prefer the FIRST email in the value (headers may
    # carry "Name <addr@dom>, other@dom", quoted names with commas, etc.)
    from_match = _ADDR_RE.search(from_val)
    from_addr = from_match.group(0).lower() if from_match else ""
    all_to = tuple(a.lower() for a in _ADDR_RE.findall(to_val))
    to_addr = all_to[0] if all_to else ""

    if not from_addr and not to_addr:
        return None
    return ForwardedHeaders(
        from_addr=from_addr,
        to_addr=to_addr,
        all_to_addrs=all_to,
        subject=subject,
        date=date,
    )


__all__ = ["ForwardedHeaders", "parse_forwarded_body"]
