"""Touch context — prior-history digest for recycled prospects.

WHY (operator directive 2026-07-03): when the recycle pass re-qualifies a
prospect (promotion cooldown lapsed, ring wrapped), it must re-enter the
pipeline as a FOLLOW-UP carrying everything we learned — emails sent, calls
made, voicemails left, outcomes, summaries — so the next email opens with
continuity ("following up on my note from June about your site's security
grade") and the next call script knows the history. Re-approaching a known
prospect cold burns the accumulated warmth instead of compounding it.

``build_touch_summary`` pulls the prospect's CRM history (conversations +
call-state) over the signed mesh and compresses it into a compact,
deterministic, single-line digest that fits a CSV cell and can be dropped
verbatim into an email opener, a voicemail line, or an LLM personalization
prompt. No LLM cost — pure projection. NEVER raises; every failure degrades
to an empty string (the prospect just proceeds without enrichment).
"""

from __future__ import annotations

import logging
import os
from typing import Any

_LOG = logging.getLogger("samus.prospecting.touch_context")

# Hard ceiling for the digest — it rides in a CSV cell and an email opener.
_MAX_SUMMARY_CHARS = 400
# How many most-recent conversations to project.
_MAX_CONVERSATIONS = 6


def _crm_url() -> str:
    return (os.environ.get("CRM_URL", "") or "http://samus-crm:8080").rstrip("/")


def _short_date(iso_ts: str) -> str:
    """'2026-06-15T17:09:58Z' -> '6/15' (best-effort; raw prefix on failure)."""
    try:
        d = str(iso_ts)[:10]
        _, m, day = d.split("-")
        return f"{int(m)}/{int(day)}"
    except Exception:  # noqa: BLE001
        return str(iso_ts)[:10]


def _digest_conversation(row: dict[str, Any]) -> str:
    channel = str(row.get("channel") or "touch")
    when = _short_date(str(row.get("ended_at") or row.get("started_at") or ""))
    outcome = str(row.get("outcome") or "").strip()
    summary = str(row.get("summary") or "").strip().replace("\n", " ")
    if len(summary) > 90:
        summary = summary[:87].rstrip() + "..."
    bits = [f"{channel} {when}".strip()]
    if outcome:
        bits.append(outcome)
    if summary:
        bits.append(f'"{summary}"')
    return " ".join(bits)


def build_touch_summary(prospect_id: str) -> str:
    """Compact prior-touch digest for one prospect, or "" when there is no
    reachable history. Deterministic, bounded, never raises."""
    pid = (prospect_id or "").strip()
    if not pid:
        return ""
    parts: list[str] = []
    try:
        from backend.common.http_client import signed_get_json_sync

        base = _crm_url()
        # Conversations — the richest signal (email + call rows, summaries,
        # outcomes). Newest handful only.
        try:
            resp = signed_get_json_sync(
                base,
                f"/crm/conversations?prospect_id={pid}&limit={_MAX_CONVERSATIONS}",
                timeout=8.0,
            )
            if resp.status_code == 200:
                convs = (resp.json() or {}).get("conversations") or []
                for row in convs:
                    if isinstance(row, dict):
                        d = _digest_conversation(row)
                        if d:
                            parts.append(d)
        except Exception as exc:  # noqa: BLE001
            _LOG.debug("touch-context conversations fetch failed for %s: %s", pid, exc)

        # Call-state — cheap last-outcome backstop even when conversations
        # are empty (e.g. voicemail drafts never became Conversation rows).
        try:
            resp = signed_get_json_sync(base, f"/crm/call-state/{pid}", timeout=6.0)
            if resp.status_code == 200:
                cs = resp.json() or {}
                last = str(cs.get("last_outcome") or "").strip()
                when = _short_date(str(cs.get("updated_at") or ""))
                if last and not any(last in p for p in parts):
                    parts.append(f"last call-state {when}: {last}")
        except Exception as exc:  # noqa: BLE001
            _LOG.debug("touch-context call-state fetch failed for %s: %s", pid, exc)
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("touch-context assembly failed for %s: %s", pid, exc)
        return ""

    if not parts:
        return ""
    digest = "prior touches: " + "; ".join(parts)
    if len(digest) > _MAX_SUMMARY_CHARS:
        digest = digest[: _MAX_SUMMARY_CHARS - 3].rstrip() + "..."
    return digest
