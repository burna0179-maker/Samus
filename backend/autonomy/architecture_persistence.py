"""``ArchitecturePersistence`` — META-REFLECTION durable cycle snapshot (Contract 2).

Appends a compact per-cycle record (plan token, compliance outcome, error
count) to a JSONL ledger so the autonomy layer's behaviour over time is
auditable. Pure persistence, no decision-making, fail-OPEN.

Contract 2 method:
    ``snapshot(plan_token=, compliance_blocked=, error_count=) -> None``

This subsystem carries NO feature flag of its own (the build-plan's new
flags gate reinforcement / autotuner / upgrade only). Like ``SituationIndex``
and ``HeuristicRegistry`` it is always-safe pure-compute/IO whose execution is
governed entirely by the (dormant) wrapper that constructs it — so it stays
inert until the wrapper is armed. It is additionally fail-open, so even when
the wrapper runs a write fault never breaks a cycle.

DORMANCY: constructed no-arg; no live importer; inert unless the (dormant)
``MetaCognitionEngine`` constructs and calls it.
"""

from __future__ import annotations

import json
import logging
import time as _time
from typing import Any, Dict

log = logging.getLogger(__name__)


def _snapshot_path():
    from backend.common.state_paths import state_path

    return state_path("autonomy", "architecture_snapshots.jsonl")


class ArchitecturePersistence:
    """Append-only per-cycle architecture snapshot. Fail-open, no-arg construct."""

    def __init__(self) -> None:  # no-arg construct (Contract 2)
        pass

    def snapshot(
        self,
        *,
        plan_token: str = "",
        compliance_blocked: bool = False,
        error_count: int = 0,
    ) -> None:
        """Append one cycle-outcome record to the JSONL ledger.

        Never raises — a write/serialisation error is logged + swallowed
        (fail-open) so persistence can never break a cycle.
        """
        try:
            record: Dict[str, Any] = {
                "schema": "samus.autonomy.architecture_snapshot/1",
                "ts": _time.time(),
                "plan_token": str(plan_token or ""),
                "compliance_blocked": bool(compliance_blocked),
                "error_count": int(error_count or 0),
            }
            path = _snapshot_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, sort_keys=True) + "\n")
        except Exception:  # pragma: no cover — persistence must never break a cycle
            log.exception("ArchitecturePersistence.snapshot failed")


__all__ = ["ArchitecturePersistence"]
