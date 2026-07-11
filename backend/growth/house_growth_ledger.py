"""House-account growth scorecard ledger — measures Client Zero's trajectory.

Records one scorecard row per ``house_growth_tick`` pass (see
``backend.growth.house_growth_tick``) so an operator can SEE whether running
Hustleforge as Samus's own Client Zero is actually moving the numbers — SEO
score, content/social freshness, queued improvements — over time, not merely
that the loop ran.

Same substrate + graceful-degradation contract as
``backend.common.control_tick_ledger``: the append-only
``backend.common.persistence.JsonlLedger`` (already used by the CRM audit trail,
DLQ, and conversion funnel), ``record_scorecard`` returns a bool and never
raises, ``recent_scorecards`` degrades to an empty view with an error string. A
telemetry hiccup must never break the growth loop it instruments.

``scorecard_trend`` is the effect-measurement read: it diffs the oldest vs
newest value of a metric across the retained window so a real improvement (or
regression) shows up as a signed delta, not a single point.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Final, Optional

from backend.common import storage
from backend.common.dates import iso_now
from backend.common.persistence import JsonlLedger

_LOG = logging.getLogger("samus.growth.house_growth_ledger")

# Tail window for the recent read. The growth loop is low-frequency (one row
# per weekly tick / operator trigger), so this covers a long history without an
# unbounded read.
_RECENT_TAIL_MAX: Final[int] = 10_000


def _artifact_root() -> Path:
    """Artifact root resolved WITHOUT ``storage.root()``'s eager ``mkdir``.

    ``storage.root()`` mkdirs on every call, which is ACL-denied when a
    non-owner account (e.g. the operator) imports this module — the per-agent
    data isolation working as designed. The ledger must resolve its path
    *inertly* and let ``JsonlLedger`` create the dir lazily on first write
    (under the owning svc account; a denied write degrades to ``False``).
    Mirrors storage's precedence (monkeypatched ``_ROOT`` > env > default) so
    test isolation via ``monkeypatch.setattr(storage, "_ROOT", tmp)`` still
    routes these rows to the patched root.
    """
    if storage._ROOT is not None:
        return Path(storage._ROOT)
    return Path(os.getenv("SAMUS_ARTIFACT_ROOT", storage._DEFAULT_ROOT))


def _ledger_path() -> str:
    """Resolve the scorecard ledger path (env override -> artifact-root default).

    Pure path computation (no side effects): correct on the Windows host AND
    the docker/compose path, with no eager mkdir.
    """
    override = os.getenv("SAMUS_HOUSE_GROWTH_PATH", "").strip()
    if override:
        return override
    return str(_artifact_root() / "telemetry" / "house_growth.jsonl")


def _ledger() -> JsonlLedger:
    return JsonlLedger(_ledger_path())


def record_scorecard(record: dict[str, Any]) -> bool:
    """Append one house-growth scorecard row.

    A ``ts`` ISO8601 field is injected when the caller did not supply one so
    every row is timestamped consistently. Returns ``True`` when written,
    ``False`` on a filesystem error. Never raises — the tick fires this
    best-effort and surfaces the result as a ``recorded`` flag.
    """
    row = dict(record)
    row.setdefault("ts", iso_now())
    try:
        _ledger().append(row)
        return True
    except OSError as exc:
        _LOG.warning("house_growth_ledger append failed: %s", exc)
        return False


def recent_scorecards(limit: int = 50) -> dict[str, Any]:
    """Tail the scorecard ledger into a recent view.

    Returns a serialisable dict ``{"scorecards": [...newest last...], "count":
    int, "error": str | None}``. ``limit`` is clamped to ``[1, _RECENT_TAIL_MAX]``.
    Never raises: any read failure yields an empty list with the error string
    populated so a dashboard need not special-case a telemetry outage.
    """
    safe_limit = max(1, min(int(limit), _RECENT_TAIL_MAX))
    rows: list[dict[str, Any]] = []
    error: Optional[str] = None
    try:
        rows = _ledger().tail(limit=safe_limit)
    except Exception as exc:  # noqa: BLE001 — telemetry read, never blocks
        _LOG.warning("house_growth_ledger recent read failed: %s", exc)
        error = f"house_growth_read_failed: {exc}"
    return {"scorecards": rows, "count": len(rows), "error": error}


def scorecard_trend(metric: str, window: int = 12) -> dict[str, Any]:
    """Signed delta of ``metric`` across the retained window (effect measurement).

    Reads the last ``window`` scorecards, pulls the numeric ``metric`` from each
    (skipping rows that lack it or carry a non-numeric / boolean value), and
    reports the oldest->newest movement so a real improvement/regression is a
    visible delta, not a single point. ``direction`` is ``up``/``down``/``flat``
    (or ``insufficient_data`` with < 2 samples). Never raises.
    """
    view = recent_scorecards(limit=max(2, int(window)))
    series: list[float] = []
    for row in view["scorecards"]:
        val = row.get(metric)
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            series.append(float(val))
    if len(series) < 2:
        return {
            "metric": metric,
            "samples": len(series),
            "first": series[0] if series else None,
            "last": series[-1] if series else None,
            "delta": None,
            "direction": "insufficient_data",
            "error": view["error"],
        }
    first, last = series[0], series[-1]
    delta = last - first
    direction = "up" if delta > 0 else ("down" if delta < 0 else "flat")
    return {
        "metric": metric,
        "samples": len(series),
        "first": first,
        "last": last,
        "delta": delta,
        "direction": direction,
        "error": view["error"],
    }


__all__ = ["record_scorecard", "recent_scorecards", "scorecard_trend"]
