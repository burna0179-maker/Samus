"""Local audio transcription using faster-whisper (no internet, no API).

TalkerACR records each call as an AMR audio file with no extension. After
Sync-TranscriptFiles.ps1 lands them in staging with a .amr suffix, this
module decodes them via pyav (amrnb/amrwb codecs are bundled with pyav)
and runs Whisper inference locally on CPU.

Default model is "base" (~140MB download on first use, cached under
%USERPROFILE%\.cache\huggingface). For better accuracy on noisy calls,
set SAMUS_WHISPER_MODEL=small (~460MB) or "medium" (~1.5GB).

Outputs are .txt sidecar files next to each .amr in the same staging
folder so the existing parse_transcript_file() picks them up.

Compute type: int8 by default for CPU speed. Override with
SAMUS_WHISPER_COMPUTE=int8_float32 / float32 for higher accuracy.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

_LOG = logging.getLogger("samus.voice.audio_transcribe")

_MODEL_NAME = os.getenv("SAMUS_WHISPER_MODEL", "base")
_COMPUTE_TYPE = os.getenv("SAMUS_WHISPER_COMPUTE", "int8")
_DEVICE = os.getenv("SAMUS_WHISPER_DEVICE", "cpu")
_LANGUAGE = os.getenv("SAMUS_WHISPER_LANG", "en")

_AUDIO_EXTENSIONS = {".amr", ".m4a", ".mp3", ".wav", ".ogg", ".aac", ".3gp"}

_model_cache: object | None = None


def _get_model():
    """Lazy-load the Whisper model. First call downloads if not cached."""
    global _model_cache
    if _model_cache is not None:
        return _model_cache

    from faster_whisper import WhisperModel

    _LOG.info(
        "audio_transcribe: loading whisper model=%s compute=%s device=%s",
        _MODEL_NAME, _COMPUTE_TYPE, _DEVICE,
    )
    _model_cache = WhisperModel(
        _MODEL_NAME,
        device=_DEVICE,
        compute_type=_COMPUTE_TYPE,
    )
    return _model_cache


def transcribe_audio(audio_path: Path, *, force: bool = False) -> Path | None:
    """Transcribe one audio file → write sidecar .txt with same basename.

    Returns the sidecar Path on success, or None on failure.
    Skips transcription if the sidecar already exists unless force=True.
    """
    if not audio_path.exists():
        _LOG.warning("audio_transcribe: file missing: %s", audio_path)
        return None

    sidecar = audio_path.with_suffix(".txt")
    if sidecar.exists() and not force:
        return sidecar

    try:
        model = _get_model()
    except Exception as exc:  # noqa: BLE001
        _LOG.error("audio_transcribe: model load failed: %s", exc)
        return None

    try:
        segments, info = model.transcribe(
            str(audio_path),
            language=_LANGUAGE,
            beam_size=5,
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 500},
        )
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("audio_transcribe: %s — transcribe failed: %s", audio_path.name, exc)
        return None

    lines: list[str] = []
    try:
        for seg in segments:
            start = _fmt_ts(seg.start)
            end = _fmt_ts(seg.end)
            text = (seg.text or "").strip()
            if text:
                lines.append(f"[{start} - {end}] transcript: {text}")
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("audio_transcribe: %s — segment iter failed: %s", audio_path.name, exc)
        return None

    if not lines:
        _LOG.info("audio_transcribe: %s — empty (silent or unintelligible)", audio_path.name)
        try:
            sidecar.write_text("", encoding="utf-8")
        except OSError:
            pass
        return sidecar

    try:
        sidecar.write_text("\n".join(lines), encoding="utf-8")
        _LOG.info(
            "audio_transcribe: %s — %d segments, %.1fs audio, lang=%s",
            audio_path.name, len(lines), info.duration, info.language,
        )
        return sidecar
    except OSError as exc:
        _LOG.warning("audio_transcribe: sidecar write failed %s: %s", sidecar, exc)
        return None


def transcribe_pending(staging_dir: Path) -> dict:
    """Transcribe every audio file in staging that lacks a .txt sidecar.

    Returns a summary dict: {transcribed, skipped, errors}.
    """
    transcribed = 0
    skipped = 0
    errors: list[str] = []

    if not staging_dir.exists():
        return {"transcribed": 0, "skipped": 0, "errors": ["staging_dir_missing"]}

    audio_files: list[Path] = []
    for ext in _AUDIO_EXTENSIONS:
        audio_files.extend(staging_dir.glob(f"*{ext}"))

    for audio in sorted(audio_files):
        sidecar = audio.with_suffix(".txt")
        if sidecar.exists():
            skipped += 1
            continue

        result = transcribe_audio(audio)
        if result is None:
            errors.append(f"transcribe_failed: {audio.name}")
        else:
            transcribed += 1

    return {
        "transcribed": transcribed,
        "skipped": skipped,
        "errors": errors,
        "model": _MODEL_NAME,
    }


def _fmt_ts(seconds: float) -> str:
    """Format seconds as H:MM:SS."""
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}"
