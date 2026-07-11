"""Push LLM-derived call outcomes to the operator's call_outcomes journal.

The forge-ui Samus call card reads
  D:\\Hustleforge\\data\\call_outcomes\\call_outcomes_YYYY-MM-DD.jsonl
and buckets each prospect by today's outcome:
  no_answer/voicemail/gatekeeper/follow_up → 'followup'
  booked/not_interested/disqualified/etc.  → 'done'

When the transcript pipeline analyzes a call, write the result to the same
journal so the prospect drops off the active queue without operator action.

Operator precedence (HARD RULE): if the prospect already has an entry in
today's journal from a non-pipeline source, the pipeline NEVER overwrites
it. The operator's manual classification is always authoritative. The
pipeline only fills in entries for calls the operator hasn't logged yet.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime, timezone
from pathlib import Path

_LOG = logging.getLogger("samus.voice.crm_writeback")

# Map our analyzer outcomes onto the call_outcomes journal vocabulary.
# Pipeline outcomes that don't map are skipped (no journal entry written).
_OUTCOME_MAP: dict[str, str] = {
    "converted":            "booked",
    "follow_up_scheduled":  "follow_up",
    "call_back_requested":  "follow_up",
    "warm_lead":            "follow_up",
    "objection_unresolved": "follow_up",
    "not_interested":       "not_interested",
    "gatekeeper":           "gatekeeper",
    "voicemail_left":       "voicemail",
    "no_answer":            "no_answer",
    "dnc_requested":        "do_not_call",
}

_SOURCE_TAG = "transcript_pipeline"

# Journal location — matches forge-ui's HF_DATA_DIR convention.
def _journal_path(call_ts: datetime | None = None) -> Path:
    base = Path(os.getenv("HF_DATA_DIR", r"D:\Hustleforge\data"))
    day = (call_ts or datetime.now(timezone.utc)).date().isoformat()
    return base / "call_outcomes" / f"call_outcomes_{day}.jsonl"


def write_outcome(
    *,
    prospect_id: str,
    company: str,
    phone: str,
    pipeline_outcome: str,
    notes: str,
    call_ts: datetime,
) -> dict:
    """Append a pipeline-derived outcome to today's journal if-and-only-if
    no operator entry exists for this prospect on this call date.

    Returns a dict with keys:
      written       bool — did we actually append a row
      skipped_for   str | None — reason for skip (operator_already_logged, mapped_none, etc.)
      mapped_outcome str | None — the call_outcomes vocabulary value used
    """
    if not prospect_id:
        return {"written": False, "skipped_for": "no_prospect_id"}

    mapped = _OUTCOME_MAP.get(pipeline_outcome)
    if not mapped:
        return {"written": False, "skipped_for": f"no_map_for_{pipeline_outcome}"}

    journal = _journal_path(call_ts)
    if _operator_already_logged(journal, prospect_id):
        return {
            "written": False,
            "skipped_for": "operator_already_logged",
            "mapped_outcome": mapped,
        }

    entry = {
        "ts": call_ts.strftime("%Y-%m-%dT%H:%M:%S"),
        "prospect_id": prospect_id,
        "company": company or "",
        "phone": phone or "",
        "outcome": mapped,
        "notes": (notes or "")[:500],
        "source": _SOURCE_TAG,
    }

    try:
        journal.parent.mkdir(parents=True, exist_ok=True)
        with journal.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        _LOG.info(
            "crm_writeback: appended %s for %s (mapped from %s)",
            mapped, company or prospect_id, pipeline_outcome,
        )
        return {"written": True, "mapped_outcome": mapped}
    except OSError as exc:
        _LOG.warning("crm_writeback: journal append failed: %s", exc)
        return {"written": False, "skipped_for": f"io_error: {exc}"}


def _operator_already_logged(journal: Path, prospect_id: str) -> bool:
    """Return True if any non-pipeline source has logged this prospect today.

    Pipeline-source entries are NOT counted, so re-runs of the pipeline can
    correct/refresh their own earlier entries if the LLM result changes.
    """
    if not journal.is_file():
        return False
    try:
        with journal.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if str(row.get("prospect_id") or "") != prospect_id:
                    continue
                if str(row.get("source") or "").strip() != _SOURCE_TAG:
                    return True
    except OSError as exc:
        _LOG.warning("crm_writeback: journal read failed (%s): %s", journal, exc)
    return False
