"""Minimal ConfusionEvent JSONL writer for Samus.

Cross-agent depth-layer participant; PDC stressors consume these events.
Writer only — no integrations, no enforcement, no tests yet.
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.common.state_paths import state_path

_VALID_KINDS = {"kr_gap", "evidence_conflict", "axiom_violation", "goal_incoherence"}

# Default events.jsonl under the writable state root (the data volume
# in-container, <code root>/state on the host). SAMUS_CONFUSION_EVENTS_PATH
# overrides at call time. See backend/common/state_paths.py.
_EVENTS_PATH = state_path("confusion", "events.jsonl")


def emit_confusion(
    *,
    kind: str,
    delta: float,
    source_refs: list[str],
    agent: str = "samus",
    threshold_breach: bool = False,
) -> dict[str, Any]:
    """Append one ConfusionEvent JSONL record and return it.

    Raises ValueError if `kind` is not one of the four accepted kinds.
    """
    if kind not in _VALID_KINDS:
        raise ValueError(f"invalid confusion kind {kind!r}; must be one of {sorted(_VALID_KINDS)}")

    record: dict[str, Any] = {
        "event_id": str(uuid.uuid4()),
        "agent": agent,
        "ts": datetime.now(timezone.utc).isoformat(),
        "kind": kind,
        "delta": float(delta),
        "source_refs": list(source_refs),
        "threshold_breach": bool(threshold_breach),
    }

    path = Path(os.environ.get("SAMUS_CONFUSION_EVENTS_PATH", str(_EVENTS_PATH)))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, separators=(",", ":")) + "\n")
    return record
