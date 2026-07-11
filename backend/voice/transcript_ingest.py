"""Parse TalkerACR .txt transcription files into normalized RawTranscript records.

TalkerACR filenames follow one of:
  20260623_143022_Outgoing_John_Smith_+15551234567.txt
  20260623_143022_Incoming_+15551234567.txt
  20260623_143022_Incoming_Unknown_+15551234567.txt

Transcription body formats supported:

  Format A — timestamped speaker labels (TalkerACR AI transcript):
    [0:00:00 - 0:00:05] You: Hi, this is Alex...
    [0:00:05 - 0:00:12] John Smith: Yeah, what is this about?

  Format B — bare speaker labels:
    [0:00] You: Hi, this is Alex...
    [0:15] Caller: Yeah, what is this about?

  Format C — plain text block (older / non-AI):
    Hi, this is Alex calling from HustleForge...
"""
from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

_LOG = logging.getLogger("samus.voice.transcript_ingest")

# Filename patterns
# Legacy TalkerACR text-export shape (.txt with embedded direction + contact)
_FILENAME_RE_LEGACY = re.compile(
    r"^(?P<date>\d{8})_(?P<time>\d{6})_"
    r"(?P<direction>Incoming|Outgoing|Unknown)"
    r"(?:_(?P<contact>.+?))?_"
    r"(?P<phone>[\+\d][\d\s\-().]{8,})\s*\.txt$",
    re.IGNORECASE,
)
# Current TalkerACR audio-recording shape: phone_YYYYMMDD-HHMMSS_PHONE(.txt|.amr)
_FILENAME_RE_PHONE = re.compile(
    r"^phone_(?P<date>\d{8})-(?P<time>\d{6})_(?P<phone>\d{7,15})"
    r"(?:\.[a-zA-Z0-9]+)*$"
)

# Speaker-label patterns
_SPEAKER_A_RE = re.compile(
    r"^\[(?P<start>[\d:]+)\s*-\s*[\d:]+\]\s+(?P<speaker>[^:]{1,40}):\s*(?P<text>.+)$"
)
_SPEAKER_B_RE = re.compile(
    r"^\[(?P<ts>[\d:]+)\]\s+(?P<speaker>[^:]{1,40}):\s*(?P<text>.+)$"
)


@dataclass
class TranscriptTurn:
    speaker: str
    text: str
    timestamp_offset: str = ""


@dataclass
class RawTranscript:
    source_file: str
    file_hash: str
    call_ts: datetime
    direction: str
    contact_phone: str
    contact_name: str
    raw_text: str
    turns: list[TranscriptTurn] = field(default_factory=list)
    parse_format: str = "unknown"


def parse_transcript_file(path: Path) -> RawTranscript | None:
    """Parse a TalkerACR .txt file. Returns None on unrecoverable failure."""
    try:
        raw_text = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError as exc:
        _LOG.warning("cannot read %s: %s", path, exc)
        return None

    file_hash = hashlib.sha256(raw_text.encode()).hexdigest()[:16]
    call_ts, direction, contact_phone, contact_name = _parse_filename(path.name)
    turns, parse_format = _parse_body(raw_text)

    return RawTranscript(
        source_file=path.name,
        file_hash=file_hash,
        call_ts=call_ts,
        direction=direction,
        contact_phone=contact_phone,
        contact_name=contact_name,
        raw_text=raw_text,
        turns=turns,
        parse_format=parse_format,
    )


def _parse_filename(filename: str) -> tuple[datetime, str, str, str]:
    # Strip stacked extensions (e.g. phone_2026....amr.txt) for matching.
    name_no_ext = re.sub(r"(?:\.[a-zA-Z0-9]+)+$", "", filename)

    m = _FILENAME_RE_LEGACY.match(filename)
    if m:
        try:
            date_str = m.group("date") + m.group("time")
            call_ts = datetime.strptime(date_str, "%Y%m%d%H%M%S").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            call_ts = datetime.now(timezone.utc)

        direction = m.group("direction") or "Unknown"
        phone_raw = m.group("phone") or ""
        phone = re.sub(r"[\s\-().]", "", phone_raw)
        contact = (m.group("contact") or "").replace("_", " ").strip()
        return call_ts, direction, phone, contact

    m2 = _FILENAME_RE_PHONE.match(name_no_ext)
    if m2:
        try:
            date_str = m2.group("date") + m2.group("time")
            call_ts = datetime.strptime(date_str, "%Y%m%d%H%M%S").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            call_ts = datetime.now(timezone.utc)
        phone = m2.group("phone") or ""
        return call_ts, "Unknown", phone, ""

    # Last-ditch: pull any YYYYMMDD[_-]HHMMSS substring
    date_m = re.search(r"(\d{8})[_-]?(\d{6})?", filename)
    if date_m:
        try:
            part = date_m.group(1) + (date_m.group(2) or "000000")
            ts = datetime.strptime(part, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
            return ts, "Unknown", "", ""
        except ValueError:
            pass

    return datetime.now(timezone.utc), "Unknown", "", ""


def _parse_body(raw_text: str) -> tuple[list[TranscriptTurn], str]:
    lines = [l.rstrip() for l in raw_text.splitlines() if l.strip()]

    # Try Format A (timestamped range + speaker)
    a_matches = [_SPEAKER_A_RE.match(l) for l in lines]
    if sum(1 for m in a_matches if m) >= 2:
        turns = []
        for line, m in zip(lines, a_matches):
            if m:
                turns.append(TranscriptTurn(
                    speaker=m.group("speaker").strip(),
                    text=m.group("text").strip(),
                    timestamp_offset=m.group("start"),
                ))
        return turns, "speaker_labeled_range"

    # Try Format B (bare timestamp + speaker)
    b_matches = [_SPEAKER_B_RE.match(l) for l in lines]
    if sum(1 for m in b_matches if m) >= 2:
        turns = []
        for line, m in zip(lines, b_matches):
            if m:
                turns.append(TranscriptTurn(
                    speaker=m.group("speaker").strip(),
                    text=m.group("text").strip(),
                    timestamp_offset=m.group("ts"),
                ))
        return turns, "speaker_labeled_bare"

    # Format C — plain block
    cleaned = " ".join(l.strip() for l in lines if l.strip())
    if cleaned:
        return [TranscriptTurn(speaker="transcript", text=cleaned[:8000])], "plain_text"

    return [], "empty"
