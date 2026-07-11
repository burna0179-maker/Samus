"""Geographic-ring catalog + advancement + state persistence (ADR-014).

ADR-014 (ACCEPTED 2026-06-18) — supports the dormant-by-default in-stack prospecting
cadence (``backend/prospecting/cadence_task.py``). This is the faithful Python
port of the geo-ring logic that has lived in the host PowerShell producer
``scripts/Run-ProspectingDaily.ps1`` (lines 115-173 + the state-update /
operator-nudge tail), moved into the container so the daily fire no longer
depends on a host process. Behaviour is intended to match the PowerShell
script exactly:

* **Ring catalog** — an operator-editable, ordered list of named rings, each a
  set of zipcodes. Yuba City anchor expanding southward into the Sacramento
  metro. Rings are CUMULATIVE: ring index N searches the zipcodes of rings
  0..N. The catalog here is byte-for-byte the same names + zipcodes as the
  ``$ringCatalog`` in the PS1.
* **Cumulative composition** — ``cumulative_zipcodes(ring_index)`` returns the
  union of rings 0..N preserving first-seen order and de-duplicating, matching
  the PS1's ``for ($i=0; $i -le current_ring) { ... } | Select-Object -Unique``.
* **Advancement** — ``advance_ring`` bumps ``current_ring`` by one (resetting
  ``days_at_ring`` to 0) UNLESS already at the top ring, in which case it is a
  no-op (the PS1 prints a warning and leaves state unchanged).
* **State persistence** — a JSON file (default ``prospecting_geo_state.json``
  under ``backend.common.storage.root()`` — i.e. the shared ``samus-data``
  volume in the container, the ``SAMUS_ARTIFACT_ROOT`` override on the host).
  Same shape the PS1 writes: ``current_ring`` / ``days_at_ring`` /
  ``last_run_date`` / ``history`` (bounded to the most recent 90 entries). A
  corrupt/unreadable state file is quarantined to a timestamped ``.corrupt-*``
  sibling and state re-initialises at ring 0, mirroring the PS1.

Pure + unit-testable: the catalog and the composition/advancement helpers have
no I/O; the loader/saver take an explicit path so tests point them at a temp
dir. Nothing here makes any network call or runs any worker.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from backend.common import storage

_LOG = logging.getLogger("samus.prospecting.geo_ring")

# Default state filename under the artifact root. The PS1 wrote
# ``prospecting_geo_state.json`` under its host artifact root; we keep the same
# basename so the artifact tree is recognisable across the host/container
# split. ``storage.root()`` resolves the writable root (the samus-data volume
# in the container via SAMUS_ARTIFACT_ROOT).
STATE_FILENAME = "prospecting_geo_state.json"

# Cap on retained history entries — matches the PS1's
# ``if ($state.history.Count -gt 90) { Select-Object -Last 90 }``.
_HISTORY_LIMIT = 90


@dataclass(frozen=True)
class Ring:
    """One named geographic ring: a set of zipcodes searched at this index."""

    name: str
    zipcodes: tuple[str, ...]


# Operator-editable ring catalog. Yuba City anchor expanding southward into the
# Sacramento metro. Rings are cumulative once added: ring index N includes
# rings 0..N's zipcodes in the active search. This is the faithful port of
# ``$ringCatalog`` in scripts/Run-ProspectingDaily.ps1:115-122 — same order,
# names, and zipcodes. Edit here (and the PS1, until the host path retires)
# when the field changes.
RING_CATALOG: tuple[Ring, ...] = (
    Ring("yuba_city_core", ("95991", "95993", "95901", "95961", "95692")),
    Ring("yuba_sutter_outer", ("95953", "95919", "95941", "95918", "95659", "95957", "95668")),
    Ring("placer_lincoln_auburn", ("95648", "95603", "95746", "95765", "95650")),
    Ring("roseville_corridor", ("95661", "95678", "95747")),
    Ring("folsom_elk_grove", ("95630", "95758", "95624", "95757")),
    Ring("sacramento_metro", ("95814", "95816", "95818", "95820", "95822", "95823", "95825")),
)


def top_ring_index() -> int:
    """Index of the last configured ring (``ringCatalog.Count - 1`` in the PS1)."""
    return len(RING_CATALOG) - 1


def clamp_ring_index(ring_index: int) -> int:
    """Clamp an arbitrary ring index into ``[0, top_ring_index()]``.

    A hand-edited state file or a shrunk catalog could leave ``current_ring``
    out of range; the PS1 indexes ``$ringCatalog[$state.current_ring]``
    unconditionally, so we defend the equivalent Python lookups by clamping.
    """
    if ring_index < 0:
        return 0
    top = top_ring_index()
    return top if ring_index > top else ring_index


def ring_name(ring_index: int) -> str:
    """Name of the ring at ``ring_index`` (clamped)."""
    return RING_CATALOG[clamp_ring_index(ring_index)].name


def cumulative_zipcodes(ring_index: int) -> list[str]:
    """Compose the active zipcode set for ``ring_index`` (rings 0..N, deduped).

    Faithful to the PS1's cumulative composition::

        for ($i = 0; $i -le current_ring; $i++) { add ring[i].zipcodes }
        $activeZipcodes | Select-Object -Unique

    Order is first-seen (the order zipcodes appear walking rings 0..N), and
    duplicates across overlapping rings are dropped — matching
    ``Select-Object -Unique`` which preserves first occurrence.
    """
    target = clamp_ring_index(ring_index)
    seen: set[str] = set()
    out: list[str] = []
    for i in range(target + 1):
        for zipcode in RING_CATALOG[i].zipcodes:
            if zipcode in seen:
                continue
            seen.add(zipcode)
            out.append(zipcode)
    return out


@dataclass
class GeoRingState:
    """Mutable prospecting geo-ring state. Mirrors the PS1 state object.

    Fields match the JSON the PowerShell producer wrote/read:
    ``current_ring`` (int), ``days_at_ring`` (int), ``last_run_date``
    (``YYYY-MM-DD`` string or None), ``history`` (list of run records).
    """

    current_ring: int = 0
    days_at_ring: int = 0
    last_run_date: str | None = None
    history: list[dict[str, Any]] = field(default_factory=list)

    # ---- composition convenience (pure) ------------------------------------

    def active_zipcodes(self) -> list[str]:
        """Cumulative zipcode set for the current ring (rings 0..current)."""
        return cumulative_zipcodes(self.current_ring)

    def current_ring_name(self) -> str:
        return ring_name(self.current_ring)

    def at_top_ring(self) -> bool:
        return clamp_ring_index(self.current_ring) >= top_ring_index()

    # ---- serialisation -----------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "current_ring": int(self.current_ring),
            "days_at_ring": int(self.days_at_ring),
            "last_run_date": self.last_run_date,
            "history": list(self.history),
        }

    @classmethod
    def from_dict(cls, raw: Any) -> "GeoRingState":
        """Build state from a parsed JSON object, defending malformed fields.

        A non-dict or a dict with missing/garbage fields degrades to the
        ring-0 defaults rather than raising — the PS1 likewise reinitialises
        on an unusable state object.
        """
        if not isinstance(raw, dict):
            return cls()

        def _as_int(value: Any, default: int) -> int:
            try:
                return int(value)
            except (TypeError, ValueError):
                return default

        current_ring = clamp_ring_index(_as_int(raw.get("current_ring"), 0))
        days_at_ring = _as_int(raw.get("days_at_ring"), 0)
        if days_at_ring < 0:
            days_at_ring = 0
        last_run_date = raw.get("last_run_date")
        if last_run_date is not None and not isinstance(last_run_date, str):
            last_run_date = None
        history_raw = raw.get("history")
        history: list[dict[str, Any]] = (
            [h for h in history_raw if isinstance(h, dict)] if isinstance(history_raw, list) else []
        )
        return cls(
            current_ring=current_ring,
            days_at_ring=days_at_ring,
            last_run_date=last_run_date,
            history=history,
        )


def default_state_path() -> Path:
    """Resolve the on-disk geo-state path under the artifact root.

    Uses ``backend.common.storage.root()`` so the file lands on the writable
    ``samus-data`` volume in the container (``SAMUS_ARTIFACT_ROOT``) and under
    the host artifact root on the host — the same resolver the call-list
    exporters use, so the state file sits alongside ``daily_calls/``.
    """
    return storage.root() / STATE_FILENAME


def load_state(path: Path | None = None) -> GeoRingState:
    """Load geo-ring state from ``path`` (default: under the artifact root).

    A missing file yields fresh ring-0 defaults. An unreadable / non-JSON file
    is quarantined to a timestamped ``.corrupt-*`` sibling and state
    re-initialises — mirroring the PS1's corrupt-state handling.
    """
    target = path or default_state_path()
    if not target.exists():
        _LOG.info(
            "geo-ring state absent at %s; initialising at ring 0 (%s)",
            target,
            ring_name(0),
        )
        return GeoRingState()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = target.with_name(f"{target.name}.corrupt-{stamp}")
        try:
            target.replace(backup)
            _LOG.warning(
                "geo-ring state unreadable (%s) — quarantined to %s, reinitialising at ring 0",
                exc,
                backup,
            )
        except OSError as move_exc:  # noqa: PERF203 — quarantine is best-effort
            _LOG.warning(
                "geo-ring state unreadable (%s) and quarantine failed (%s); "
                "reinitialising at ring 0 without moving the bad file",
                exc,
                move_exc,
            )
        return GeoRingState()
    return GeoRingState.from_dict(raw)


def save_state(state: GeoRingState, path: Path | None = None) -> Path:
    """Persist ``state`` as JSON. Returns the path written.

    The parent directory is created if needed (``storage.root()`` already
    creates the artifact root, but an explicit path in a test may not exist).
    """
    target = path or default_state_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(state.to_dict(), indent=2),
        encoding="utf-8",
    )
    return target


def advance_ring(state: GeoRingState) -> tuple[GeoRingState, bool]:
    """Advance to the next ring in place; return ``(state, advanced)``.

    Faithful to the PS1 ``-AdvanceRing`` branch: when below the top ring,
    increment ``current_ring`` and reset ``days_at_ring`` to 0. At the top ring
    it is a NO-OP (``advanced=False``) — the PS1 prints
    "Already at top ring ... no further rings configured" and leaves state
    untouched. Mutates and also returns ``state`` for ergonomic chaining.
    """
    if state.at_top_ring():
        _LOG.warning(
            "already at top ring '%s' (index %d) — no further rings "
            "configured; advance is a no-op. Extend RING_CATALOG to expand.",
            state.current_ring_name(),
            clamp_ring_index(state.current_ring),
        )
        return state, False
    old_name = state.current_ring_name()
    state.current_ring = clamp_ring_index(state.current_ring) + 1
    state.days_at_ring = 0
    _LOG.info(
        "advanced from ring '%s' to ring '%s' (ring index now %d)",
        old_name,
        state.current_ring_name(),
        state.current_ring,
    )
    return state, True


def record_run(
    state: GeoRingState,
    *,
    run_date: date | None = None,
    exit_code: int = 0,
    zipcodes_count: int | None = None,
) -> GeoRingState:
    """Stamp a completed daily run onto ``state`` in place.

    Mirrors the PS1 state-update tail (lines 277-292): increment
    ``days_at_ring`` by one, set ``last_run_date`` to today's ``YYYY-MM-DD``,
    and append a history record bounded to the most recent 90 entries. The
    ``day_at_ring`` written into the history record is the incremented value
    (the PS1 computes ``$dayAtRing = days_at_ring + 1`` before the run and then
    persists it). Mutates and returns ``state``.
    """
    run_date = run_date or date.today()
    state.days_at_ring = int(state.days_at_ring) + 1
    state.last_run_date = run_date.isoformat()
    zip_count = zipcodes_count if zipcodes_count is not None else len(state.active_zipcodes())
    state.history.append(
        {
            "date": state.last_run_date,
            "ring_index": clamp_ring_index(state.current_ring),
            "ring_name": state.current_ring_name(),
            "zipcodes_count": int(zip_count),
            "day_at_ring": int(state.days_at_ring),
            "exit_code": int(exit_code),
        }
    )
    if len(state.history) > _HISTORY_LIMIT:
        state.history = state.history[-_HISTORY_LIMIT:]
    return state


__all__ = [
    "Ring",
    "RING_CATALOG",
    "STATE_FILENAME",
    "GeoRingState",
    "top_ring_index",
    "clamp_ring_index",
    "ring_name",
    "cumulative_zipcodes",
    "default_state_path",
    "load_state",
    "save_state",
    "advance_ring",
    "record_run",
]
