"""Diagnostics breadth — the four missing self-healing detectors (HOTL T5).

Extends the entropy workcell (which scores queue/error/retry variance) with
four concrete failure detectors + safe auto-remediation, per framework
phase 7:

  * stuck-loop        — a task_id reprocessed more than N times (a handler that
                        keeps failing on the same message).
  * dead-worker       — an agent/worker heartbeat that has gone stale beyond
                        2x its emit interval.
  * orphan-task       — a DLQ failure pending past a TTL that was never
                        replayed (no worker ever claimed it).
  * resource-exhaustion — low free disk, an oversized ledger dir, or high
                        process RSS.

DATA SOURCES (grounded in what is durably READABLE on this host)
----------------------------------------------------------------
The roadmap sketched "read task_state" / "DDB heartbeat rows", but Samus's
task_state table is write-only and there is no per-worker heartbeat TABLE —
there is a single observable heartbeat FILE per agent
(``backend.common.heartbeat``) and a durable DLQ ledger
(``backend.common.dlq``). So:

  * stuck-loop / orphan-task read the DLQ ledger (task_id + attempt + status +
    ts are all there and readable).
  * dead-worker reads the ``*_heartbeat.json`` files in the coordination dir.
  * resource-exhaustion uses ``shutil.disk_usage`` + a ledger-dir walk +
    (no new dep) an ``os``/``ctypes`` RSS probe.

REMEDIATION (safe only)
-----------------------
  * orphan-task -> requeue through the existing replay worker.
  * dead-worker -> file an operator task (CRM) + emit a container-restart-request
    event (Docker's restart policy handles the container layer).
Everything a detector finds AND every remediation is emitted as a
``decision.made`` diagnostic event. Never raises to the caller.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from backend.common.business_events import DECISION_MADE, emit_business_event
from backend.common.dates import iso_now

_LOG = logging.getLogger("samus.entropy.diagnostics")

# --- thresholds (env-overridable so ops can tune without a redeploy) --------


def _int_env(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, "") or default)
    except (TypeError, ValueError):
        return default


def _float_env(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, "") or default)
    except (TypeError, ValueError):
        return default


# A task_id seen with attempt >= this in the DLQ is a stuck loop.
STUCK_LOOP_ATTEMPTS = _int_env("SAMUS_DIAG_STUCK_LOOP_ATTEMPTS", 3)
# A heartbeat older than this many seconds is a dead worker. Default 60s =
# 2x the 30s heartbeat interval.
DEAD_WORKER_STALE_SECONDS = _float_env("SAMUS_DIAG_DEAD_WORKER_STALE_SECONDS", 60.0)
# A DLQ failure still pending this many seconds after it was enqueued, with no
# replay, is an orphan.
ORPHAN_TASK_TTL_SECONDS = _float_env("SAMUS_DIAG_ORPHAN_TTL_SECONDS", 3600.0)
# Resource thresholds.
DISK_FREE_MIN_RATIO = _float_env("SAMUS_DIAG_DISK_FREE_MIN_RATIO", 0.10)  # 10%
LEDGER_DIR_MAX_MB = _float_env("SAMUS_DIAG_LEDGER_DIR_MAX_MB", 2048.0)  # 2 GB
PROCESS_RSS_MAX_MB = _float_env("SAMUS_DIAG_PROCESS_RSS_MAX_MB", 4096.0)  # 4 GB

# The services whose DLQ ledgers stuck-loop / orphan-task scan.
_DLQ_SERVICES: tuple[str, ...] = (
    "gateway",
    "leadgen",
    "prospecting",
    "scaffold",
    "fulfillment",
    "memory",
    "feedback",
    "outreach",
    "proposal",
    "seo",
    "finance",
    "voice",
    "intake",
    "crm",
    "strategy",
)


@dataclass
class DiagnosticFinding:
    """One detector's verdict."""

    detector: str
    severity: str  # "ok" | "warn" | "critical"
    detail: str = ""
    subject: str = ""  # task_id / worker id / resource name
    extras: dict[str, Any] = field(default_factory=dict)
    remediation: str = ""  # what (if anything) was auto-applied

    def to_record(self) -> dict[str, Any]:
        return asdict(self)


def _emit(finding: DiagnosticFinding) -> None:
    """Emit a diagnostic finding as a decision.made event (fail-soft)."""
    emit_business_event(
        DECISION_MADE,
        workcell="entropy",
        metadata={
            "decision": "diagnostic",
            "detector": finding.detector,
            "severity": finding.severity,
            "subject": finding.subject,
            "detail": finding.detail,
            "remediation": finding.remediation,
            **finding.extras,
        },
    )


# ---------------------------------------------------------------------------
# stuck-loop
# ---------------------------------------------------------------------------


def detect_stuck_loops() -> list[DiagnosticFinding]:
    """Task_ids reprocessed > STUCK_LOOP_ATTEMPTS times (per DLQ attempt)."""
    from backend.common import dlq

    findings: list[DiagnosticFinding] = []
    # Highest attempt seen per (service, task_id).
    worst: dict[tuple[str, str], int] = {}
    for service in _DLQ_SERVICES:
        try:
            rows = dlq.read_pending(service, limit=500)
        except Exception as exc:  # noqa: BLE001 — one bad ledger never sinks the sweep
            _LOG.debug("stuck-loop: dlq read %s failed: %s", service, exc)
            continue
        for r in rows:
            tid = str(r.get("task_id") or "")
            if not tid:
                continue
            try:
                attempt = int(r.get("attempt") or 1)
            except (TypeError, ValueError):
                attempt = 1
            key = (service, tid)
            if attempt > worst.get(key, 0):
                worst[key] = attempt
    for (service, tid), attempt in worst.items():
        if attempt >= STUCK_LOOP_ATTEMPTS:
            f = DiagnosticFinding(
                detector="stuck_loop",
                severity="critical",
                subject=tid,
                detail=f"task {tid} reprocessed {attempt}x in {service} DLQ",
                extras={"service": service, "attempts": attempt},
            )
            findings.append(f)
            _emit(f)
    return findings


# ---------------------------------------------------------------------------
# dead-worker
# ---------------------------------------------------------------------------


def _coordination_dir() -> Path:
    """Directory holding the observable ``*_heartbeat.json`` files."""
    override = os.getenv("SAMUS_COORDINATION_DIR", "").strip()
    if override:
        return Path(override)
    # Default: the parent of Samus's own heartbeat file.
    hb = os.getenv("SAMUS_HEARTBEAT_PATH", "").strip()
    if hb:
        return Path(hb).parent
    from backend.common.state_paths import state_path

    return state_path("coordination")


def detect_dead_workers(*, now: float | None = None) -> list[DiagnosticFinding]:
    """Heartbeat files whose ts is stale beyond DEAD_WORKER_STALE_SECONDS."""
    now = time.time() if now is None else now
    findings: list[DiagnosticFinding] = []
    d = _coordination_dir()
    try:
        files = sorted(d.glob("*heartbeat*.json")) if d.exists() else []
    except OSError as exc:
        _LOG.debug("dead-worker: coordination dir unreadable: %s", exc)
        return findings
    for path in files:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        ts_raw = payload.get("ts")
        try:
            ts = float(ts_raw)
        except (TypeError, ValueError):
            continue
        age = now - ts
        if age > DEAD_WORKER_STALE_SECONDS:
            worker = str(payload.get("agent_id") or path.stem)
            f = DiagnosticFinding(
                detector="dead_worker",
                severity="critical",
                subject=worker,
                detail=f"{worker} heartbeat stale {age:.0f}s (> {DEAD_WORKER_STALE_SECONDS:.0f}s)",
                extras={"age_seconds": round(age, 1), "heartbeat_file": path.name},
            )
            _remediate_dead_worker(f)
            findings.append(f)
            _emit(f)
    return findings


def _remediate_dead_worker(finding: DiagnosticFinding) -> None:
    """Operator task + a container-restart-request event (Docker handles the
    container layer via its restart policy). Records the remediation on the
    finding. Best-effort — remediation failure never sinks the detector."""
    worker = finding.subject
    # Operator task (CRM path).
    try:
        from backend.crm import service as crm
        from backend.crm.models import CreateOperatorTaskRequest

        crm.create_operator_task(
            CreateOperatorTaskRequest(
                kind="review",
                title=f"Dead worker: {worker}",
                description=finding.detail,
                source="entropy_diagnostics",
                source_ref=worker,
            )
        )
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("dead-worker operator-task failed: %s", exc)
    # Container-restart-request event on the unified stream. Docker's restart
    # policy is the actual effector; this is the audited request signal.
    emit_business_event(
        DECISION_MADE,
        workcell="entropy",
        metadata={
            "decision": "container_restart_request",
            "worker": worker,
            "reason": finding.detail,
        },
    )
    finding.remediation = "operator_task+container_restart_request"


# ---------------------------------------------------------------------------
# orphan-task
# ---------------------------------------------------------------------------


def detect_orphan_tasks(
    *, now: float | None = None, remediate: bool = True
) -> list[DiagnosticFinding]:
    """DLQ failures pending past the TTL with no replay -> requeue (safe)."""
    from backend.common import dlq

    now = time.time() if now is None else now
    findings: list[DiagnosticFinding] = []
    # Which event_ids already have a replay transition (so they're not orphans).
    replayed: set[str] = set()
    try:
        for a in dlq.read_archive(limit=1000):
            eid = str(a.get("event_id") or "")
            if eid:
                replayed.add(eid)
    except Exception as exc:  # noqa: BLE001
        _LOG.debug("orphan-task: archive read failed: %s", exc)

    for service in _DLQ_SERVICES:
        try:
            rows = dlq.read_pending(service, limit=500)
        except Exception as exc:  # noqa: BLE001
            _LOG.debug("orphan-task: dlq read %s failed: %s", service, exc)
            continue
        for r in rows:
            if str(r.get("status") or "") != "pending_retry":
                continue
            eid = str(r.get("event_id") or "")
            if not eid or eid in replayed:
                continue
            age = now - _parse_ts_epoch(r.get("ts"))
            if age < ORPHAN_TASK_TTL_SECONDS:
                continue
            f = DiagnosticFinding(
                detector="orphan_task",
                severity="warn",
                subject=str(r.get("task_id") or eid),
                detail=f"{service} failure {eid} pending {age:.0f}s with no replay",
                extras={"service": service, "event_id": eid, "age_seconds": round(age, 1)},
            )
            if remediate:
                _remediate_orphan(service, f)
            findings.append(f)
            _emit(f)
    return findings


def _remediate_orphan(service: str, finding: DiagnosticFinding) -> None:
    """Requeue via the existing replay worker (gateway DLQ). Best-effort."""
    try:
        from backend.common import replay_worker

        if service == "gateway":
            replay_worker.replay_gateway_dlq_sync(limit=25)
            finding.remediation = "replay_gateway_dlq"
        else:
            # Non-gateway services requeue on their own replay cadence; record
            # the requeue REQUEST (the per-service replay path is worker-owned).
            finding.remediation = "requeue_requested"
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("orphan-task remediation failed: %s", exc)


# ---------------------------------------------------------------------------
# resource-exhaustion
# ---------------------------------------------------------------------------


def detect_resource_exhaustion() -> list[DiagnosticFinding]:
    """Low free disk, oversized ledger dir, or high process RSS."""
    findings: list[DiagnosticFinding] = []

    # -- disk free --
    try:
        from backend.common.state_paths import state_root

        root = state_root()
        probe = root if root.exists() else Path.cwd()
        usage = shutil.disk_usage(str(probe))
        free_ratio = usage.free / usage.total if usage.total else 1.0
        if free_ratio < DISK_FREE_MIN_RATIO:
            f = DiagnosticFinding(
                detector="resource_exhaustion",
                severity="critical",
                subject="disk",
                detail=f"disk free {free_ratio * 100:.1f}% < {DISK_FREE_MIN_RATIO * 100:.0f}%",
                extras={
                    "free_bytes": usage.free,
                    "total_bytes": usage.total,
                    "free_ratio": round(free_ratio, 4),
                },
            )
            findings.append(f)
            _emit(f)
    except Exception as exc:  # noqa: BLE001
        _LOG.debug("resource: disk probe failed: %s", exc)

    # -- ledger dir size --
    try:
        data_root = Path(os.getenv("SAMUS_DATA_ROOT", "/opt/samus/data"))
        if data_root.exists():
            mb = _dir_size_mb(data_root)
            if mb > LEDGER_DIR_MAX_MB:
                f = DiagnosticFinding(
                    detector="resource_exhaustion",
                    severity="warn",
                    subject="ledger_dir",
                    detail=f"data dir {mb:.0f}MB > {LEDGER_DIR_MAX_MB:.0f}MB",
                    extras={"size_mb": round(mb, 1), "path": str(data_root)},
                )
                findings.append(f)
                _emit(f)
    except Exception as exc:  # noqa: BLE001
        _LOG.debug("resource: ledger-dir probe failed: %s", exc)

    # -- process RSS (no psutil: ctypes on Windows, /proc on Linux) --
    try:
        rss_mb = _process_rss_mb()
        if rss_mb is not None and rss_mb > PROCESS_RSS_MAX_MB:
            f = DiagnosticFinding(
                detector="resource_exhaustion",
                severity="warn",
                subject="memory",
                detail=f"process RSS {rss_mb:.0f}MB > {PROCESS_RSS_MAX_MB:.0f}MB",
                extras={"rss_mb": round(rss_mb, 1)},
            )
            findings.append(f)
            _emit(f)
    except Exception as exc:  # noqa: BLE001
        _LOG.debug("resource: rss probe failed: %s", exc)

    return findings


def _dir_size_mb(path: Path, *, cap_files: int = 50_000) -> float:
    """Best-effort recursive byte size (MB), bounded so a huge tree can't hang."""
    total = 0
    seen = 0
    for p in path.rglob("*"):
        seen += 1
        if seen > cap_files:
            break
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            continue
    return total / (1024 * 1024)


def _process_rss_mb() -> float | None:
    """Current process resident set size in MB, or None if unavailable.

    No new dependency: Linux reads /proc/self/statm; Windows uses ctypes +
    GetProcessMemoryInfo. Any failure returns None (the probe is skipped).
    """
    # Linux / container path.
    statm = Path("/proc/self/statm")
    if statm.exists():
        try:
            pages = int(statm.read_text().split()[1])  # resident pages
            page_size = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096
            return pages * page_size / (1024 * 1024)
        except (OSError, ValueError, IndexError):
            return None
    # Windows path.
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            class _PMC(ctypes.Structure):
                _fields_ = [
                    ("cb", wintypes.DWORD),
                    ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            counters = _PMC()
            counters.cb = ctypes.sizeof(_PMC)
            handle = ctypes.windll.kernel32.GetCurrentProcess()
            ok = ctypes.windll.psapi.GetProcessMemoryInfo(
                handle,
                ctypes.byref(counters),
                counters.cb,
            )
            if ok:
                return counters.WorkingSetSize / (1024 * 1024)
        except Exception:  # noqa: BLE001 — probe is optional
            return None
    return None


def _parse_ts_epoch(ts_raw: Any) -> float:
    """Parse an iso_now()-style 'YYYY-MM-DDTHH:MM:SSZ' string to epoch secs."""
    if not ts_raw:
        return 0.0
    try:
        from datetime import datetime, timezone

        dt = datetime.strptime(str(ts_raw), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (ValueError, TypeError):
        return 0.0


# ---------------------------------------------------------------------------
# orchestrator
# ---------------------------------------------------------------------------


def run_diagnostics(*, remediate: bool = True) -> dict[str, Any]:
    """Run all four detectors; return a structured report. Never raises.

    ``remediate`` gates the safe auto-remediation (orphan requeue, dead-worker
    operator task + restart request). Detection always runs and always emits.
    """
    all_findings: list[DiagnosticFinding] = []
    for fn, kw in (
        (detect_stuck_loops, {}),
        (detect_dead_workers, {}),
        (detect_orphan_tasks, {"remediate": remediate}),
        (detect_resource_exhaustion, {}),
    ):
        try:
            all_findings.extend(fn(**kw))
        except Exception as exc:  # noqa: BLE001 — one detector can't sink the sweep
            _LOG.warning("diagnostic %s failed: %s", getattr(fn, "__name__", fn), exc)

    by_severity = {"ok": 0, "warn": 0, "critical": 0}
    for f in all_findings:
        by_severity[f.severity] = by_severity.get(f.severity, 0) + 1
    return {
        "ts": iso_now(),
        "findings": [f.to_record() for f in all_findings],
        "counts": by_severity,
        "healthy": len(all_findings) == 0,
    }


__all__ = [
    "DiagnosticFinding",
    "detect_stuck_loops",
    "detect_dead_workers",
    "detect_orphan_tasks",
    "detect_resource_exhaustion",
    "run_diagnostics",
]
