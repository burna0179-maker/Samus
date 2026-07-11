"""Learning telemetry -- make bandit / attribution updates reconstructable.

Samus's learning loop (the strategy policy/channel bandits and the attribution
email-variant bandit) updated its arms with only a debug-level log line. So an
observer could not reconstruct, from durable telemetry, WHAT Samus learned:

    * which arm was updated, from what outcome, to what wins/trials?
    * did the durable write actually SUCCEED, or did it silently degrade to
      cache-only? (the store's success/failure return was dropped on the floor
      -- a swallowed miss the next process restart loses entirely.)

This closes that blind spot additively + fail-soft: one reconstructable record
per learning update to a dedicated ``telemetry/bandit_learning.jsonl`` ledger,
plus a Prometheus counter keyed on whether the write persisted -- so a rising
``persisted="false"`` series is a "learning is silently degrading" alarm.

COMPOSES WITH THE CASH-ENGINE DECISION RECORDS (PR #37): a terminal escalate /
park decision recorded there -- an outcome that SHOULD teach the bandit -- with
NO matching learning record here reveals the learning-loop gap: intermediate
failures (codex block, park, produce-fail) that never feed the learner. Making
that gap observable is this module's job; WIRING those failures into the
learner is a behavioural change reserved for governance, not done here.

GRACEFUL DEGRADATION (non-negotiable): ``record_learning_update`` NEVER raises
and NEVER changes the learning outcome it instruments -- it mirrors
``emit_business_event`` / ``record_decision``.
"""
from __future__ import annotations

import logging
import os
import uuid
from typing import Any, Final

from .correlation import ensure_trace_id
from .dates import iso_now
from .persistence import open_ledger

_LOG = logging.getLogger("samus.common.learning_telemetry")

# --- Kind taxonomy ------------------------------------------------------
KIND_BANDIT: Final[str] = "bandit"    # strategy policy/channel/flat arm
KIND_VARIANT: Final[str] = "variant"  # attribution email-variant arm

# --- Ledger plumbing ----------------------------------------------------
_LEDGER_PATH_DEFAULT: Final[str] = "/opt/samus/data/telemetry/bandit_learning.jsonl"
_FIRESTORE_COLLECTION: Final[str] = "samus_bandit_learning"
_READ_TAIL_MAX: Final[int] = 100_000


def _ledger_path() -> str:
    """Resolve the learning-update ledger path (env override -> default)."""
    return os.getenv("SAMUS_LEARNING_TELEMETRY_PATH", _LEDGER_PATH_DEFAULT)


def _ledger():
    return open_ledger(
        jsonl_path=_ledger_path(), collection=_FIRESTORE_COLLECTION,
    )


def _inc_counter(*, kind: str, persisted: bool) -> None:
    """Best-effort Prometheus counter increment. Never raises."""
    try:
        from . import metrics as _metrics

        _metrics.SAMUS_LEARNING_UPDATES_TOTAL.labels(
            kind=kind or "", persisted="true" if persisted else "false",
        ).inc()
    except Exception as exc:  # noqa: BLE001 -- telemetry must never break callers
        _LOG.debug("learning_telemetry counter publish skipped: %s", exc)


def record_learning_update(
    *,
    kind: str,
    arm_id: str,
    outcome: float,
    reward: float,
    wins: float = 0.0,
    trials: int = 0,
    persisted: bool = True,
    density_applied: bool = False,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Record one learning update. Fail-soft: NEVER raises.

    ``kind`` is :data:`KIND_BANDIT` or :data:`KIND_VARIANT`. ``outcome`` is the
    raw reward scalar the learner was handed; ``reward`` is what it actually
    credited the arm (post reward-density when ``density_applied``). ``wins`` /
    ``trials`` are the arm's state AFTER the update. ``persisted`` is whether
    the durable store accepted the write (False = degraded to cache-only, lost
    on restart). Returns the record dict even when persistence fails.
    """
    record: dict[str, Any] = {
        "ts": iso_now(),
        "update_id": uuid.uuid4().hex,
        "trace_id": ensure_trace_id(),
        "kind": kind or "",
        "arm_id": arm_id or "",
        "outcome": round(float(outcome or 0.0), 6),
        "reward": round(float(reward or 0.0), 6),
        "wins": round(float(wins or 0.0), 6),
        "trials": int(trials or 0),
        "persisted": bool(persisted),
        "density_applied": bool(density_applied),
    }
    if extra:
        record["extra"] = dict(extra)
    try:
        _ledger().append(record)
    except Exception as exc:  # noqa: BLE001 -- telemetry must never break callers
        _LOG.debug("learning_telemetry append failed arm=%s: %s", arm_id, exc)

    _inc_counter(kind=kind, persisted=bool(persisted))

    try:
        if not persisted:
            # The learner credited an arm the durable store did NOT keep -- the
            # update is cache-only and dies on restart. Make it visible (the
            # ledger + counter carry it; the store logs its own fault).
            _LOG.info(
                "learning update NOT persisted (cache-only) kind=%s arm=%s reward=%.4f",
                kind, arm_id, record["reward"],
            )
        else:
            _LOG.debug(
                "learning update kind=%s arm=%s outcome=%.4f reward=%.4f wins=%.3f trials=%d",
                kind, arm_id, record["outcome"], record["reward"],
                record["wins"], record["trials"],
            )
    except Exception:  # noqa: BLE001 -- a logging fault must not break callers
        pass
    return record


def read_learning(
    *,
    kind: str | None = None,
    arm_id: str | None = None,
    persisted: bool | None = None,
    since: str | None = None,
    limit: int = 1000,
) -> list[dict[str, Any]]:
    """Read + filter the learning-update ledger, oldest-first. Never raises.

    The reconstruction surface: ``read_learning(persisted=False)`` surfaces
    every silently-degraded (cache-only) learning update; ``read_learning(
    arm_id=...)`` reconstructs an arm's full learning history. Returns ``[]`` on
    any failure.
    """
    try:
        safe_limit = max(1, min(int(limit), _READ_TAIL_MAX))
        rows = _ledger().tail(limit=_READ_TAIL_MAX)
        out: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            if kind and row.get("kind") != kind:
                continue
            if arm_id and row.get("arm_id") != arm_id:
                continue
            if persisted is not None and bool(row.get("persisted")) != persisted:
                continue
            if since and str(row.get("ts") or "") < since:
                continue
            out.append(row)
        return out[-safe_limit:]
    except Exception as exc:  # noqa: BLE001 -- telemetry read, never blocks
        _LOG.debug("learning_telemetry read failed: %s", exc)
        return []


__all__ = [
    "KIND_BANDIT",
    "KIND_VARIANT",
    "record_learning_update",
    "read_learning",
]
