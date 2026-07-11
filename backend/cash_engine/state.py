"""CashEngineState — the durable per-deal state machine for the sequence.

Once the front door enqueues a job, the worker walks it through an ordered
sequence of stages. That walk is durable: each stage's outcome is persisted
under the state root keyed by ``opportunity_id``, so re-processing the same
job (the mock JSONL queue is append-only; SQS can redeliver) is idempotent —
already-completed stages are skipped. The same record is what the
re-engagement loop reads back to compile a deal's history.

This module owns only the state shape + its load/save; the stage logic lives
in :mod:`backend.cash_engine.stages` and the orchestration in
:mod:`backend.cash_engine.worker`.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from backend.common.dates import iso_now
from backend.common.state_paths import state_path

_LOG = logging.getLogger("samus.cash_engine.state")

# The ordered sequence the worker walks. After the initial outreach the walk
# reaches "deliver": the bridge to the website builder, which only fires once
# the Opportunity is actually won (closed_won) — until then it parks honestly
# and the deal rests dormant, awaiting a re-engagement signal. "dormant" is the
# resting state after the walk parks/completes; re-engagement is a separate
# trigger, not a stage in this forward walk.
STAGE_SEQUENCE: tuple[str, ...] = ("audit", "proposal", "contact", "outreach", "deliver")
DORMANT = "dormant"

_STATE_DIR = "cash_engine"
_STATE_SUBDIR = "state"


class CashEngineState(BaseModel):
    """Durable state of one Opportunity moving through the cash-engine sequence."""

    model_config = ConfigDict(extra="ignore")

    opportunity_id: str
    prospect_id: str = ""
    task_id: str = ""
    trigger_source: str = ""

    # The stage the worker will run next. Once every stage in STAGE_SEQUENCE
    # is in ``completed_stages`` the worker flips ``status`` to "dormant".
    stage: str = "audit"
    # running   — mid-walk
    # parked    — a stage needs an external route/input not yet wired; resumable
    # escalated — a Codex gate blocked a handoff; operator must resolve
    # dormant   — initial outreach done; awaiting a re-engagement signal
    status: str = "running"
    completed_stages: list[str] = Field(default_factory=list)

    # Per-stage outputs (artifact ids / dispatch refs).
    gap_report_artifact_id: str = ""
    proposal_ref: str = ""
    callsheet_artifact_id: str = ""
    voicemail_artifact_id: str = ""
    outreach_ref: str = ""
    outreach_scheduled_for: str = ""
    # Set by the deliver stage once a won deal is bridged to the website builder.
    website_order_id: str = ""

    dormant_since: str = ""
    # Set when status == "escalated": {stage, violated_rule_id, reason,
    # drafted_adr_path}.
    escalation: dict[str, Any] | None = None
    # Set when status == "parked": {stage, reason}.
    park: dict[str, Any] | None = None

    history: list[dict[str, Any]] = Field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""

    def log(self, event: str, **detail: Any) -> None:
        """Append a timestamped event to the deal's history."""
        self.history.append({"ts": iso_now(), "event": event, **detail})


def _state_file(opportunity_id: str) -> Path:
    # opportunity_id is a server-minted op_... id (no path separators), but be
    # defensive against traversal in a persisted key all the same.
    safe = opportunity_id.replace("/", "_").replace("\\", "_").replace("..", "_")
    return state_path(_STATE_DIR, _STATE_SUBDIR, f"{safe}.json")


def load_state(opportunity_id: str) -> CashEngineState | None:
    """Load the persisted state for an Opportunity, or None if absent."""
    path = _state_file(opportunity_id)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return CashEngineState.model_validate(data)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        _LOG.error("cash_engine state load failed (%s): %s", path, exc)
        return None


def save_state(state: CashEngineState) -> bool:
    """Persist state atomically-ish (write temp then replace). Best-effort."""
    if not state.created_at:
        state.created_at = iso_now()
    state.updated_at = iso_now()
    path = _state_file(state.opportunity_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(state.model_dump_json(indent=2), encoding="utf-8")
        tmp.replace(path)
        return True
    except OSError as exc:
        _LOG.error("cash_engine state save failed (%s): %s", path, exc)
        return False


__all__ = [
    "STAGE_SEQUENCE",
    "DORMANT",
    "CashEngineState",
    "load_state",
    "save_state",
]
