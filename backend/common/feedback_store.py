"""Shared, PERSISTENT call-outcome feedback store.

Single source of truth for the outcome counters — objection frequencies,
close/failure counts per product, and win/loss per pitch angle — that feed
``prospecting.intelligence.determine_pitch_angle`` angle selection and the
operator feedback snapshot.

Replaces the two former in-memory copies (``crm.feedback_engine`` and
``outreach.metrics``), which were byte-identical module-level dicts written
by *different* workcells (CRM vs outreach). That split meant call outcomes
accumulated into two divergent process-local dicts and were lost on every
restart. Both modules now delegate here, so:

  * CRM- and outreach-logged outcomes accumulate into ONE set of counters
    (no parallel systems), and
  * the counters PERSIST across workcell restarts.

Persistence: a JSON document under the writable, persistent ``samus-data``
volume via :func:`backend.common.state_paths.state_path` (so every workcell
container, which all mount that volume, reads/writes the same file). Writes
reload-then-merge under a process lock and replace atomically, so a crash
mid-write never corrupts the file (reader sees the old or new doc, never a
partial one).

Concurrency: the process lock serialises writers *within* a container.
Across containers (CRM + outreach writing at the same instant) a lost
increment is possible in the small reload→write window. That is acceptable
for this advisory, self-correcting data tier (call outcomes are human-paced
and rare relative to the window), and matches the JSON-store posture of
``backend.crm.needs_warm_path``. A DDB atomic-counter (``ADD``) upgrade can
later sit behind this same API without touching callers — see
``crm/__init__.py`` "feedback engine v2".

``reset_metrics()`` wipes the store and is a TEST-ISOLATION helper only.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from typing import Any

from backend.common.state_paths import state_path

_LOG = logging.getLogger("samus.feedback_store")
_LOCK = threading.Lock()

# Plain-int counter sections (vs the nested wins/losses "angles" section).
_COUNTER_SECTIONS = ("objections", "closes", "failures")


def _is_int(v: Any) -> bool:
    # bool is an int subclass — exclude it so a stray True/False can't poison a count.
    return isinstance(v, int) and not isinstance(v, bool)


def _empty() -> dict[str, Any]:
    return {"objections": {}, "closes": {}, "failures": {}, "angles": {}}


def _path() -> str:
    # SAMUS_FEEDBACK_STORE_PATH override mirrors the SAMUS_LLM_BUDGET_PATH /
    # SAMUS_STRATEGY_BANDIT_PATH convention so tests (and any operator who wants
    # a bespoke location) can redirect the store off the default volume path.
    override = os.getenv("SAMUS_FEEDBACK_STORE_PATH", "").strip()
    if override:
        return override
    return str(state_path("feedback", "metrics.json"))


def _load() -> dict[str, Any]:
    """Read + sanitise the persisted doc. Caller holds ``_LOCK``.

    A missing or corrupt file yields an empty doc rather than raising — the
    store must never break the call-logging flow.
    """
    path = _path()
    if not os.path.isfile(path):
        return _empty()
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:  # noqa: BLE001 — defensive
        _LOG.warning("feedback_store read failed (%s); starting empty", exc)
        return _empty()
    if not isinstance(raw, dict):
        return _empty()

    out = _empty()
    for section in _COUNTER_SECTIONS:
        block = raw.get(section)
        if isinstance(block, dict):
            out[section] = {
                str(k): int(v) for k, v in block.items() if _is_int(v)
            }
    angles = raw.get("angles")
    if isinstance(angles, dict):
        for name, rec in angles.items():
            if not isinstance(rec, dict):
                continue
            wins = rec.get("wins", 0)
            losses = rec.get("losses", 0)
            out["angles"][str(name)] = {
                "wins": int(wins) if _is_int(wins) else 0,
                "losses": int(losses) if _is_int(losses) else 0,
            }
    return out


def _write(data: dict[str, Any]) -> None:
    """Atomic-replace write. Caller holds ``_LOCK``.

    Fail-OPEN: a write failure is logged and swallowed so a read-only-fs or
    transient disk error never breaks the outcome-logging path. (Losing one
    increment is strictly better than failing the call flow.)
    """
    path = _path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            prefix="feedback-", suffix=".json.tmp", dir=os.path.dirname(path),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except OSError as exc:  # noqa: BLE001 — defensive boundary
        _LOG.warning("feedback_store write failed: %s", exc)


def log_interaction(
    prospect_id: str,
    outcome: str,
    objection: str | None,
    product: str,
    angle: str,
) -> None:
    """Record one call outcome. Thread-safe + persistent (reload→merge→write).

    ``prospect_id`` is accepted for call-site parity / future per-prospect
    event sourcing; the aggregate counters do not key on it.
    """
    with _LOCK:
        data = _load()
        if objection:
            data["objections"][objection] = data["objections"].get(objection, 0) + 1
        rec = data["angles"].setdefault(angle, {"wins": 0, "losses": 0})
        if outcome == "closed":
            data["closes"][product] = data["closes"].get(product, 0) + 1
            rec["wins"] = int(rec.get("wins", 0)) + 1
        else:
            data["failures"][product] = data["failures"].get(product, 0) + 1
            rec["losses"] = int(rec.get("losses", 0)) + 1
        _write(data)


def get_top_objections() -> list[tuple[str, int]]:
    """Objection counts, descending by frequency."""
    with _LOCK:
        objections = _load()["objections"]
    return sorted(objections.items(), key=lambda x: x[1], reverse=True)


def get_best_products() -> list[tuple[str, int]]:
    """Close counts per product, descending."""
    with _LOCK:
        closes = _load()["closes"]
    return sorted(closes.items(), key=lambda x: x[1], reverse=True)


def get_angle_performance() -> dict[str, float]:
    """Win-rate (wins / total) per angle; skips angles with zero interactions."""
    with _LOCK:
        angles = _load()["angles"]
    out: dict[str, float] = {}
    for angle, rec in angles.items():
        total = rec.get("wins", 0) + rec.get("losses", 0)
        if total == 0:
            continue
        out[angle] = rec["wins"] / total
    return out


def optimize_weights(intel: dict[str, Any]) -> dict[str, Any]:
    """Surface the best-performing angle into ``intel["strategy"]``.

    Sets ``intel["strategy"]["angle_bias"]`` to the highest-win-rate angle and
    ``intel["strategy"]["angle_performance"]`` to the full win-rate map, so the
    call sheet / downstream can see the learned signal. Returns ``intel``
    mutated in place. Advisory only — it never overrides the deterministic
    angle choice; see ``determine_pitch_angle`` for the applicability-bounded
    re-ranking.
    """
    angle_perf = get_angle_performance()
    if angle_perf:
        best_angle = max(angle_perf, key=lambda k: angle_perf[k])
        strategy = intel.setdefault("strategy", {})
        strategy["angle_bias"] = best_angle
        strategy["angle_performance"] = angle_perf
    return intel


def snapshot() -> dict[str, Any]:
    """JSON-serialisable plain-dict snapshot of all counters."""
    with _LOCK:
        data = _load()
    return {
        "objections": dict(data["objections"]),
        "closes": dict(data["closes"]),
        "failures": dict(data["failures"]),
        "angles": {k: dict(v) for k, v in data["angles"].items()},
    }


def reset_metrics() -> None:
    """TEST-ISOLATION ONLY — wipe the persisted store to empty counters."""
    with _LOCK:
        _write(_empty())


__all__ = [
    "log_interaction",
    "get_top_objections",
    "get_best_products",
    "get_angle_performance",
    "optimize_weights",
    "snapshot",
    "reset_metrics",
]
