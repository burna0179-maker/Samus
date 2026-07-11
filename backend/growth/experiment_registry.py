"""Outcome-Linked Experiment Registry (OLER).

Shared append-only JSONL ledger for content + sales action outcomes.
Records experiment inputs, outputs, and a monotonic reward signal per pass.

Path: $SAMUS_DATA_ROOT/growth/experiments.jsonl

Design rules (mirror control_tick_ledger.py):
  * Append-only JSONL — no update, no delete.
  * Never raises into callers — errors degrade to logged warnings.
  * Uses JsonlLedger from backend.common.persistence (same as audit trails,
    DLQ, conversion funnel, and control-tick telemetry).
  * Path resolved lazily from SAMUS_DATA_ROOT env var so container and
    host paths work without code changes.
"""
from __future__ import annotations

import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any

_LOG = logging.getLogger("samus.growth.experiment_registry")


def _growth_root() -> Path:
    """$SAMUS_DATA_ROOT/growth — created on first access."""
    base = Path(os.getenv("SAMUS_DATA_ROOT", "data"))
    p = base / "growth"
    p.mkdir(parents=True, exist_ok=True)
    return p


class ExperimentRegistry:
    """Append-only JSONL ledger for outcome-linked experiments.

    Each record:
      experiment_id  -- uuid hex
      kind           -- "content" | "sales" | "offer" | caller-defined
      ts             -- unix epoch float
      inputs         -- task description / parameters (what ran)
      outputs        -- observed metrics (what happened)
    """

    def __init__(self, root: Path | None = None) -> None:
        self._root = root if root is not None else _growth_root()
        self._root.mkdir(parents=True, exist_ok=True)
        self._ledger_path = self._root / "experiments.jsonl"

    def _ledger(self):
        from backend.common.persistence import JsonlLedger  # deferred: no gateway coupling
        return JsonlLedger(self._ledger_path)

    def log(
        self,
        kind: str,
        inputs: dict[str, Any],
        outputs: dict[str, Any],
    ) -> str:
        """Append one experiment record. Returns the new experiment_id."""
        exp_id = uuid.uuid4().hex
        record: dict[str, Any] = {
            "experiment_id": exp_id,
            "kind": kind,
            "ts": time.time(),
            "inputs": inputs,
            "outputs": outputs,
        }
        try:
            self._ledger().append(record)
        except Exception as exc:  # noqa: BLE001 — best-effort telemetry
            _LOG.warning("experiment_registry append failed kind=%s: %s", kind, exc)
        return exp_id

    def tail(self, limit: int = 200) -> list[dict[str, Any]]:
        """Return the most recent experiment records (oldest first)."""
        try:
            return self._ledger().tail(limit=max(1, min(int(limit), 5000)))
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("experiment_registry tail failed: %s", exc)
            return []

    def tail_by_kind(self, kind: str, limit: int = 50) -> list[dict[str, Any]]:
        """Tail filtered to one kind. Reads limit*5 rows then filters."""
        rows = self.tail(limit=limit * 5)
        return [r for r in rows if r.get("kind") == kind][-limit:]


__all__ = ["ExperimentRegistry"]
