"""Pull recently-completed Vapi call transcripts into the transcript pipeline.

WHY THIS EXISTS (dead middle of the voice -> UCB1 bandit learning loop, 2026-07-07)
-----------------------------------------------------------------------------------
The Tranche-3 learning loop is coded end-to-end but its MIDDLE leg was dead:

  * FRONT (works): the dialer stamps a ``variant_arm_id`` on every placed call
    and records a dispatch row (:mod:`backend.voice.arm_stamp`).
  * REWARD (works): :func:`backend.voice.transcript_analyzer.analyze_transcript`
    computes a [0,1] reward from the call outcome and flows it back to the UCB1
    bandit via :func:`backend.attribution.engine.record_outcome`.
  * MIDDLE (was dead): ``analyze_transcript`` only runs inside
    :func:`backend.voice.ingest_pipeline.run_ingest_pipeline`, which ingests
    files from a local STAGING directory. Nothing pulled the completed Vapi
    calls' transcripts into that staging directory, so the analyzer never ran on
    cold-call transcripts, the reward never flowed, and the bandit never learned.

    Root cause is the same as the cold-dial outage: the host cron that drove the
    post-call sweep was removed 2026-07-03 (``operator:remove-host-crons``) and
    nothing in-container replaced it. Calls completed, the webhook wrote
    ``end_of_call`` (200 OK), but the transcript was never analyzed.

THE FIX (this module + :mod:`backend.gateway.voice_ingest_task`)
----------------------------------------------------------------
Make Vapi a SOURCE for the EXISTING transcript pipeline instead of building a
parallel analyzer. This module pulls recently-completed Vapi calls (read-only
``GET /call`` via :class:`backend.voice.client.VapiClient`) and STAGES each
transcript as a ``.txt`` file in the same ``voice/transcript_staging`` directory
:func:`run_ingest_pipeline` already reads. The gateway ingest cadence then runs
that pipeline over the newly-staged files -> ``analyze_transcript`` ->
``record_outcome``. One pipeline, one analyzer, one reward path.

The staged filename uses the TalkerACR legacy convention
(``YYYYMMDD_HHMMSS_Outgoing_<contact>_<phone>.txt``) so
:func:`backend.voice.transcript_ingest.parse_transcript_file` parses it and the
prospect ``phone`` survives to :func:`backend.voice.arm_stamp.lookup_arm` (the
reward join key). The prospect itself is resolved from the daily call-list CSVs
by :mod:`backend.voice.privacy_gate` exactly as for a hand-synced call.

Dedup is by Vapi ``call_id``: an append-only ledger
(``voice/vapi_ingested_calls.jsonl``) records every staged call so a re-pull
never re-stages it -> the reward flows EXACTLY ONCE per call (belt-and-braces
with the pipeline's own file-hash dedup).

Everything here is fail-soft: a Vapi error, an unreadable ledger, or a bad row
degrades to a skip + a populated summary dict. Ingest is an optimization; it
must never raise into the gateway loop that drives it.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from backend.common import storage
from backend.common.dates import iso_now

_LOG = logging.getLogger("samus.voice.vapi_transcript_ingest")

# Same staging dir the offline TalkerACR pipeline uses -- Vapi is just another
# source feeding it, NOT a parallel staging area.
_STAGING_SUBDIR = "voice/transcript_staging"
_MANIFEST_SUBDIR = "voice"
_MANIFEST_NAME = "transcript_manifest.json"
# Append-only call_id dedup ledger (one JSON row per staged call).
_INGESTED_LEDGER = "voice/vapi_ingested_calls.jsonl"

_DEFAULT_LIMIT = 100  # Vapi list_calls clamps to [1, 100]
_DEFAULT_LOOKBACK_HOURS = 72  # bound a fresh-boot backfill; dedup handles repeats

# Vapi transcript filename phone group must satisfy transcript_ingest's
# _FILENAME_RE_LEGACY: a leading '+' or digit then 8+ of [digit/space/-/(/)].


# ---------------------------------------------------------------------------
# Vapi call field access (tolerates a VapiCall model OR a plain dict row)
# ---------------------------------------------------------------------------


def _g(call: Any, name: str, default: Any = None) -> Any:
    if isinstance(call, dict):
        return call.get(name, default)
    return getattr(call, name, default)


def _call_phone(call: Any) -> str:
    """The dialed customer number off a Vapi call, or "" when absent."""
    customer = _g(call, "customer")
    if isinstance(customer, dict):
        return str(customer.get("number") or "")
    if customer is not None:
        return str(getattr(customer, "number", "") or "")
    return ""


def _call_company(call: Any) -> str:
    customer = _g(call, "customer")
    if isinstance(customer, dict):
        return str(customer.get("name") or "")
    if customer is not None:
        return str(getattr(customer, "name", "") or "")
    return ""


def _parse_iso(value: Any) -> Optional[datetime]:
    """Parse a Vapi ISO-8601 timestamp to an aware datetime, else None."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None


def _call_ts(call: Any, now: datetime) -> datetime:
    """Best call timestamp: startedAt -> createdAt -> endedAt -> now."""
    for key in ("startedAt", "createdAt", "endedAt"):
        ts = _parse_iso(_g(call, key))
        if ts is not None:
            return ts
    return now


# ---------------------------------------------------------------------------
# Staged-filename construction (round-trips through transcript_ingest)
# ---------------------------------------------------------------------------


def _phone_for_filename(phone: str) -> str:
    """Keep only a leading '+' and digits -- the shape the legacy filename regex
    accepts, and the shape ``_norm10`` collapses to the CSV join key."""
    kept = [c for c in (phone or "") if c == "+" or c.isdigit()]
    return "".join(kept)


def _safe_contact(company: str, call_id: str) -> str:
    """A NON-EMPTY, underscore-free contact token for the filename.

    Alphanumerics only (spaces/punct/underscores stripped) so the single '_'
    delimiter before the phone can never be ambiguous, and never empty (an empty
    token would produce a '__' that the legacy regex can't parse -- which would
    drop the phone and break the reward join). Falls back to the call_id tail,
    then a literal 'vapi'.
    """
    token = re.sub(r"[^A-Za-z0-9]", "", company or "")[:24]
    if token:
        return token
    token = re.sub(r"[^A-Za-z0-9]", "", call_id or "")[-8:]
    return token or "vapi"


def _staged_filename(call: Any, ts: datetime, phone: str) -> str:
    """``YYYYMMDD_HHMMSS_Outgoing_<contact>_<phone>.txt`` -- a TalkerACR legacy
    name so :func:`transcript_ingest.parse_transcript_file` recovers direction +
    phone. Cold dials are always OUTBOUND."""
    contact = _safe_contact(_call_company(call), str(_g(call, "id", "") or ""))
    stamp = ts.astimezone(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return f"{stamp}_Outgoing_{contact}_{_phone_for_filename(phone)}.txt"


# ---------------------------------------------------------------------------
# Paths + dedup ledger
# ---------------------------------------------------------------------------


def _staging_dir() -> Path:
    p = storage.root() / _STAGING_SUBDIR
    p.mkdir(parents=True, exist_ok=True)
    return p


def _manifest_path() -> Path:
    p = storage.root() / _MANIFEST_SUBDIR
    p.mkdir(parents=True, exist_ok=True)
    return p / _MANIFEST_NAME


def _ingested_ledger_path() -> Path:
    return storage.root() / _INGESTED_LEDGER


def _load_ingested_call_ids() -> set[str]:
    """Read the set of already-staged Vapi call_ids. Fail-soft -> empty set."""
    path = _ingested_ledger_path()
    if not path.exists():
        return set()
    out: set[str] = set()
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            cid = str(row.get("call_id") or "")
            if cid:
                out.add(cid)
    except OSError as exc:
        _LOG.warning("vapi ingest: dedup ledger read failed: %s", exc)
    return out


def _append_ingested(call_id: str, filename: str) -> None:
    """Append one dedup row. Fail-soft -- a ledger write miss at worst re-stages
    a call next pass (the pipeline's file-hash dedup still prevents re-reward)."""
    path = _ingested_ledger_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(
                json.dumps(
                    {"ts": iso_now(), "call_id": call_id, "staged_file": filename},
                    ensure_ascii=False,
                )
                + "\n"
            )
    except OSError as exc:
        _LOG.warning("vapi ingest: dedup ledger append failed for %s: %s", call_id, exc)


def _freshen_manifest(summary: dict[str, Any]) -> None:
    """Overwrite the transcript_manifest.json freshness marker with this pass.

    Nothing in-code parses specific keys off this file -- it is an observability
    / freshness artifact (the acceptance signal that ingest is running). We tag
    the source so a reader can tell an in-container Vapi pull from the (legacy)
    host MTP sync. Fail-soft."""
    try:
        payload = {
            "ts": iso_now(),
            "source": "vapi_in_container_ingest",
            "last_pull": summary.get("ts"),
            "pulled": summary.get("pulled", 0),
            "eligible": summary.get("eligible", 0),
            "staged": summary.get("staged", 0),
            "skipped_already_staged": summary.get("skipped_already", 0),
            "skipped_no_transcript": summary.get("skipped_no_transcript", 0),
            "skipped_no_phone": summary.get("skipped_no_phone", 0),
            "skipped_not_ended": summary.get("skipped_not_ended", 0),
            "skipped_outside_lookback": summary.get("skipped_old", 0),
            "error": summary.get("error"),
        }
        _manifest_path().write_text(
            json.dumps(payload, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError as exc:
        _LOG.warning("vapi ingest: manifest freshen failed: %s", exc)


# ---------------------------------------------------------------------------
# Vapi client (read-only) -- same idiom as reconcile / call_batch_analyzer
# ---------------------------------------------------------------------------


def _build_client() -> Any | None:
    """A read-only :class:`VapiClient` from settings, or None when the API key
    is unset (nothing to pull -- a clean no-op, never an error)."""
    try:
        from backend.common.config import get_settings
        from .client import VapiClient

        api_key = (getattr(get_settings(), "vapi_api_key", "") or "").strip()
        if not api_key:
            return None
        return VapiClient(api_key)
    except Exception as exc:  # noqa: BLE001 -- a client build fault is a no-op pull
        _LOG.warning("vapi ingest: client build failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# The pull -- stage recently-completed Vapi transcripts for the pipeline
# ---------------------------------------------------------------------------


def pull_and_stage_recent(
    *,
    client: Any = None,
    now: Optional[datetime] = None,
    limit: int = _DEFAULT_LIMIT,
    lookback_hours: int = _DEFAULT_LOOKBACK_HOURS,
) -> dict[str, Any]:
    """Pull recent completed Vapi calls and stage their transcripts as ``.txt``.

    Read-only Vapi (``GET /call?limit=N``). Stages one file per newly-completed
    call whose transcript is present, dedup'd by ``call_id``. Freshens the
    transcript manifest every pass (even a no-op pull, so the freshness signal
    stays current). Never raises -- all faults land in the returned summary.

    ``client`` is an injectable Vapi client (tests pass a fake exposing
    ``list_calls``); production builds one from settings. ``now`` pins the
    lookback window (tests). Returns a summary dict; ``staged`` is the count of
    files newly written -- the driver only runs the (heavier) ingest pipeline
    when this is > 0.
    """
    now = now or datetime.now(timezone.utc)
    summary: dict[str, Any] = {
        "ts": iso_now(),
        "pulled": 0,
        "eligible": 0,
        "staged": 0,
        "skipped_already": 0,
        "skipped_no_transcript": 0,
        "skipped_no_phone": 0,
        "skipped_not_ended": 0,
        "skipped_old": 0,
        "staged_files": [],
        "error": None,
    }

    client = client or _build_client()
    if client is None:
        summary["error"] = "no_vapi_key"
        _freshen_manifest(summary)
        return summary

    try:
        calls = client.list_calls(limit=max(1, min(100, int(limit))))
    except Exception as exc:  # noqa: BLE001 -- a Vapi fault is a skipped pass
        _LOG.warning("vapi ingest: list_calls failed: %s", exc)
        summary["error"] = f"vapi_list_failed: {exc}"
        _freshen_manifest(summary)
        return summary

    calls = list(calls or [])
    summary["pulled"] = len(calls)
    ingested = _load_ingested_call_ids()
    cutoff = now - timedelta(hours=max(0, int(lookback_hours)))
    staging = _staging_dir()

    for call in calls:
        call_id = str(_g(call, "id", "") or "")
        if not call_id:
            continue
        if str(_g(call, "status", "") or "").lower() != "ended":
            summary["skipped_not_ended"] += 1
            continue
        transcript = str(_g(call, "transcript", "") or "")
        if not transcript.strip():
            summary["skipped_no_transcript"] += 1
            continue
        phone = _call_phone(call)
        if not _phone_for_filename(phone):
            summary["skipped_no_phone"] += 1
            continue
        ts = _call_ts(call, now)
        if ts < cutoff:
            summary["skipped_old"] += 1
            continue

        summary["eligible"] += 1
        if call_id in ingested:
            summary["skipped_already"] += 1
            continue

        filename = _staged_filename(call, ts, phone)
        try:
            (staging / filename).write_text(transcript, encoding="utf-8")
        except OSError as exc:
            _LOG.warning("vapi ingest: stage write failed for %s: %s", call_id, exc)
            continue

        _append_ingested(call_id, filename)
        ingested.add(call_id)
        summary["staged"] += 1
        summary["staged_files"].append(filename)
        _LOG.info(
            "vapi ingest: staged transcript call=%s -> %s",
            call_id,
            filename,
        )

    _freshen_manifest(summary)
    return summary


__all__ = [
    "pull_and_stage_recent",
]
