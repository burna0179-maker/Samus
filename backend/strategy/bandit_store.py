"""Durable storage for the strategy multi-armed bandit.

``portfolio_manager`` holds the UCB1 bandit state as ``{arm_id: {"wins":
float, "trials": int}}``. The strategy-integration loop runs **decide** and
**learn** in separate processes — the host-side 07:30 prospecting process
selects an arm, the container-side CRM/strategy process records the outcome —
so the state cannot live in one process's memory. It must be durable and
shared.

This module is that durable layer. It mirrors
:mod:`backend.common.llm_budget` exactly:

  - **DynamoDB primary.** Table ``samus_strategy_bandit``, one row per arm
    (PK = arm id string). Counter updates use an atomic DynamoDB
    ``UpdateExpression="ADD wins :w, trials :t"`` so the host-side and
    container-side writers increment the same row without clobbering each
    other — that concurrency-safety is the whole point.
  - **JSON-file fallback.** When DDB is unset / unreachable the store
    degrades to a JSON file at ``SAMUS_STRATEGY_BANDIT_PATH`` (default
    ``/opt/samus/data/strategy/bandit.json``), exactly like
    ``llm_budget``'s ``SAMUS_LLM_BUDGET_PATH``.
  - **Allow-on-persistence-failure.** A catastrophic store failure never
    raises out of this module. ``read_all`` returns ``{}`` and ``add`` is a
    best-effort no-op — the bandit degrades to "no memory" rather than
    blocking the decide/learn loop. Mirrors how ``llm_budget.can_spend``
    returns ``True`` when the store is unavailable.

The flat arms (``"industry"``) and the hierarchical arms
(``"industry::policy_family"``) share one ``_BANDIT_STATS`` dict in
``portfolio_manager``; they therefore share this one table — the arm id is
just an opaque string PK, no separate structure.

``_BANDIT_TOTAL`` (the UCB1 ``ln(total)`` denominator) is **not** persisted
as its own row. It is derived as ``sum(trials for every arm)`` on every read.
This is the simpler correct option: a separate sentinel row would need its
own atomic increment and could drift from the arms if one write succeeded and
the other failed. Deriving the total from the same snapshot that produced the
arms is always internally consistent.

The store is injectable: ``portfolio_manager`` reaches it through
``get_default_store()`` and tests swap it via ``set_default_store()``, so the
pure-logic bandit functions stay unit-testable without a hard DDB import.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable


_LOG = logging.getLogger("samus.strategy.bandit_store")

# --- storage constants -------------------------------------------------------
# DDB attribute names for the per-arm counter row. PK is ``ARM_PK_ATTR``.
ARM_PK_ATTR: str = "arm_id"
WINS_ATTR: str = "wins"
TRIALS_ATTR: str = "trials"

# Default JSON fallback path. Lives under the same /opt/samus/data tree as the
# other Samus on-disk stores (llm_budgets.json, portfolio_snapshots.json).
_DEFAULT_JSON_PATH: str = "/opt/samus/data/strategy/bandit.json"
_DEFAULT_DDB_TABLE: str = "samus_strategy_bandit"
_DEFAULT_AWS_REGION: str = "us-west-1"

# Read-through cache freshness. The bandit tolerates slightly stale reads —
# UCB1 is robust to a few-second lag — and a short TTL keeps a hot decide loop
# from hammering DDB. Mirrors ``llm_budget.LlmBudgetStore._cache_ttl``.
_DEFAULT_CACHE_TTL_SEC: float = 5.0


# ---------------------------------------------------------------------------
# Data structure
# ---------------------------------------------------------------------------


@dataclass
class BanditArm:
    """In-memory shape of one arm's row. Mirrors the DDB schema.

    ``arm_id`` is the partition key — a flat ``"industry"`` or a composite
    ``"industry::policy_family"`` string; the store treats it as opaque.
    """

    arm_id: str
    wins: float = 0.0
    trials: int = 0

    def to_item(self) -> dict[str, Any]:
        """DDB serialization. Floats -> Decimal (DDB rejects float)."""
        return {
            ARM_PK_ATTR: self.arm_id,
            WINS_ATTR: Decimal(str(self.wins)),
            TRIALS_ATTR: int(self.trials),
        }

    @classmethod
    def from_item(cls, item: dict[str, Any]) -> "BanditArm":
        """DDB / JSON deserialization. Decimal -> float/int; tolerant of nulls."""
        return cls(
            arm_id=str(item[ARM_PK_ATTR]),
            wins=float(item.get(WINS_ATTR, 0.0) or 0.0),
            trials=int(item.get(TRIALS_ATTR, 0) or 0),
        )


# ---------------------------------------------------------------------------
# Storage backends
# ---------------------------------------------------------------------------


class _JsonBanditBackend:
    """Local JSON-file backend. Process-local lock; atomic rename on save.

    Multi-process safety relies on the whole-file atomic rename: each ``add``
    re-reads, merges, and renames into place. Safe for the host venv + a
    single test process. Genuine multi-writer concurrency (host process +
    container process at the same instant) is what the DDB backend's atomic
    ``ADD`` is for; this JSON path is the dev / test / DDB-down fallback.
    """

    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.Lock()

    def _read_unlocked(self) -> dict[str, Any]:
        if not os.path.exists(self.path):
            return {}
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                return json.load(f) or {}
        except (OSError, ValueError) as exc:
            _LOG.warning("bandit_store json load failed: %s", exc)
            return {}

    def read_all(self) -> dict[str, BanditArm]:
        with self._lock:
            data = self._read_unlocked()
        out: dict[str, BanditArm] = {}
        for arm_id, row in data.items():
            try:
                merged = dict(row or {})
                merged[ARM_PK_ATTR] = arm_id
                out[arm_id] = BanditArm.from_item(merged)
            except (KeyError, TypeError, ValueError) as exc:
                _LOG.warning("bandit_store json row %s malformed: %s", arm_id, exc)
        return out

    def add(self, arm_id: str, wins_delta: float, trials_delta: int) -> bool:
        """Read-modify-write one arm's counters. Returns success."""
        with self._lock:
            data = self._read_unlocked()
            row = data.get(arm_id) or {}
            new_wins = float(row.get(WINS_ATTR, 0.0) or 0.0) + float(wins_delta)
            new_trials = int(row.get(TRIALS_ATTR, 0) or 0) + int(trials_delta)
            data[arm_id] = {WINS_ATTR: new_wins, TRIALS_ATTR: new_trials}
            try:
                os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
                tmp = f"{self.path}.tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2, default=str)
                os.replace(tmp, self.path)
                return True
            except OSError as exc:
                _LOG.warning("bandit_store json save failed: %s", exc)
                return False

    def clear(self) -> bool:
        """Truncate the JSON file. Used by ``reset_bandit()`` in tests."""
        with self._lock:
            try:
                os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
                tmp = f"{self.path}.tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump({}, f)
                os.replace(tmp, self.path)
                return True
            except OSError as exc:
                _LOG.warning("bandit_store json clear failed: %s", exc)
                return False


class _DdbBanditBackend:
    """DynamoDB backend. One row per arm, atomic ADD for counter increments.

    Schema: PK=``arm_id`` (string), attributes ``wins`` (Decimal) and
    ``trials`` (Number). No SK. ``add`` uses ``UpdateExpression`` with the
    ``ADD`` action so two processes incrementing the same arm concurrently
    both land — DynamoDB serializes the ADDs server-side, no race window and
    no last-writer-wins clobber.
    """

    def __init__(self, table_name: str, region: str) -> None:
        self.table_name = table_name
        self.region = region

    def _table(self) -> Any:
        from backend.common import aws  # local import so JSON fallback works without boto3

        return aws.table(self.table_name, self.region)

    def read_all(self) -> dict[str, BanditArm] | None:
        """Scan every arm row. Returns None on failure (caller falls back)."""
        try:
            table = self._table()
            items: list[dict[str, Any]] = []
            resp = table.scan()
            items.extend(resp.get("Items") or [])
            # The arm count is small (single industries + their policy
            # families), but page defensively in case a future build grows it.
            while "LastEvaluatedKey" in resp:
                resp = table.scan(ExclusiveStartKey=resp["LastEvaluatedKey"])
                items.extend(resp.get("Items") or [])
        except Exception as exc:  # noqa: BLE001 - degraded path
            _LOG.warning("bandit_store ddb scan failed: %s", exc)
            return None
        out: dict[str, BanditArm] = {}
        for item in items:
            try:
                arm = BanditArm.from_item(item)
            except (KeyError, TypeError, ValueError) as exc:
                _LOG.warning("bandit_store ddb row malformed: %s", exc)
                continue
            out[arm.arm_id] = arm
        return out

    def add(self, arm_id: str, wins_delta: float, trials_delta: int) -> bool:
        """Atomic ``ADD`` on wins + trials. Concurrency-safe across processes."""
        try:
            self._table().update_item(
                Key={ARM_PK_ATTR: arm_id},
                UpdateExpression=f"ADD {WINS_ATTR} :w, {TRIALS_ATTR} :t",
                ExpressionAttributeValues={
                    ":w": Decimal(str(wins_delta)),
                    ":t": int(trials_delta),
                },
            )
            return True
        except Exception as exc:  # noqa: BLE001 - degraded path
            _LOG.warning("bandit_store ddb add failed: %s", exc)
            return False


# ---------------------------------------------------------------------------
# Store (public API)
# ---------------------------------------------------------------------------


class BanditStore:
    """Durable bandit store. DDB primary, JSON fallback, never raises.

    Construction does NO I/O — backends are lazily probed on first call, so
    the store can be built at import time in environments without AWS.
    Matches the :class:`backend.common.llm_budget.LlmBudgetStore` contract.

    Source-of-truth is the store. The in-memory ``_cache`` is purely a
    short-TTL read optimisation for a hot decide loop: every ``add`` writes
    the store first and then invalidates the cache, so a stale cache can
    never outlive a write.
    """

    def __init__(
        self,
        *,
        ddb_table: str | None = None,
        region: str | None = None,
        json_path: str | None = None,
        cache_ttl_sec: float = _DEFAULT_CACHE_TTL_SEC,
        now_func: Callable[[], float] | None = None,
    ) -> None:
        # ``ddb_table=""`` is the explicit "disable DDB" signal (mirrors the
        # llm_budget / triggers convention); only fall through to the env var
        # when the caller passed nothing (None).
        if ddb_table is None:
            self._ddb_table = os.environ.get(
                "DDB_STRATEGY_BANDIT_TABLE",
                _DEFAULT_DDB_TABLE,
            )
        else:
            self._ddb_table = ddb_table
        self._region = region or os.environ.get("AWS_REGION", _DEFAULT_AWS_REGION)
        self._json_path = json_path or bandit_store_path()
        self._json_backend = _JsonBanditBackend(self._json_path)
        self._ddb_backend: _DdbBanditBackend | None = None
        # Empty DDB table name disables the DDB path entirely (tests + dev).
        if self._ddb_table:
            self._ddb_backend = _DdbBanditBackend(self._ddb_table, self._region)
        self._cache_ttl = float(cache_ttl_sec)
        self._now: Callable[[], float] = now_func or time.time
        self._lock = threading.Lock()
        self._cache: dict[str, BanditArm] | None = None
        self._cache_at: float = 0.0

    # -- public API --------------------------------------------------------

    def read_all(self) -> dict[str, BanditArm]:
        """Return every arm's current counters. Never raises; ``{}`` on failure.

        Read-through cache: a cache fresher than ``cache_ttl_sec`` is returned
        directly; otherwise the store is re-read (DDB first, then JSON). The
        store is always the source of truth — the cache only absorbs repeated
        reads inside one decide pass.
        """
        now = self._now()
        with self._lock:
            if self._cache is not None and (now - self._cache_at) < self._cache_ttl:
                return {k: BanditArm(v.arm_id, v.wins, v.trials) for k, v in self._cache.items()}

        arms: dict[str, BanditArm] | None = None
        try:
            if self._ddb_backend is not None:
                arms = self._ddb_backend.read_all()
            if arms is None:
                arms = self._json_backend.read_all()
        except Exception as exc:  # noqa: BLE001 - never raise out of the store
            _LOG.warning("bandit_store read_all failed (%s); degrading to empty", exc)
            arms = {}

        with self._lock:
            self._cache = {k: BanditArm(v.arm_id, v.wins, v.trials) for k, v in arms.items()}
            self._cache_at = self._now()
        return arms

    def add(self, arm_id: str, wins_delta: float, trials_delta: int) -> bool:
        """Atomically add to one arm's ``wins`` / ``trials``. Never raises.

        Writes the store first (DDB atomic ADD if configured, else the JSON
        fallback), then invalidates the cache so the next ``read_all`` re-reads
        the authoritative state. Returns whether a backend accepted the write;
        a ``False`` return is a logged best-effort miss, not an exception.
        """
        wrote = False
        try:
            if self._ddb_backend is not None:
                wrote = self._ddb_backend.add(arm_id, wins_delta, trials_delta)
            if not wrote:
                # DDB unconfigured or its write failed — the JSON fallback is
                # the source of truth in dev / test and the safety net in prod.
                wrote = self._json_backend.add(arm_id, wins_delta, trials_delta)
        except Exception as exc:  # noqa: BLE001 - never raise out of the store
            _LOG.warning("bandit_store add failed (%s); arm=%s dropped", exc, arm_id)
            wrote = False
        with self._lock:
            self._cache = None
            self._cache_at = 0.0
        return wrote

    def clear(self) -> None:
        """Clear the JSON fallback file + the cache. Test-isolation helper.

        Intentionally does NOT wipe real DynamoDB — production bandit history
        must survive a test run. ``reset_bandit()`` calls this; in the test
        suite (``DDB_STRATEGY_BANDIT_TABLE=""``) the JSON file IS the store,
        so this fully resets state.
        """
        self._json_backend.clear()
        with self._lock:
            self._cache = None
            self._cache_at = 0.0


# ---------------------------------------------------------------------------
# Settings-bound path + process-global accessor
# ---------------------------------------------------------------------------


def bandit_store_path() -> str:
    """JSON fallback path. ``SAMUS_STRATEGY_BANDIT_PATH`` overrides the default.

    Mirrors ``triggers.snapshot_store_path()`` / ``SAMUS_LLM_BUDGET_PATH`` —
    an env-var-resolved on-disk path under the Samus data tree.
    """
    return os.environ.get("SAMUS_STRATEGY_BANDIT_PATH", _DEFAULT_JSON_PATH)


_DEFAULT_STORE: BanditStore | None = None
_STORE_LOCK = threading.Lock()


def get_default_store() -> BanditStore:
    """Lazily build the process-wide :class:`BanditStore` from settings.

    Reads ``ddb_strategy_bandit_table`` from :func:`backend.common.config.
    get_settings` so the table name flows from the same config surface as the
    other ``ddb_*_table`` fields. Falls back to env-var / default resolution
    if settings can't be loaded (keeps the store usable in bare-import tests).
    """
    global _DEFAULT_STORE
    with _STORE_LOCK:
        if _DEFAULT_STORE is not None:
            return _DEFAULT_STORE
        ddb_table: str | None = None
        region: str | None = None
        try:
            from backend.common.config import get_settings

            s = get_settings()
            # ``""`` (DDB disabled) is a meaningful value — pass it through as
            # the empty string so BanditStore disables DDB, rather than None
            # which would re-trigger env-var lookup.
            ddb_table = s.ddb_strategy_bandit_table
            region = s.aws_region
        except Exception as exc:  # noqa: BLE001 - degrade to env-var resolution
            _LOG.debug("bandit_store: settings unavailable (%s); using env", exc)
        _DEFAULT_STORE = BanditStore(ddb_table=ddb_table, region=region)
        return _DEFAULT_STORE


def set_default_store(store: BanditStore | None) -> None:
    """Swap (or drop) the process-wide store. Tests + secret-rotation tooling."""
    global _DEFAULT_STORE
    with _STORE_LOCK:
        _DEFAULT_STORE = store


def reset_store() -> None:
    """Drop the cached singleton so the next call rebuilds from settings."""
    set_default_store(None)


__all__ = [
    "BanditArm",
    "BanditStore",
    "bandit_store_path",
    "get_default_store",
    "set_default_store",
    "reset_store",
    "ARM_PK_ATTR",
    "WINS_ATTR",
    "TRIALS_ATTR",
]
