"""Detect a business-closure-with-return-date from a call transcript.

When an outbound call hits an answering machine that announces a temporary
closure ("our office will be closed June 29th through July 5th, reopening
July 6th"), the prospect is a LEAD, not a dead-end — it should be re-surfaced
on its return date (see :mod:`backend.voice.callback_queue`).

Extraction is LLM-based on purpose: Vapi STT spells numbers out ("June twenty
ninth") and phrasings vary wildly, which defeats regex. To keep it cheap, the
LLM is only called when the transcript contains closure keywords; otherwise we
return None immediately (no model call, no cost). The LLM runs through
``backend.common.local_llm`` (local-first, OpenAI-backup), so a missing local
model still resolves via the fallback.

Returns ``(callback_date_iso, reason)`` or ``(None, "")``. Never raises.
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime

_LOG = logging.getLogger("samus.voice.closure_detector")

# Cheap gate: only invoke the LLM if the transcript smells like a closure.
_CLOSURE_HINTS = (
    "closed", "closure", "reopen", "re-open", "be closed", "will be closed",
    "back in the office", "back on", "out of the office", "holiday hours",
    "observ",  # "observing the holiday"
)

_SYSTEM = (
    "You read a voicemail/answering-machine transcript from a business phone "
    "line and decide if it announces a TEMPORARY closure with a reopen date. "
    "Today's date is {today}. If the message says the business is closed and "
    "will reopen / be back on a specific date, output ONLY that reopen date as "
    "YYYY-MM-DD (assume the nearest future occurrence). If it is closed 'through' "
    "or 'until' a date, output the next day after that date. If there is no "
    "temporary closure with a knowable reopen date, output exactly NONE. "
    "Output ONLY the date or NONE — no other words."
)

_DATE_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")


def detect_closure_callback(
    transcript: str,
    *,
    today: str | None = None,
    llm=None,
) -> tuple[str | None, str]:
    """Return (reopen_date_iso, reason) if the transcript announces a temporary
    closure with a reopen date, else (None, "")."""
    text = (transcript or "").strip()
    if len(text) < 15:
        return (None, "")
    low = text.lower()
    if not any(h in low for h in _CLOSURE_HINTS):
        return (None, "")

    today = today or date.today().isoformat()
    if llm is None:
        from backend.common.local_llm import chat as llm  # noqa: PLC0415

    try:
        raw = llm(
            system=_SYSTEM.format(today=today),
            user=text[:2000],
            max_tokens=24,
            temperature=0,
        )
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("closure detect llm failed: %s", exc)
        return (None, "")

    raw = (raw or "").strip()
    if not raw or raw.upper().startswith("NONE"):
        return (None, "")
    m = _DATE_RE.search(raw)
    if not m:
        return (None, "")
    iso = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    # Sanity: must parse and be in the future (a closure reopens later).
    try:
        d = datetime.strptime(iso, "%Y-%m-%d").date()
    except ValueError:
        return (None, "")
    if d.isoformat() <= today:
        return (None, "")
    return (iso, "office announced temporary closure; reopens on this date")
