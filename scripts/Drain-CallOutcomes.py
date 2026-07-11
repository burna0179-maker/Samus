"""Drain Samus autonomous-call outcomes into forge-ui's call_outcomes journal.

The autonomous dialer fires calls and (via reconcile) records outcomes inside
the samus-voice container. But the OPERATOR's "work X prospects from the call
list" view in forge-ui drains itself by reading
``HF_DATA_DIR/call_outcomes/call_outcomes_<date>.jsonl`` on the HOST — which
the container can't write to. Without this bridge, a prospect Samus already
called still shows on the operator's list and gets re-dialled by hand.

This host-side script reads the reconcile CLI's JSON output (passed on stdin)
and appends each call's outcome to the forge-ui journal in forge-ui's exact
format, honouring forge-ui's outcome taxonomy + operator precedence:
  * "done" outcomes (not_interested/booked/do_not_call) DROP the prospect.
  * "followup" outcomes (voicemail/no_answer/gatekeeper) stay but show the
    outcome so the operator knows Samus already tried.
  * an entry from a real OPERATOR source is never overwritten (operator wins).
  * a prospect already drained by an automated source isn't double-written.

Usage (host, called by Run-CallReconcile.ps1):
    docker exec samus-voice python3 -m backend.voice.reconcile_cli \
        | python Drain-CallOutcomes.py
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta

# Samus reconcile outcome -> forge-ui call_outcomes vocabulary.
# Operator decision (2026-06-30): Samus OWNS the redial cadence (7-day
# cooldown), so every prospect Samus dialed drops from the operator's manual
# list — they "never see them again unless it converts." So the dead-end
# outcomes (voicemail/no_answer) map to "noted" (a DONE-bucket value → drops),
# with the real outcome preserved in the entry's notes. Only a genuine warm/
# converted outcome stays visible for the operator to act on.
_OUTCOME_MAP = {
    "not_interested": "not_interested",   # done -> drop
    "voicemail_left": "noted",            # done -> drop (real outcome in notes)
    "no_answer": "noted",                 # done -> drop (real outcome in notes)
    "dnc_requested": "do_not_call",       # done -> drop
    "converted": "booked",                # done -> drop from call list, surfaces in pipeline
    "completed_conversation": "follow_up",  # followup -> STAYS (operator engages a warm lead)
    "gatekeeper": "follow_up",            # followup -> STAYS (live human answered; warm lead)
}
# When we remap a dead-end to "noted", keep the human-readable real outcome.
_REAL_OUTCOME_LABEL = {
    "voicemail_left": "voicemail left",
    "no_answer": "no answer",
    "gatekeeper": "reached gatekeeper/receptionist",
}
_SOURCE = "samus_autodial"
# Automated sources don't block each other; only a real operator entry does.
_AUTOMATED = {"transcript_pipeline", "samus_autodial"}


def _journal_path() -> str:
    return _journal_path_for(datetime.now().date())


def _journal_path_for(day) -> str:
    base = os.getenv("HF_DATA_DIR", r"D:\Hustleforge\data")
    d = os.path.join(base, "call_outcomes")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"call_outcomes_{day.isoformat()}.jsonl")


def _existing(journal: str) -> tuple[set, set]:
    """Return (operator_logged_pids, automated_logged_pids)."""
    op, auto = set(), set()
    if not os.path.isfile(journal):
        return op, auto
    try:
        with open(journal, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                pid = str(r.get("prospect_id") or "")
                src = str(r.get("source") or "")
                if not pid:
                    continue
                (auto if src in _AUTOMATED else op).add(pid)
    except OSError:
        pass
    return op, auto


def main() -> int:
    # PowerShell's pipe to a native exe (Run-CallReconcile.ps1) prepends a UTF-8
    # BOM. Read raw BYTES and strip the BOM there — decoding first is unsafe
    # because Windows stdin may decode the BOM bytes as cp1252 mojibake
    # ('\xef\xbb\xbf' -> 'ï»¿'), which a char-level strip can't match. After
    # stripping, tolerate leading/trailing noise by scanning for the JSON object.
    data = sys.stdin.buffer.read()
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    raw = data.decode("utf-8", errors="replace").strip()
    if not raw:
        print(json.dumps({"drained": 0, "reason": "no_input"}))
        return 0
    summary = None
    try:
        summary = json.loads(raw)
    except ValueError:
        for line in raw.splitlines():
            line = line.strip().lstrip("﻿")
            if not line.startswith("{"):
                continue
            try:
                cand = json.loads(line)
            except ValueError:
                continue
            if isinstance(cand, dict) and ("details" in cand or "candidates" in cand):
                summary = cand  # last matching summary wins
    if not isinstance(summary, dict):
        print(json.dumps({"drained": 0, "reason": "bad_json"}))
        return 0

    details = summary.get("details") or []
    journal = _journal_path()
    op_logged, auto_logged = _existing(journal)
    # The emit window (Gap-18) straddles UTC midnight, so a call already drained
    # LATE YESTERDAY must not re-surface today. Union yesterday's journal into the
    # dedup sets (write still goes to today's journal).
    yo, ya = _existing(_journal_path_for(datetime.now().date() - timedelta(days=1)))
    op_logged |= yo
    auto_logged |= ya

    drained = 0
    skipped = []
    with open(journal, "a", encoding="utf-8", newline="\n") as fh:
        for d in details:
            pid = str(d.get("prospect_id") or "")
            if not pid:
                continue
            if pid in op_logged:
                skipped.append((d.get("company"), "operator_already_logged"))
                continue
            if pid in auto_logged:
                skipped.append((d.get("company"), "already_drained"))
                continue
            raw_outcome = str(d.get("outcome") or "")
            mapped = _OUTCOME_MAP.get(raw_outcome)
            if not mapped:
                skipped.append((d.get("company"), f"no_map_{raw_outcome}"))
                continue
            real = _REAL_OUTCOME_LABEL.get(raw_outcome)
            notes = (f"Samus autonomous dial: {real}" if real
                     else "Samus autonomous dial (auto-drain via reconcile)")
            fh.write(json.dumps({
                "ts": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
                "prospect_id": pid,
                "company": d.get("company") or "",
                "phone": d.get("phone") or "",
                "outcome": mapped,
                "notes": notes,
                "source": _SOURCE,
            }, ensure_ascii=False) + "\n")
            auto_logged.add(pid)
            drained += 1

    print(json.dumps({"drained": drained, "journal": journal, "skipped": skipped}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
