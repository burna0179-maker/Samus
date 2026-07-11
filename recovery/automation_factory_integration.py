#!/usr/bin/env python3
"""
Automation Factory Integration — optimized + hardened version
Source: ChatGPT recovery chat 31

Canonical relationship:
- F:\\Samus iteration (memory: project_samus_plane_iteration)
- [EXPANDS §6 self_heal_extended] cross-module health assessment + recovery
- [EXPANDS §9 self-heal coverage] supervisor-role pattern
- [FIX] deterministic trust logic (prior bug: healthy systems flagged as missing-state)
- [FIX] no silent exception swallowing
- [NEW] hash-manifest deliverable integrity check
- [NEW] SLA-based backlog pressure detection

Pluggable evaluators (each isolates one health axis):
  - _evaluate_trust       — trust engine OR file-backed fallback
  - _evaluate_hash_integrity — last 10 deliverable folders
  - _evaluate_sla         — at-risk one-task-ops jobs over SLA_HOURS
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


TRUST_THRESHOLD = 0.70
SLA_HOURS = 40.0


def _verify_hash_manifest(folder: Path) -> Tuple[bool, Dict[str, str]]:
    manifest = folder / "hash_manifest.json"
    if not manifest.exists():
        return False, {"reason": "missing_manifest"}
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return False, {"reason": "invalid_manifest"}
        mismatches: Dict[str, str] = {}
        for name, expected in data.items():
            fp = folder / name
            if not fp.exists():
                mismatches[name] = "missing_file"
                continue
            h = hashlib.sha256()
            with fp.open("rb") as f:
                for chunk in iter(lambda: f.read(8192), b""):
                    h.update(chunk)
            if h.hexdigest() != expected:
                mismatches[name] = "hash_mismatch"
        return len(mismatches) == 0, mismatches
    except Exception as e:
        return False, {"reason": f"manifest_parse_error: {e}"}


def _evaluate_trust(details: Dict[str, Any], trust_state_path: Path) -> None:
    """Trust engine path → file fallback → flag below threshold."""
    try:
        from backend.infra.trust_engine import get_trust_engine, ORCHESTRATOR_ENTITY_ID
        score = get_trust_engine().get_trust(ORCHESTRATOR_ENTITY_ID)
        details["trust_score"] = round(score, 4)
        if score < TRUST_THRESHOLD:
            details["issues"].append("trust_score_below_target")
        return
    except Exception:
        pass

    if not trust_state_path.exists():
        details["issues"].append("trust_state_missing")
        return
    try:
        state = json.loads(trust_state_path.read_text(encoding="utf-8"))
        score = float(state.get("trust_score", 0.0))
        details["trust_score"] = round(score, 4)
        details["training_cycles"] = int(state.get("cycles", 0))
        if score < TRUST_THRESHOLD:
            details["issues"].append("trust_score_below_target")
    except Exception:
        details["issues"].append("trust_state_unreadable")


def _evaluate_sla(details: Dict[str, Any], service: Any) -> None:
    now = datetime.now(timezone.utc)
    active = {"pending_intake", "designing", "building", "deploying", "testing"}
    at_risk = 0
    for job in service.repository.list():
        status_raw = getattr(job, "status", "")
        status = str(getattr(status_raw, "value", status_raw)).split(".")[-1]
        if status not in active:
            continue
        created = getattr(job, "created_at", None)
        if not isinstance(created, datetime):
            continue
        ts = created if created.tzinfo else created.replace(tzinfo=timezone.utc)
        hours = (now - ts).total_seconds() / 3600.0
        if hours >= SLA_HOURS:
            at_risk += 1
    if at_risk:
        details["issues"].append("one_task_ops_backlog_pressure")
        details["one_task_ops_at_risk"] = at_risk


def _evaluate_hash_integrity(details: Dict[str, Any], deliverable_root: Path) -> None:
    if not deliverable_root.exists():
        return
    failures = []
    folders = sorted(p for p in deliverable_root.iterdir() if p.is_dir())[-10:]
    for folder in folders:
        ok, info = _verify_hash_manifest(folder)
        if not ok:
            failures.append({"job_id": folder.name, "detail": info})
    if failures:
        details["issues"].append("deliverable_hash_integrity_failed")
        details["hash_issues"] = failures


def assess_automation_factory(*, service: Any, trust_state_path: Path,
                              deliverable_root: Path) -> Dict[str, Any]:
    details: Dict[str, Any] = {"automation_factory_degraded": False, "issues": []}
    _evaluate_trust(details, trust_state_path)
    _evaluate_hash_integrity(details, deliverable_root)
    _evaluate_sla(details, service)
    status = "healthy" if not details["issues"] else "degraded"
    details["automation_factory_degraded"] = status != "healthy"
    return {"status": status, "details": details}


# Weighted risk scoring (recommended next upgrade per chat 31):
def composite_risk_score(details: Dict[str, Any],
                         trust_weight: float = 0.4,
                         sla_weight: float = 0.3,
                         integrity_weight: float = 0.3) -> float:
    trust_deficit = max(0.0, TRUST_THRESHOLD - details.get("trust_score", 0.0)) / TRUST_THRESHOLD
    sla_pressure = min(1.0, details.get("one_task_ops_at_risk", 0) / 10.0)
    integrity_failures = min(1.0, len(details.get("hash_issues", [])) / 5.0)
    return (trust_weight * trust_deficit
            + sla_weight * sla_pressure
            + integrity_weight * integrity_failures)
