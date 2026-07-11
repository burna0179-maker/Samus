"""Inbound-call artifact persistence — recording, transcript, voicemail, record.

Each inbound receptionist call gets a directory
``<SAMUS_ARTIFACT_ROOT>/customers/<slug>/calls/<call_id>/`` holding:

  - ``recording.mp3``   — call audio (downloaded from Vapi's expiring URL)
  - ``transcript.txt``  — full transcript text
  - ``voicemail.mp3``   — after-hours / unanswered message audio (when present)
  - ``call.json``       — the normalized :class:`InboundCallRecord`, the
                          portal-facing record of the call

Recording download is best-effort: Vapi's ``recordingUrl`` is time-limited,
so a fetch failure is logged and the corresponding ``*_path`` left empty —
the transcript + ``call.json`` still land. Nothing here raises; a storage
failure must never fail the inbound webhook.
"""
from __future__ import annotations

import logging
from pathlib import Path

import httpx

from backend.common import storage
from backend.common.dates import iso_now

from .models import InboundCallRecord


_LOG = logging.getLogger("samus.voice.inbound_storage")

_DOWNLOAD_TIMEOUT_S = 30.0


def call_dir(slug: str, call_id: str) -> Path:
    """Directory holding one inbound call's artifacts.

    Defense-in-depth (CWE-22): the resolved directory must stay contained
    under the artifacts root. ``call_id`` is already charset-validated by the
    caller in :mod:`backend.voice.inbound`; this assertion catches any
    traversal in ``slug`` or ``call_id`` that slipped past upstream checks.
    """
    root = storage.root().resolve()
    cdir = (root / "customers" / slug / "calls" / call_id).resolve()
    if not cdir.is_relative_to(root):
        raise ValueError(
            f"call artifact path escapes the artifacts root: "
            f"slug={slug!r} call_id={call_id!r}"
        )
    return cdir


def _download(url: str, dest: Path) -> bool:
    """Fetch ``url`` to ``dest``. Returns True on success; never raises."""
    try:
        with httpx.Client(timeout=_DOWNLOAD_TIMEOUT_S) as client:
            resp = client.get(url)
    except httpx.HTTPError as exc:
        _LOG.warning("inbound artifact download failed (%s): %s", url, exc)
        return False
    if resp.status_code >= 400:
        _LOG.warning("inbound artifact download HTTP %s: %s", resp.status_code, url)
        return False
    try:
        dest.write_bytes(resp.content)
    except OSError as exc:
        _LOG.warning("inbound artifact write failed (%s): %s", dest, exc)
        return False
    return True


def persist_inbound_call(
    record: InboundCallRecord,
    *,
    transcript: str = "",
    recording_url: str = "",
    voicemail_url: str = "",
) -> InboundCallRecord:
    """Write the call's artifacts + ``call.json``; return the record with paths set.

    Never raises. On partial failure the affected ``*_path`` stays empty and
    ``call.json`` still records everything that did land — the call.json is
    written last so it reflects the final artifact state.
    """
    try:
        cdir = call_dir(record.customer_slug, record.call_id)
        cdir.mkdir(parents=True, exist_ok=True)
    except (OSError, ValueError) as exc:
        _LOG.warning("inbound call dir create failed for %s: %s", record.call_id, exc)
        return record

    if transcript:
        try:
            (cdir / "transcript.txt").write_text(transcript, encoding="utf-8")
            record.transcript_path = str(cdir / "transcript.txt")
        except OSError as exc:
            _LOG.warning("transcript write failed for %s: %s", record.call_id, exc)

    if recording_url and _download(recording_url, cdir / "recording.mp3"):
        record.recording_path = str(cdir / "recording.mp3")

    if voicemail_url and _download(voicemail_url, cdir / "voicemail.mp3"):
        record.voicemail_path = str(cdir / "voicemail.mp3")

    record.written_at = iso_now()
    try:
        (cdir / "call.json").write_text(
            record.model_dump_json(indent=2), encoding="utf-8",
        )
    except OSError as exc:
        _LOG.warning("call.json write failed for %s: %s", record.call_id, exc)
    return record
