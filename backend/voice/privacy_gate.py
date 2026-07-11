"""Privacy gate for the TalkerACR transcript pipeline.

Every transcript file must pass this gate before any analysis runs.
A call is auto-approved only when its phone number matches a known
prospect in the local call-list CSVs. Everything else lands in a
pending queue for operator review — personal calls are never analyzed.

Gate decisions:
  PROSPECT_MATCHED  phone found in prospect CSVs → auto-proceed with context
  PENDING_REVIEW    phone unknown → held for operator classification
  APPROVED          operator explicitly approved as a business call
  PERSONAL          operator marked as personal → skip forever, archive locally

Pending queue persists at <artifact_root>/voice/pending_review.json.
Operator endpoints:
  GET  /voice/pending_transcripts        — list all pending entries
  POST /voice/classify_transcript        — approve or mark personal
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path

from backend.common import storage
from backend.common.dates import iso_now

from .prospect_lookup import ProspectRecord, lookup_by_phone
from .transcript_ingest import RawTranscript

_LOG = logging.getLogger("samus.voice.privacy_gate")

_PENDING_FILE = "voice/pending_review.json"
_PERSONAL_SUBDIR = "voice/personal"
_STAGING_SUBDIR = "voice/transcript_staging"

_lock = threading.Lock()


class GateDecision(str, Enum):
    PROSPECT_MATCHED = "prospect_matched"
    PENDING_REVIEW = "pending_review"
    APPROVED = "approved"
    PERSONAL = "personal"
    ALREADY_ANALYZED = "already_analyzed"


@dataclass
class PendingEntry:
    filename: str
    phone: str
    contact_name: str
    call_ts_iso: str
    direction: str
    staged_at: str
    decision: str
    prospect_id: str | None = None
    company_name: str | None = None
    operator_note: str | None = None


# ---------------------------------------------------------------------------
# Main gate
# ---------------------------------------------------------------------------


def classify(
    raw: RawTranscript,
    *,
    phone_index: dict[str, "ProspectRecord"] | None = None,
) -> tuple[GateDecision, ProspectRecord | None]:
    """Classify a transcript for pipeline eligibility.

    Args:
        raw:         Parsed transcript from transcript_ingest.
        phone_index: Pre-built phone→ProspectRecord index (batch efficiency).
                     When None, falls back to per-call CSV scan.

    Returns:
        (decision, prospect)  — prospect is None unless PROSPECT_MATCHED or APPROVED.
    """
    # Already in the pending queue — respect prior operator decision
    entry = _load_entry(raw.source_file)
    if entry:
        if entry.decision == GateDecision.PERSONAL:
            return GateDecision.PERSONAL, None
        if entry.decision == GateDecision.APPROVED:
            prospect = None
            if entry.prospect_id:
                prospect = lookup_by_phone(raw.contact_phone)
            return GateDecision.APPROVED, prospect

    # Phone lookup
    if raw.contact_phone:
        if phone_index is not None:
            from .prospect_lookup import _norm10

            prospect = phone_index.get(_norm10(raw.contact_phone))
        else:
            prospect = lookup_by_phone(raw.contact_phone)

        if prospect:
            return GateDecision.PROSPECT_MATCHED, prospect

    # No match — queue for operator review
    _upsert_pending(raw, decision=GateDecision.PENDING_REVIEW)
    _LOG.info(
        "privacy_gate: %s queued for review (phone=%s contact=%s)",
        raw.source_file,
        raw.contact_phone or "unknown",
        raw.contact_name or "unknown",
    )
    return GateDecision.PENDING_REVIEW, None


# ---------------------------------------------------------------------------
# Operator classification
# ---------------------------------------------------------------------------


def mark_approved(
    filename: str,
    *,
    prospect_id: str | None = None,
    company_name: str | None = None,
    operator_note: str | None = None,
) -> bool:
    """Operator approves an unmatched call as a business call.

    Returns True if the entry was found and updated.
    """
    pending = _load_all()
    for entry in pending:
        if entry.filename == filename:
            entry.decision = GateDecision.APPROVED
            entry.prospect_id = prospect_id
            entry.company_name = company_name
            entry.operator_note = operator_note
            _save_all(pending)
            _LOG.info(
                "privacy_gate: %s approved by operator (prospect_id=%s)", filename, prospect_id
            )
            return True
    # Entry may not exist yet (called before pipeline ran it)
    new_entry = PendingEntry(
        filename=filename,
        phone="",
        contact_name="",
        call_ts_iso="",
        direction="",
        staged_at=iso_now(),
        decision=GateDecision.APPROVED,
        prospect_id=prospect_id,
        company_name=company_name,
        operator_note=operator_note,
    )
    pending.append(new_entry)
    _save_all(pending)
    return True


def mark_personal(filename: str, *, operator_note: str | None = None) -> bool:
    """Operator marks a call as personal — archive file, block from analysis forever."""
    pending = _load_all()
    updated = False
    for entry in pending:
        if entry.filename == filename:
            entry.decision = GateDecision.PERSONAL
            entry.operator_note = operator_note
            updated = True
            break

    if not updated:
        pending.append(
            PendingEntry(
                filename=filename,
                phone="",
                contact_name="",
                call_ts_iso="",
                direction="",
                staged_at=iso_now(),
                decision=GateDecision.PERSONAL,
                operator_note=operator_note,
            )
        )
    _save_all(pending)

    # Move file out of staging so it can never be re-staged
    _archive_personal(filename)
    _LOG.info("privacy_gate: %s marked personal by operator", filename)
    return True


def list_pending() -> list[PendingEntry]:
    """Return all entries with decision=pending_review."""
    return [e for e in _load_all() if e.decision == GateDecision.PENDING_REVIEW]


def list_all() -> list[PendingEntry]:
    """Return all entries in the review queue (all decisions)."""
    return _load_all()


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------


def _queue_path() -> Path:
    target = storage.root() / _PENDING_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


def _load_all() -> list[PendingEntry]:
    path = _queue_path()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            return []
        return [PendingEntry(**item) for item in data if isinstance(item, dict)]
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("privacy_gate: failed to load pending queue: %s", exc)
        return []


def _save_all(entries: list[PendingEntry]) -> None:
    path = _queue_path()
    with _lock:
        try:
            path.write_text(
                json.dumps([asdict(e) for e in entries], indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError as exc:
            _LOG.warning("privacy_gate: failed to save pending queue: %s", exc)


def _load_entry(filename: str) -> PendingEntry | None:
    for e in _load_all():
        if e.filename == filename:
            return e
    return None


def _upsert_pending(raw: RawTranscript, *, decision: GateDecision) -> None:
    pending = _load_all()
    for entry in pending:
        if entry.filename == raw.source_file:
            return  # already in queue
    pending.append(
        PendingEntry(
            filename=raw.source_file,
            phone=raw.contact_phone,
            contact_name=raw.contact_name,
            call_ts_iso=raw.call_ts.isoformat(),
            direction=raw.direction,
            staged_at=iso_now(),
            decision=decision,
        )
    )
    _save_all(pending)


def _archive_personal(filename: str) -> None:
    """Move a personal file out of staging to the personal archive."""
    staging = storage.root() / _STAGING_SUBDIR / filename
    archive_dir = storage.root() / _PERSONAL_SUBDIR
    try:
        archive_dir.mkdir(parents=True, exist_ok=True)
        if staging.exists():
            staging.rename(archive_dir / filename)
    except OSError as exc:
        _LOG.warning("privacy_gate: failed to archive personal file %s: %s", filename, exc)
