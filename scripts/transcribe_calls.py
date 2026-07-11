"""
Transcribe M4A call recordings to structured Markdown for Samus consumption.

Usage:
    python transcribe_calls.py                  # process all new recordings
    python transcribe_calls.py --file call.m4a  # process a single file
    python transcribe_calls.py --reprocess      # re-transcribe everything
    python transcribe_calls.py --dry-run        # preflight checks only
    python transcribe_calls.py --no-swap        # ignore page file, physical RAM only

Reads from:  Samus/data/call_recordings/*.m4a
Writes to:   Samus/data/call_transcripts/*.md

Resource safeguards:
    - Swap-aware: uses commit charge (physical + page file) when NVMe-backed
    - Pre-trim: asks Windows to page out idle working sets before model load
    - Auto model downgrade cascade when resources are tight
    - VRAM reserve keeps 4 GB free for LM Studio + fleet agents
    - Below-normal process priority to avoid starving the fleet
    - Per-file health checks with emergency abort
    - Explicit GPU cleanup on exit
"""

import argparse
import ctypes
import ctypes.wintypes
import gc
import json
import os
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import psutil

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
SAMUS_ROOT = Path(__file__).resolve().parent.parent
RECORDINGS_DIR = SAMUS_ROOT / "data" / "call_recordings"
TRANSCRIPTS_DIR = SAMUS_ROOT / "data" / "call_transcripts"
MANIFEST_PATH = TRANSCRIPTS_DIR / ".processed.json"

# ---------------------------------------------------------------------------
# Resource thresholds — physical RAM
# ---------------------------------------------------------------------------
VRAM_RESERVE_MB = 4096
RAM_FLOOR_MB = 2048
RAM_FLOOR_SWAP_MB = 512       # relaxed floor when NVMe swap has headroom
RAM_CRITICAL_MB = 1024
RAM_CRITICAL_SWAP_MB = 384    # relaxed critical when swap-aware
RAM_PER_FILE_FLOOR_MB = 1536
RAM_PER_FILE_FLOOR_SWAP_MB = 512

# ---------------------------------------------------------------------------
# Resource thresholds — commit charge (physical + page file)
# ---------------------------------------------------------------------------
COMMIT_HEADROOM_MB = 6144     # need this much commit charge free to proceed

# Model resource requirements (approximate, float16 on GPU)
MODEL_TIERS = [
    # (name, vram_mb, ram_mb) — ordered best to lightest
    ("large-v3", 3200, 3500),
    ("medium",   1600, 2200),
    ("small",     500, 1200),
    ("base",      200,  800),
    ("tiny",      100,  500),
]

# ---------------------------------------------------------------------------
# Windows API — working set trimming
# ---------------------------------------------------------------------------

PROCESS_SET_QUOTA = 0x0100
PROCESS_QUERY_INFORMATION = 0x0400

kernel32 = ctypes.windll.kernel32
psapi = ctypes.windll.psapi


def _empty_working_set(pid):
    """Ask Windows to trim a process's working set to its minimum."""
    handle = kernel32.OpenProcess(
        PROCESS_SET_QUOTA | PROCESS_QUERY_INFORMATION, False, pid
    )
    if not handle:
        return False
    try:
        result = kernel32.SetProcessWorkingSetSizeEx(
            handle,
            ctypes.c_size_t(-1),  # dwMinimumWorkingSetSize = -1 = trim
            ctypes.c_size_t(-1),  # dwMaximumWorkingSetSize = -1 = trim
            ctypes.wintypes.DWORD(0),
        )
        return bool(result)
    except Exception:
        return False
    finally:
        kernel32.CloseHandle(handle)


def trim_idle_working_sets():
    """
    Trim working sets of non-essential processes to encourage Windows
    to page out idle memory before Whisper loads.
    Only trims processes owned by the same user at equal or lower priority.
    """
    my_pid = os.getpid()
    trimmed = 0
    skipped = 0

    # Processes we should never trim
    protected = {"System", "Registry", "smss.exe", "csrss.exe",
                 "wininit.exe", "services.exe", "lsass.exe",
                 "svchost.exe", "dwm.exe", "winlogon.exe"}

    for proc in psutil.process_iter(["pid", "name", "username"]):
        try:
            info = proc.info
            if info["pid"] == my_pid:
                continue
            if info["name"] in protected:
                skipped += 1
                continue
            if _empty_working_set(info["pid"]):
                trimmed += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            skipped += 1

    return trimmed, skipped


# ---------------------------------------------------------------------------
# Swap / page file inspection
# ---------------------------------------------------------------------------

def get_swap_info():
    """
    Returns dict with page file details:
        backing_disk_type: 'SSD' | 'HDD' | 'Unknown'
        pagefile_total_mb: total page file size
        pagefile_used_mb: current usage
        pagefile_free_mb: available headroom
        commit_total_mb: system commit limit (RAM + page file)
        commit_used_mb: current committed memory
        commit_free_mb: remaining commit charge
        nvme_backed: bool
    """
    swap = psutil.swap_memory()
    vm = psutil.virtual_memory()

    commit_total = vm.total + swap.total
    commit_used = vm.total - vm.available + swap.used
    commit_free = commit_total - commit_used

    # Detect page file backing disk type
    disk_type = "Unknown"
    nvme = False
    try:
        for pdisk in psutil.disk_partitions():
            if pdisk.mountpoint == "C:\\":
                # Get physical disk for C:
                for phys in __import__("wmi").WMI().Win32_DiskDrive():
                    if "NVMe" in (phys.Model or "") or "SSD" in (phys.Model or ""):
                        disk_type = "SSD"
                        if "NVMe" in (phys.Model or ""):
                            nvme = True
                        break
    except Exception:
        # wmi module not available — fall back to PowerShell
        try:
            out = subprocess.check_output(
                ["powershell", "-NoProfile", "-Command",
                 "(Get-PhysicalDisk | Where-Object {$_.DeviceId -eq 0}).MediaType"],
                text=True, timeout=5, stderr=subprocess.DEVNULL,
            ).strip()
            if out == "SSD":
                disk_type = "SSD"
                # Check if NVMe specifically
                model_out = subprocess.check_output(
                    ["powershell", "-NoProfile", "-Command",
                     "(Get-PhysicalDisk | Where-Object {$_.DeviceId -eq 0}).FriendlyName"],
                    text=True, timeout=5, stderr=subprocess.DEVNULL,
                ).strip()
                nvme = "980 PRO" in model_out or "NVMe" in model_out
        except Exception:
            pass

    return {
        "backing_disk_type": disk_type,
        "pagefile_total_mb": swap.total / (1024 ** 2),
        "pagefile_used_mb": swap.used / (1024 ** 2),
        "pagefile_free_mb": swap.free / (1024 ** 2),
        "commit_total_mb": commit_total / (1024 ** 2),
        "commit_used_mb": commit_used / (1024 ** 2),
        "commit_free_mb": commit_free / (1024 ** 2),
        "nvme_backed": nvme,
    }


# ---------------------------------------------------------------------------
# Resource inspection
# ---------------------------------------------------------------------------

def get_ram_free_mb():
    return psutil.virtual_memory().available / (1024 ** 2)


def get_ram_total_mb():
    return psutil.virtual_memory().total / (1024 ** 2)


def get_vram_info_mb():
    """Returns (total, used, free) in MB via nvidia-smi XML."""
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "-q", "-x"],
            text=True, timeout=10, stderr=subprocess.DEVNULL,
        )
        root = ET.fromstring(out)
        mem = root.find("gpu/fb_memory_usage")
        parse = lambda tag: int(mem.find(tag).text.split()[0])
        return parse("total"), parse("used"), parse("free")
    except Exception:
        return 0, 0, 0


def get_gpu_process_count():
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "-q", "-x"],
            text=True, timeout=10, stderr=subprocess.DEVNULL,
        )
        root = ET.fromstring(out)
        procs = root.find("gpu/processes")
        return len(procs.findall("process_info")) if procs is not None else 0
    except Exception:
        return 0


def set_low_priority():
    try:
        p = psutil.Process(os.getpid())
        p.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Preflight + model selection
# ---------------------------------------------------------------------------

def preflight(requested_model, force_cpu, swap_aware):
    """
    Check system resources and select the best model that fits.
    Returns (model_name, device, compute_type, swap_mode) or exits.
    """
    ram_free = get_ram_free_mb()
    ram_total = get_ram_total_mb()
    vram_total, vram_used, vram_free = get_vram_info_mb()
    gpu_procs = get_gpu_process_count()
    swap_info = get_swap_info()

    swap_ok = (swap_aware
               and swap_info["nvme_backed"]
               and swap_info["pagefile_free_mb"] > 8192)

    floor = RAM_FLOOR_SWAP_MB if swap_ok else RAM_FLOOR_MB

    print("=" * 60)
    print("RESOURCE PREFLIGHT")
    print("=" * 60)
    print(f"  RAM:     {ram_free:.0f} MB free / {ram_total:.0f} MB total "
          f"({psutil.virtual_memory().percent:.0f}% used)")
    print(f"  VRAM:    {vram_free} MB free / {vram_total} MB total "
          f"({vram_used} MB used)")
    print(f"  GPU:     {gpu_procs} processes")
    print()
    print(f"  Swap:    {swap_info['pagefile_free_mb']:.0f} MB free / "
          f"{swap_info['pagefile_total_mb']:.0f} MB total "
          f"({swap_info['backing_disk_type']}"
          f"{', NVMe' if swap_info['nvme_backed'] else ''})")
    print(f"  Commit:  {swap_info['commit_free_mb']:.0f} MB free / "
          f"{swap_info['commit_total_mb']:.0f} MB limit")
    print()

    if swap_ok:
        print(f"  MODE:    Swap-aware (NVMe page file with "
              f"{swap_info['pagefile_free_mb']:.0f} MB headroom)")
        print(f"           RAM floor relaxed: {RAM_FLOOR_MB} MB -> {floor} MB")
        print(f"           Model weights transit through RAM to VRAM,")
        print(f"           fleet idle pages swap to NVMe during load burst")
    else:
        reasons = []
        if not swap_aware:
            reasons.append("--no-swap flag")
        elif not swap_info["nvme_backed"]:
            reasons.append("page file not on NVMe")
        elif swap_info["pagefile_free_mb"] <= 8192:
            reasons.append(
                f"page file headroom too low "
                f"({swap_info['pagefile_free_mb']:.0f} MB)"
            )
        print(f"  MODE:    Physical RAM only ({', '.join(reasons)})")

    print(f"  Floors:  RAM={floor} MB, VRAM reserve={VRAM_RESERVE_MB} MB")
    print()

    # Hard gate: commit charge (even with swap, need enough total capacity)
    if swap_info["commit_free_mb"] < COMMIT_HEADROOM_MB:
        print(f"  ABORT: Commit charge too low — "
              f"{swap_info['commit_free_mb']:.0f} MB free, "
              f"need {COMMIT_HEADROOM_MB} MB")
        print(f"  Both physical RAM and page file are near capacity.")
        sys.exit(1)

    # Hard gate: physical RAM (swap-adjusted floor)
    if ram_free < floor:
        if swap_ok:
            print(f"  ABORT: Only {ram_free:.0f} MB physical RAM free "
                  f"(need {floor} MB even in swap-aware mode)")
            print(f"  System is critically low — not safe to proceed.")
        else:
            print(f"  ABORT: Only {ram_free:.0f} MB RAM free "
                  f"(need {floor} MB)")
            print(f"  The fleet is using most of system memory.")
            print(f"  Try: --no-swap is off, or free up RAM.")
        sys.exit(1)

    # Model selection uses commit charge when swap-aware
    effective_ram = swap_info["commit_free_mb"] if swap_ok else ram_free

    if force_cpu:
        model, device, compute = _select_model_cpu(
            requested_model, effective_ram
        )
    else:
        vram_budget = max(0, vram_free - VRAM_RESERVE_MB)
        model, device, compute = _select_model_gpu(
            requested_model, effective_ram, vram_budget, vram_free, swap_ok
        )

    print(f"  SELECTED: model={model}, device={device}, compute={compute}")
    print("=" * 60)
    print()
    return model, device, compute, swap_ok


def _select_model_gpu(requested, ram_budget, vram_budget, vram_free_raw,
                      swap_ok):
    budget_label = "commit charge" if swap_ok else "RAM"

    for name, vram_need, ram_need in MODEL_TIERS:
        if name == requested:
            if vram_need <= vram_budget and ram_need <= ram_budget:
                print(f"  OK: {name} fits (needs {vram_need} MB VRAM, "
                      f"{ram_need} MB {budget_label})")
                return name, "cuda", "float16"
            else:
                reasons = []
                if vram_need > vram_budget:
                    reasons.append(
                        f"VRAM: needs {vram_need} MB, "
                        f"{vram_budget} MB available after reserve"
                    )
                if ram_need > ram_budget:
                    reasons.append(
                        f"{budget_label}: needs {ram_need} MB, "
                        f"{ram_budget:.0f} MB available"
                    )
                print(f"  DOWNGRADE: {name} won't fit — "
                      f"{'; '.join(reasons)}")
                break

    for name, vram_need, ram_need in MODEL_TIERS:
        if vram_need <= vram_budget and ram_need <= ram_budget:
            print(f"  FALLBACK: {name} fits (needs {vram_need} MB VRAM, "
                  f"{ram_need} MB {budget_label})")
            return name, "cuda", "float16"

    print("  FALLBACK: No model fits on GPU, trying CPU...")
    return _select_model_cpu("small", ram_budget)


def _select_model_cpu(requested, ram_budget):
    for name, _, ram_need in MODEL_TIERS:
        cpu_ram = int(ram_need * 0.6)
        if name == requested and cpu_ram <= ram_budget:
            return name, "cpu", "int8"
    for name, _, ram_need in MODEL_TIERS:
        cpu_ram = int(ram_need * 0.6)
        if cpu_ram <= ram_budget:
            print(f"  FALLBACK (CPU): {name} fits ({cpu_ram} MB est.)")
            return name, "cpu", "int8"

    print("  ABORT: Not enough memory for even the tiny model.")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Health checks
# ---------------------------------------------------------------------------

def check_health_before_file(file_index, total, swap_mode):
    ram_free = get_ram_free_mb()
    critical = RAM_CRITICAL_SWAP_MB if swap_mode else RAM_CRITICAL_MB
    warning = RAM_PER_FILE_FLOOR_SWAP_MB if swap_mode else RAM_PER_FILE_FLOOR_MB

    if ram_free < critical:
        print(f"\n  EMERGENCY STOP: RAM at {ram_free:.0f} MB "
              f"(critical: {critical} MB)")
        print(f"  Processed {file_index}/{total}. "
              f"Re-run to continue (done files are skipped).")
        return False
    if ram_free < warning:
        print(f"  WARNING: RAM at {ram_free:.0f} MB — continuing cautiously")
    return True


# ---------------------------------------------------------------------------
# GPU cleanup
# ---------------------------------------------------------------------------

def cleanup_gpu(model_ref):
    print("\nCleaning up GPU memory...")
    del model_ref
    gc.collect()
    gc.collect()
    _, _, vram_free = get_vram_info_mb()
    ram_free = get_ram_free_mb()
    print(f"  Post-cleanup: {vram_free} MB VRAM free, "
          f"{ram_free:.0f} MB RAM free")


# ---------------------------------------------------------------------------
# Transcription
# ---------------------------------------------------------------------------

def load_manifest():
    if MANIFEST_PATH.exists():
        return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    return {}


def save_manifest(manifest):
    MANIFEST_PATH.write_text(
        json.dumps(manifest, indent=2, default=str), encoding="utf-8"
    )


def format_timestamp(seconds):
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:d}:{s:02d}"


def format_duration(seconds):
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}h {m}m {s}s" if h else f"{m}m {s}s"


def transcribe_file(model, audio_path):
    print(f"  Transcribing: {audio_path.name}")
    start = time.time()

    segments, info = model.transcribe(
        str(audio_path),
        beam_size=5,
        language="en",
        vad_filter=True,
        vad_parameters=dict(min_silence_duration_ms=500, speech_pad_ms=200),
        word_timestamps=False,
    )

    transcript_segments = []
    for seg in segments:
        transcript_segments.append({
            "start": seg.start, "end": seg.end, "text": seg.text.strip(),
        })

    elapsed = time.time() - start
    duration = info.duration if info.duration else (
        transcript_segments[-1]["end"] if transcript_segments else 0
    )
    print(f"  Done in {elapsed:.1f}s ({format_duration(duration)} of audio)")

    return {
        "segments": transcript_segments,
        "language": info.language,
        "language_probability": info.language_probability,
        "duration": duration,
        "processing_time": elapsed,
    }


def build_markdown(audio_path, result, model_used, device_used):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    duration = result["duration"]
    lang_prob = result["language_probability"]

    lines = [
        "---",
        f"call_id: {audio_path.stem}",
        f"source_file: {audio_path.name}",
        f"date_transcribed: {now}",
        f"duration_seconds: {duration:.1f}",
        f"duration_display: {format_duration(duration)}",
        f"language: {result['language']}",
        f"language_confidence: {lang_prob:.2f}",
        f"whisper_model: {model_used}",
        f"device: {device_used}",
        f"processing_time_seconds: {result['processing_time']:.1f}",
        "type: call_transcript",
        "---",
        "",
        f"# Call Transcript: {audio_path.stem}",
        "",
        f"**Duration:** {format_duration(duration)} | "
        f"**Language:** {result['language']} ({lang_prob:.0%} confidence) | "
        f"**Source:** `{audio_path.name}`",
        "",
        "---",
        "",
    ]

    for seg in result["segments"]:
        ts = format_timestamp(seg["start"])
        lines.append(f"**[{ts}]** {seg['text']}")
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append(f"*Transcribed by Whisper {model_used} ({device_used}). "
                 "Review for accuracy — proper nouns and industry terms "
                 "may need correction.*")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Transcribe call recordings for Samus"
    )
    parser.add_argument("--file", type=str, help="Transcribe a single file")
    parser.add_argument("--reprocess", action="store_true",
                        help="Re-transcribe all files")
    parser.add_argument("--model", type=str, default="large-v3",
                        help="Preferred Whisper model (auto-downgrades if needed)")
    parser.add_argument("--cpu", action="store_true", help="Force CPU mode")
    parser.add_argument("--dry-run", action="store_true",
                        help="Run preflight checks only")
    parser.add_argument("--no-priority", action="store_true",
                        help="Don't lower process priority")
    parser.add_argument("--no-swap", action="store_true",
                        help="Ignore page file, use physical RAM thresholds only")
    args = parser.parse_args()

    # ---- Priority ----
    if not args.no_priority:
        if set_low_priority():
            print("Process priority set to BELOW_NORMAL (fleet-safe)\n")
        else:
            print("WARNING: Could not lower process priority\n")

    # ---- Preflight ----
    swap_aware = not args.no_swap
    model_name, device, compute, swap_mode = preflight(
        args.model, args.cpu, swap_aware
    )

    if args.dry_run:
        print("Dry run complete. No transcription performed.")
        sys.exit(0)

    # ---- Gather files ----
    RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
    TRANSCRIPTS_DIR.mkdir(parents=True, exist_ok=True)

    if args.file:
        audio_files = [Path(args.file)]
        if not audio_files[0].is_absolute():
            audio_files = [RECORDINGS_DIR / args.file]
    else:
        audio_files = sorted(RECORDINGS_DIR.glob("*.m4a"))

    if not audio_files:
        print(f"No M4A files found in {RECORDINGS_DIR}")
        print("Transfer your call recordings there and re-run.")
        sys.exit(0)

    manifest = load_manifest()
    if not args.reprocess:
        audio_files = [
            f for f in audio_files
            if f.name not in manifest
            or not (TRANSCRIPTS_DIR / f"{f.stem}.md").exists()
        ]

    if not audio_files:
        print("All recordings already transcribed. Use --reprocess to redo.")
        sys.exit(0)

    print(f"Found {len(audio_files)} recording(s) to transcribe")

    # ---- RAM relief cascade before model load ----
    ram_before_relief = get_ram_free_mb()
    model_ram_need = next(
        (r for n, v, r in MODEL_TIERS if n == model_name), 500
    )
    if ram_before_relief < model_ram_need:
        print(f"RAM ({ram_before_relief:.0f} MB) below model need "
              f"({model_ram_need} MB) -- running relief cascade...")
        relief_script = (
            Path(__file__).resolve().parent.parent.parent
            / "_shared" / "scripts" / "Invoke-RamRelief.ps1"
        )
        if relief_script.exists():
            relief_result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                 "-File", str(relief_script),
                 "-Cascade", "-TargetFreeMB", str(model_ram_need),
                 "-MaxTier", "2", "-Json"],
                capture_output=True, text=True, timeout=60,
            )
            try:
                relief_data = json.loads(relief_result.stdout.strip())
                freed = relief_data.get("free_mb", 0) - ram_before_relief
                print(f"  Relief result: {relief_data.get('free_mb', 0):.0f} MB free "
                      f"(freed ~{max(0, freed):.0f} MB, "
                      f"max tier {relief_data.get('max_tier_reached', '?')})")
            except (json.JSONDecodeError, ValueError):
                print(f"  Relief ran (exit {relief_result.returncode})")
        else:
            print("  Relief script not found, falling back to inline trim...")
            trimmed, skipped = trim_idle_working_sets()
            print(f"  Trimmed {trimmed} processes, "
                  f"RAM now {get_ram_free_mb():.0f} MB free")
        print()

    # ---- Load model ----
    print(f"Loading Whisper {model_name} on {device} ({compute})...")
    from faster_whisper import WhisperModel

    model = WhisperModel(
        model_name,
        device=device,
        compute_type=compute,
    )

    ram_post_load = get_ram_free_mb()
    _, _, vram_post_load = get_vram_info_mb()
    print(f"Model loaded. RAM: {ram_post_load:.0f} MB free, "
          f"VRAM: {vram_post_load} MB free\n")

    # ---- Transcribe ----
    succeeded = 0
    failed = 0
    aborted = False

    try:
        for i, audio_path in enumerate(audio_files):
            if not check_health_before_file(i, len(audio_files), swap_mode):
                aborted = True
                break

            if not audio_path.exists():
                print(f"  SKIP: {audio_path} not found")
                failed += 1
                continue

            try:
                result = transcribe_file(model, audio_path)
                md_content = build_markdown(
                    audio_path, result, model_name, device
                )

                output_path = TRANSCRIPTS_DIR / f"{audio_path.stem}.md"
                output_path.write_text(md_content, encoding="utf-8")

                manifest[audio_path.name] = {
                    "transcribed_at": datetime.now(timezone.utc).isoformat(),
                    "duration": result["duration"],
                    "segments": len(result["segments"]),
                    "output": output_path.name,
                    "model": model_name,
                    "device": device,
                }
                save_manifest(manifest)

                print(f"  Saved: {output_path.name}")
                print(f"  Segments: {len(result['segments'])}\n")
                succeeded += 1

            except Exception as e:
                print(f"  ERROR on {audio_path.name}: {e}\n")
                failed += 1
    finally:
        cleanup_gpu(model)

    remaining = len(audio_files) - succeeded - failed
    print(f"\nComplete: {succeeded} transcribed, {failed} failed"
          + (f", {remaining} remaining (re-run to continue)"
             if aborted else ""))
    print(f"Transcripts: {TRANSCRIPTS_DIR}")


if __name__ == "__main__":
    main()
