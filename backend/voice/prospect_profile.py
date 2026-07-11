"""Prior-call context for the outbound SDR assistant ("Morgan").

When the operator logs a call outcome + notes on a prospect (via forge-ui or
the autodial drain), it lands in a LOCAL host journal:

    ``HF_DATA_DIR/call_outcomes/call_outcomes_<YYYY-MM-DD>.jsonl``

(default ``HF_DATA_DIR=D:\\Hustleforge\\data``). Each line is a JSON object::

    {"ts", "prospect_id", "company", "phone", "outcome", "notes", "source"}

This is the canonical operator-action ledger — latest entry (by ``ts``) wins.
``build_prospect_context`` reads THIS journal (NOT the DynamoDB CRM, which the
container can't reach reliably) and projects a prospect's most-recent prior
outcome + note into a small dict of ``str`` values so the dialer can merge them
into Vapi ``assistantOverrides.variableValues`` and Morgan dials INFORMED.

Design constraints (per the build spec):
  * stdlib + ``json`` only.
  * NEVER raises — every failure path degrades to an all-empty dict-of-empties.
  * Absent ``HF_DATA_DIR`` / ``call_outcomes`` dir -> all-empty (graceful).
"""
from __future__ import annotations

import json
import os
from datetime import date, timedelta
from pathlib import Path

# Default host data root. Overridable via ``HF_DATA_DIR`` so the same code
# runs on the host (Windows path) or under a bind mount.
_DEFAULT_DATA_DIR = r"D:\Hustleforge\data"

# Note truncation ceiling (chars). Keeps the Vapi variable compact so the
# assistant prompt stays readable and within token budget.
_NOTES_MAX = 200

# Keys this module produces. Exposed so the dialer (and tests) can assert the
# exact contract and so an all-empty default is trivially constructed.
CONTEXT_KEYS: tuple[str, ...] = (
    "prior_outcome",
    "prior_notes",
    "prior_contact_summary",
)


def _empty_context() -> dict[str, str]:
    """The all-empty result returned whenever nothing is known / on any error."""
    return {key: "" for key in CONTEXT_KEYS}


def _data_dir() -> Path:
    """Resolve the host data root from ``HF_DATA_DIR`` (with the canonical default)."""
    raw = (os.getenv("HF_DATA_DIR") or "").strip() or _DEFAULT_DATA_DIR
    return Path(raw)


def _truncate(text: str, limit: int = _NOTES_MAX) -> str:
    """Trim ``text`` to ``limit`` chars, appending an ellipsis when cut."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _ts_sort_key(entry: dict) -> str:
    """Sortable ``ts`` key. Missing/garbage ts sorts oldest (loses to anything)."""
    ts = entry.get("ts")
    return ts if isinstance(ts, str) else ""


def build_prospect_context(prospect_id: str, *, days: int = 30) -> dict[str, str]:
    """Project a prospect's most-recent prior call context for Morgan's template.

    Scans the last ``days`` daily ``call_outcomes_<date>.jsonl`` files under
    ``HF_DATA_DIR/call_outcomes/``, collects entries whose ``prospect_id``
    matches, and keeps the latest by ``ts``. Returns a dict of ``str`` values:

      * ``prior_outcome`` — most recent prior outcome (e.g. ``"not_interested"``,
        ``"voicemail"``, ``"follow_up"``). ``""`` if none.
      * ``prior_notes`` — latest operator note, truncated to ~200 chars. ``""``
        if none.
      * ``prior_contact_summary`` — a one-liner like
        ``"Previously contacted 2026-06-15; outcome: follow_up; note: <...>"``,
        or ``""`` if there is no prior contact.

    All values are empty when nothing is known. NEVER raises — any error
    (missing dir, unreadable file, malformed JSON) degrades to all-empties.
    """
    try:
        pid = (prospect_id or "").strip()
        if not pid:
            return _empty_context()

        base = _data_dir() / "call_outcomes"
        if not base.is_dir():
            return _empty_context()

        # Window: today back through ``days`` prior days (inclusive). days<=0
        # collapses to today only.
        today = date.today()
        span = max(0, int(days))
        candidate_days = [today - timedelta(days=offset) for offset in range(span + 1)]

        latest: dict | None = None
        for day in candidate_days:
            path = base / f"call_outcomes_{day.isoformat()}.jsonl"
            if not path.is_file():
                continue
            try:
                raw = path.read_text(encoding="utf-8")
            except OSError:
                continue
            for line in raw.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if not isinstance(entry, dict):
                    continue
                if (entry.get("prospect_id") or "") != pid:
                    continue
                if latest is None or _ts_sort_key(entry) >= _ts_sort_key(latest):
                    latest = entry

        if latest is None:
            return _empty_context()

        outcome = str(latest.get("outcome") or "").strip()
        notes = _truncate(str(latest.get("notes") or ""))

        summary = ""
        ts_raw = str(latest.get("ts") or "").strip()
        contacted_on = ts_raw[:10] if ts_raw else ""
        if contacted_on or outcome:
            parts = ["Previously contacted"]
            if contacted_on:
                parts.append(f" {contacted_on}")
            if outcome:
                parts.append(f"; outcome: {outcome}")
            if notes:
                parts.append(f"; note: {notes}")
            summary = "".join(parts)

        return {
            "prior_outcome": outcome,
            "prior_notes": notes,
            "prior_contact_summary": summary,
        }
    except Exception:  # noqa: BLE001 — fail-soft by contract; never block a dial.
        return _empty_context()


__all__ = ["build_prospect_context", "CONTEXT_KEYS"]
