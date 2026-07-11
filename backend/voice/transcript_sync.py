"""Pull new TalkerACR transcription files from Z Flip5 MTP to local staging.

Invokes Sync-TranscriptFiles.ps1 via subprocess. Returns a dict with the
list of newly staged .txt paths. Never raises — all errors land in the
result dict under "error" so the ingest pipeline stays non-fatal.
"""
from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

from backend.common import storage

_LOG = logging.getLogger("samus.voice.transcript_sync")

_SCRIPT_PATH = Path(r"D:\Hustleforge\_shared\scripts\Sync-TranscriptFiles.ps1")
_STAGING_SUBDIR = "voice/transcript_staging"
_MANIFEST_SUBDIR = "voice"
_MANIFEST_NAME = "transcript_manifest.json"

# Same dual-root fallback as prospect_lookup — agent works under either env.
_FALLBACK_ROOTS = (
    Path(r"D:\Hustleforge\Samus\.data\host_artifacts"),
    Path(r"E:\Hustleforge\Samus\data\artifacts"),
)


def _candidate_staging_dirs() -> list[Path]:
    """Return all existing staging directories that hold transcripts."""
    seen: list[Path] = []
    primary = storage.root() / _STAGING_SUBDIR
    if primary.exists():
        seen.append(primary)
    for root in _FALLBACK_ROOTS:
        d = root / _STAGING_SUBDIR
        if d.exists() and not any(d.samefile(s) for s in seen):
            seen.append(d)
    return seen


def sync_transcripts(
    *,
    device_name: str = "Hustle's Z Flip5",
    phone_sub_path: str = r"Internal storage\Documents\TalkerACR\All",
    script_path: Path | None = None,
) -> dict:
    """Copy new TalkerACR .txt files from the phone to local staging.

    Returns:
        {
            "copied":      [str, ...],   absolute paths of newly staged files
            "skipped":     int,          already-seen files skipped
            "staging_dir": str,
            "errors":      [str, ...],   per-file copy errors
            "error":       str | None,   fatal error (device not found, etc.)
        }
    """
    script = script_path or _SCRIPT_PATH
    if not script.exists():
        msg = f"sync script not found: {script}"
        _LOG.error(msg)
        return {"copied": [], "skipped": 0, "staging_dir": "", "errors": [], "error": msg}

    staging_dir = str(storage.root() / _STAGING_SUBDIR)
    manifest_file = str(storage.root() / _MANIFEST_SUBDIR / _MANIFEST_NAME)

    cmd = [
        "powershell.exe",
        "-NonInteractive",
        "-ExecutionPolicy", "Bypass",
        "-File", str(script),
        "-StagingDir", staging_dir,
        "-ManifestFile", manifest_file,
        "-DeviceName", device_name,
        "-PhoneSubPath", phone_sub_path,
    ]

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=180,
        )
        if proc.returncode != 0:
            _LOG.warning(
                "transcript sync script exited %d stderr=%s",
                proc.returncode, (proc.stderr or "")[:300],
            )

        stdout = (proc.stdout or "").strip()
        # Script writes a single -Compress JSON line to stdout
        for line in reversed(stdout.splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    return json.loads(line)
                except json.JSONDecodeError:
                    continue

        _LOG.warning("transcript sync: no JSON in stdout: %s", stdout[:200])
        return {
            "copied": [], "skipped": 0,
            "staging_dir": staging_dir, "errors": [],
            "error": "no_json_output",
        }

    except subprocess.TimeoutExpired:
        _LOG.error("transcript sync timed out after 180s")
        return {
            "copied": [], "skipped": 0,
            "staging_dir": staging_dir, "errors": [],
            "error": "timeout",
        }
    except Exception as exc:  # noqa: BLE001
        _LOG.error("transcript sync unexpected: %s", exc)
        return {
            "copied": [], "skipped": 0,
            "staging_dir": staging_dir, "errors": [],
            "error": str(exc),
        }


def staging_dir() -> Path:
    """Return the absolute staging directory (created on demand)."""
    p = storage.root() / _STAGING_SUBDIR
    p.mkdir(parents=True, exist_ok=True)
    return p


def list_staged_text() -> list[Path]:
    """Return all .txt transcript files in staging, sorted by filename."""
    staging = storage.root() / _STAGING_SUBDIR
    if not staging.exists():
        return []
    return sorted(staging.glob("*.txt"))


def list_staged_audio() -> list[Path]:
    """Return all audio files in staging that don't have a sidecar transcript yet."""
    staging = storage.root() / _STAGING_SUBDIR
    if not staging.exists():
        return []
    out: list[Path] = []
    for ext in (".amr", ".m4a", ".mp3", ".wav", ".ogg", ".3gp"):
        for p in staging.glob(f"*{ext}"):
            if not p.with_suffix(".txt").exists():
                out.append(p)
    return sorted(out)


# Back-compat — older callers expect .txt-only
list_staged_files = list_staged_text
