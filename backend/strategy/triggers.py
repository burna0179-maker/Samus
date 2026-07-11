"""Event-driven trigger layer for the portfolio re-plan LLM call (Lever 3.2).

The portfolio LLM call (``backend.strategy.portfolio_manager.propose_allocation``)
is expensive — one Anthropic round-trip per fire — so it must NOT fire on a
periodic timer. This module wraps it in a tiny rule engine that walks five
*signal-change* conditions every tick (default 15 min); the first true
condition fires ``propose_allocation()`` and the rest short-circuit. Cadence
target: 5–15 fires/day under normal load.

Six trigger conditions (walked in order)
----------------------------------------
1. ``pipeline_ev_step``   — total EV across active prospects shifted by
   >= ``portfolio_min_signal_change`` * previous-total between snapshots.
2. ``forecast_density``    — predictive: ``should_proactively_shift`` is true
   for the supplied industry forecasts. Walked BEFORE ``bandit_divergence``
   because prediction beats reaction — shift capital before the reactive
   bandit accumulates regret.
3. ``bandit_divergence``  — top-1 arm flipped in any bandit family since
   the last snapshot.
4. ``new_cohort``          — a fresh batch of prospects whose combined
   ``lead_score`` exceeds the current pipeline median entered the pipeline.
5. ``operator_manual``     — explicit manual fire via ``manual_signal()``;
   rate-limited to 1/hour by an in-process token bucket so an operator
   mashing the button can't blow the $1/day cap.
6. ``budget_recovery``     — ``efficiency_ema`` < 0.4 on prospecting OR
   seo workcells; force a re-rank to reduce wasted spend.

Persistence
-----------
Snapshots and the trigger audit log persist via a DDB-or-JSON pattern that
mirrors :mod:`backend.common.llm_budget` (PK=``bucket_day`` (YYYY-MM-DD),
one row per UTC day so old snapshots roll off naturally). DDB table:
``samus_portfolio_snapshots``. Local fallback path:
``SAMUS_PORTFOLIO_SNAPSHOT_PATH``; default
``/opt/samus/data/strategy/portfolio_snapshots.json``.

Trigger fire events are appended to a JSONL ledger at
``SAMUS_PORTFOLIO_TRIGGER_LEDGER`` (default
``/opt/samus/data/strategy/portfolio_triggers.jsonl``) for forensic /
debug purposes.

Budget gate
-----------
Each fire calls ``propose_allocation()`` which already routes through
``backend.common.llm_client.anthropic_messages`` with
``workcell="strategy"`` — the budget gate is NOT bypassed. If the gate
denies the call, ``propose_allocation`` returns a fail-closed empty
decision and the trigger layer records a ``budget_denied=True`` audit
entry but still re-snapshots so the next tick doesn't immediately
re-fire on the same delta.

Worker tick
-----------
``start_tick_loop()`` spawns a daemon ``threading.Timer`` chain that calls
``check_and_fire()`` every ``portfolio_tick_interval_sec`` seconds (default
900s / 15 min). The chain is cancel-safe via the returned ``stop`` handle;
intended to be wired into a FastAPI lifespan or a CLI long-runner.
"""
from __future__ import annotations

import json
import logging
import os
import statistics
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

__all__ = [
    "PortfolioSnapshot",
    "TriggerEvent",
    "TriggerContext",
    "RateLimiter",
    "SnapshotStore",
    "check_pipeline_ev_step",
    "check_forecast_density",
    "check_bandit_divergence",
    "check_new_cohort",
    "check_operator_manual",
    "check_budget_recovery",
    "check_and_fire",
    "manual_signal",
    "start_tick_loop",
    "trigger_ledger_path",
    "snapshot_store_path",
    "TRIGGER_NAMES",
]

_LOG = logging.getLogger("samus.strategy.triggers")

TRIGGER_NAMES: tuple[str, ...] = (
    "pipeline_ev_step",
    "forecast_density",
    "bandit_divergence",
    "new_cohort",
    "operator_manual",
    "budget_recovery",
)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class PortfolioSnapshot:
    """In-memory shape of one daily snapshot row. Mirrors the DDB schema.

    ``bucket_day`` is the partition key (YYYY-MM-DD, UTC); ``ts`` is the
    write-time of the most recent snapshot inside that day so successive
    re-snapshots within the day overwrite the row but keep the daily-roll
    semantics intact.
    """

    bucket_day: str = ""
    ts: str = ""
    ev_total: float = 0.0
    prospect_count: int = 0
    pipeline_median_score: float = 0.0
    bandit_top_arms: dict[str, str] = field(default_factory=dict)
    efficiency_ema_by_workcell: dict[str, float] = field(default_factory=dict)
    prospect_ids: list[str] = field(default_factory=list)

    def to_item(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_item(cls, item: dict[str, Any]) -> "PortfolioSnapshot":
        return cls(
            bucket_day=str(item.get("bucket_day", "") or ""),
            ts=str(item.get("ts", "") or ""),
            ev_total=float(item.get("ev_total", 0.0) or 0.0),
            prospect_count=int(item.get("prospect_count", 0) or 0),
            pipeline_median_score=float(item.get("pipeline_median_score", 0.0) or 0.0),
            bandit_top_arms=dict(item.get("bandit_top_arms") or {}),
            efficiency_ema_by_workcell=dict(item.get("efficiency_ema_by_workcell") or {}),
            prospect_ids=list(item.get("prospect_ids") or []),
        )


@dataclass(frozen=True)
class TriggerEvent:
    """Audit record for one trigger fire.

    ``name`` is the trigger that fired (one of :data:`TRIGGER_NAMES`).
    ``detail`` captures the deciding measurements so an operator can
    reconstruct *why* this fire happened from the ledger alone.
    """

    name: str
    fired_at: str
    detail: dict[str, Any]
    budget_denied: bool = False


@dataclass
class TriggerContext:
    """All inputs ``check_and_fire`` needs to evaluate the 5 triggers.

    Built by the caller from the current pipeline + bandit + budget state.
    Passing it explicitly (rather than reading from globals inside the
    triggers module) makes the module pure-function-friendly and trivially
    testable.
    """

    prospects: list[dict[str, Any]] = field(default_factory=list)
    market_signals: dict[str, Any] = field(default_factory=dict)
    bandit_stats: dict[str, dict[str, float]] = field(default_factory=dict)
    efficiency_ema_by_workcell: dict[str, float] = field(default_factory=dict)
    budget_remaining: float = 0.0
    avg_conversion_rate: float = 0.0
    pipeline_value: float = 0.0
    # Predictive industry forecasts for the forecast_density trigger.
    # Defaults empty so existing callers that don't supply it still work.
    industry_forecasts: list[Any] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Helpers (utc clock isolated so tests can monkeypatch)
# ---------------------------------------------------------------------------

def _today_utc() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _score_opportunity(prospect: dict[str, Any]) -> float:
    """EV-per-prospect for the pipeline_ev_step check.

    Mirrors ``StrategyEngine._score_opportunity``: ``lead_score`` weight 0.6,
    ``conversion_signals`` count weight 5, ``seo_score`` inverted (100-x)
    weight 0.2, clamped to >=0. Inputs are read from the prospect dict.
    """
    lead = float(prospect.get("lead_score", 0.0) or 0.0)
    seo = float(prospect.get("seo_score", 100.0) or 100.0)
    signals = prospect.get("conversion_signals") or []
    raw = (lead * 0.6) + (len(signals) * 5.0) + ((100.0 - seo) * 0.2)
    return max(raw, 0.0)


def _bandit_top_arms(stats: dict[str, dict[str, float]]) -> dict[str, str]:
    """Pick the top-1 arm per *family* (arm prefix before the first ``:``).

    Bandit arm ids in the existing portfolio_manager are flat strings like
    ``"accelerate"``, ``"defer"``. We treat the family as the substring up
    to the first ``:`` so the trigger remains correct when future code
    promotes the bandit to ``"family:arm"`` keys without a schema change.
    """
    families: dict[str, tuple[str, float]] = {}
    for arm_id, row in stats.items():
        trials = float(row.get("trials", 0) or 0)
        wins = float(row.get("wins", 0.0) or 0.0)
        if trials <= 0:
            continue
        mean = wins / trials
        family = arm_id.split(":", 1)[0] if ":" in arm_id else "_default"
        prev = families.get(family)
        if prev is None or mean > prev[1]:
            families[family] = (arm_id, mean)
    return {family: arm for family, (arm, _mean) in families.items()}


def trigger_ledger_path() -> str:
    return os.environ.get(
        "SAMUS_PORTFOLIO_TRIGGER_LEDGER",
        "/opt/samus/data/strategy/portfolio_triggers.jsonl",
    )


def snapshot_store_path() -> str:
    return os.environ.get(
        "SAMUS_PORTFOLIO_SNAPSHOT_PATH",
        "/opt/samus/data/strategy/portfolio_snapshots.json",
    )


def _append_trigger_event(event: TriggerEvent) -> None:
    """Best-effort append to the trigger JSONL ledger. Never raises."""
    path = trigger_ledger_path()
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    except OSError as exc:  # pragma: no cover - defensive
        _LOG.debug("triggers: ledger dir create skipped: %s", exc)
        return
    record = {
        "name": event.name,
        "fired_at": event.fired_at,
        "detail": event.detail,
        "budget_denied": event.budget_denied,
    }
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")
    except OSError as exc:  # pragma: no cover - defensive
        _LOG.warning("triggers: ledger append failed: %s", exc)


# ---------------------------------------------------------------------------
# Rate limiter (operator_manual: 1 fire / hour, in-process token bucket)
# ---------------------------------------------------------------------------

class RateLimiter:
    """Tiny in-process leaky-bucket. One slot, refilled every ``interval_sec``.

    Process-local on purpose — we want a hard cap against accidental-mash;
    multi-process coordination would add an SQS / DDB round-trip we don't
    need (an operator typing on one keyboard can't multi-press across two
    processes).
    """

    def __init__(self, interval_sec: float = 3600.0) -> None:
        self.interval_sec = float(interval_sec)
        self._last_fired_at: float = 0.0
        self._lock = threading.Lock()
        self._clock: Callable[[], float] = time.time

    def set_clock(self, clock: Callable[[], float]) -> None:
        """Inject a deterministic clock for tests."""
        self._clock = clock

    def try_acquire(self) -> bool:
        with self._lock:
            now = self._clock()
            if (now - self._last_fired_at) < self.interval_sec:
                return False
            self._last_fired_at = now
            return True

    def reset(self) -> None:
        with self._lock:
            self._last_fired_at = 0.0


# Operator-manual flag + rate limiter. Module-level singletons so the
# manual_signal() entry point is callable from anywhere (HTTP route, CLI,
# admin shell) without plumbing through.
_MANUAL_SIGNAL: threading.Event = threading.Event()
_MANUAL_RATE_LIMITER: RateLimiter = RateLimiter(interval_sec=3600.0)


def manual_signal() -> None:
    """Mark the operator-manual flag. The next ``check_and_fire`` consumes it.

    Idempotent — calling multiple times before the next tick still only
    arms the flag once. The 1/hour rate-limit is applied inside the
    ``check_operator_manual`` check, not here, so callers always get a
    successful "noted" response.
    """
    _MANUAL_SIGNAL.set()


def _manual_signal_armed() -> bool:
    return _MANUAL_SIGNAL.is_set()


def _consume_manual_signal() -> None:
    _MANUAL_SIGNAL.clear()


# ---------------------------------------------------------------------------
# Snapshot store (DDB + JSON fallback; mirrors llm_budget._JsonBackend)
# ---------------------------------------------------------------------------

class _JsonSnapshotBackend:
    """Local JSON-file backend. Process-local lock; atomic rename on save."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.Lock()

    def load(self, bucket_day: str) -> PortfolioSnapshot | None:
        with self._lock:
            if not os.path.exists(self.path):
                return None
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    data = json.load(f) or {}
            except (OSError, ValueError) as exc:
                _LOG.warning("triggers: snapshot json load failed: %s", exc)
                return None
            entry = data.get(bucket_day)
            if not entry:
                return None
            return PortfolioSnapshot.from_item(entry)

    def load_latest(self) -> PortfolioSnapshot | None:
        """Return the snapshot with the largest bucket_day key, or None."""
        with self._lock:
            if not os.path.exists(self.path):
                return None
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    data = json.load(f) or {}
            except (OSError, ValueError):
                return None
            if not data:
                return None
            latest_key = max(data.keys())
            return PortfolioSnapshot.from_item(data[latest_key])

    def save(self, snap: PortfolioSnapshot) -> bool:
        with self._lock:
            data: dict[str, Any] = {}
            if os.path.exists(self.path):
                try:
                    with open(self.path, "r", encoding="utf-8") as f:
                        data = json.load(f) or {}
                except (OSError, ValueError):
                    data = {}
            data[snap.bucket_day] = snap.to_item()
            try:
                os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
                tmp = f"{self.path}.tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2, default=str)
                os.replace(tmp, self.path)
                return True
            except OSError as exc:
                _LOG.warning("triggers: snapshot json save failed: %s", exc)
                return False


class _DdbSnapshotBackend:
    """DynamoDB backend. PK=bucket_day."""

    def __init__(self, table_name: str, region: str) -> None:
        self.table_name = table_name
        self.region = region

    def _table(self) -> Any:
        from backend.common import aws  # local import so JSON fallback works without boto3
        return aws.table(self.table_name, self.region)

    def load(self, bucket_day: str) -> PortfolioSnapshot | None:
        try:
            resp = self._table().get_item(Key={"bucket_day": bucket_day})
        except Exception as exc:  # noqa: BLE001 - degraded path
            _LOG.warning("triggers: snapshot ddb load failed: %s", exc)
            return None
        item = resp.get("Item")
        if not item:
            return None
        try:
            return PortfolioSnapshot.from_item(item)
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("triggers: snapshot ddb row malformed: %s", exc)
            return None

    def load_latest(self) -> PortfolioSnapshot | None:
        # Scan is fine — single-digit rows/day, ttl-aged
        try:
            resp = self._table().scan(Limit=32)
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("triggers: snapshot ddb scan failed: %s", exc)
            return None
        items = resp.get("Items") or []
        if not items:
            return None
        items.sort(key=lambda r: str(r.get("bucket_day", "")), reverse=True)
        try:
            return PortfolioSnapshot.from_item(items[0])
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("triggers: snapshot ddb latest malformed: %s", exc)
            return None

    def save(self, snap: PortfolioSnapshot) -> bool:
        try:
            self._table().put_item(Item=snap.to_item())
            return True
        except Exception as exc:  # noqa: BLE001 - degraded path
            _LOG.warning("triggers: snapshot ddb save failed: %s", exc)
            return False


class SnapshotStore:
    """Public facade. Selects DDB backend when configured, else JSON fallback.

    Construction does NO I/O — backends are lazily probed on first call.
    Designed so it can be instantiated at import time without blowing up
    in environments that don't have AWS reachable, matching the
    :class:`backend.common.llm_budget.LlmBudgetStore` contract.
    """

    def __init__(
        self,
        *,
        ddb_table: str | None = None,
        region: str | None = None,
        json_path: str | None = None,
    ) -> None:
        # ``ddb_table=""`` is the explicit "disable DDB" signal (mirrors the
        # llm_budget convention); only fall through to the env var when the
        # caller didn't pass anything (None).
        if ddb_table is None:
            self._ddb_table = os.environ.get(
                "DDB_PORTFOLIO_SNAPSHOTS_TABLE", "samus_portfolio_snapshots",
            )
        else:
            self._ddb_table = ddb_table
        self._region = region or os.environ.get("AWS_REGION", "us-west-1")
        self._json_path = json_path or snapshot_store_path()
        self._json_backend = _JsonSnapshotBackend(self._json_path)
        self._ddb_backend: _DdbSnapshotBackend | None = None
        # Empty DDB table name disables the DDB path entirely (tests +
        # local dev). Matches the llm_budget convention.
        if self._ddb_table:
            self._ddb_backend = _DdbSnapshotBackend(self._ddb_table, self._region)

    # --- public API ---

    def load(self, bucket_day: str | None = None) -> PortfolioSnapshot | None:
        key = bucket_day or _today_utc()
        if self._ddb_backend is not None:
            snap = self._ddb_backend.load(key)
            if snap is not None:
                return snap
        return self._json_backend.load(key)

    def load_latest(self) -> PortfolioSnapshot | None:
        if self._ddb_backend is not None:
            snap = self._ddb_backend.load_latest()
            if snap is not None:
                return snap
        return self._json_backend.load_latest()

    def save(self, snap: PortfolioSnapshot) -> bool:
        if not snap.bucket_day:
            snap.bucket_day = _today_utc()
        if not snap.ts:
            snap.ts = _iso_now()
        wrote = False
        if self._ddb_backend is not None:
            wrote = self._ddb_backend.save(snap) or wrote
        # Always mirror to the JSON path so a DDB outage doesn't lose
        # snapshot continuity (and the JSON path is the source-of-truth
        # in dev / test where DDB is disabled).
        wrote = self._json_backend.save(snap) or wrote
        return wrote


# Module-level default store. Tests override via ``set_default_store``.
_DEFAULT_STORE: SnapshotStore | None = None


def get_default_store() -> SnapshotStore:
    global _DEFAULT_STORE
    if _DEFAULT_STORE is None:
        _DEFAULT_STORE = SnapshotStore()
    return _DEFAULT_STORE


def set_default_store(store: SnapshotStore | None) -> None:
    """Replace the module-level store (tests use this for tmp_path injection)."""
    global _DEFAULT_STORE
    _DEFAULT_STORE = store


# ---------------------------------------------------------------------------
# Snapshot derivation from a TriggerContext
# ---------------------------------------------------------------------------

def derive_snapshot(ctx: TriggerContext) -> PortfolioSnapshot:
    """Compute a snapshot from the current context. Pure function."""
    ev_total = sum(_score_opportunity(p) for p in ctx.prospects)
    lead_scores = [float(p.get("lead_score", 0.0) or 0.0) for p in ctx.prospects]
    pipeline_median = float(statistics.median(lead_scores)) if lead_scores else 0.0
    return PortfolioSnapshot(
        bucket_day=_today_utc(),
        ts=_iso_now(),
        ev_total=float(ev_total),
        prospect_count=len(ctx.prospects),
        pipeline_median_score=pipeline_median,
        bandit_top_arms=_bandit_top_arms(ctx.bandit_stats),
        efficiency_ema_by_workcell=dict(ctx.efficiency_ema_by_workcell),
        prospect_ids=[str(p.get("prospect_id") or p.get("id") or "") for p in ctx.prospects],
    )


# ---------------------------------------------------------------------------
# The five trigger checks — pure booleans
# ---------------------------------------------------------------------------

def check_pipeline_ev_step(
    snapshot: PortfolioSnapshot,
    prev_snapshot: PortfolioSnapshot | None,
    *,
    min_change: float,
) -> bool:
    """True when EV total shifted by >= ``min_change`` * prev_total.

    On first snapshot (no prev), returns False — we don't fire on the
    bootstrap pass. ``min_change`` is a fraction (0.15 -> 15%).
    """
    if prev_snapshot is None:
        return False
    prev_total = float(prev_snapshot.ev_total or 0.0)
    if prev_total <= 0.0:
        # Going from zero EV to any positive EV is a meaningful change
        # — fire when current crosses a $1 floor (avoids float noise).
        return snapshot.ev_total >= 1.0
    delta = abs(snapshot.ev_total - prev_total)
    return delta >= (min_change * prev_total)


def check_forecast_density(
    industry_forecasts: list[Any],
    *,
    threshold: float | None = None,
) -> bool:
    """True when the predictive allocator says a proactive shift is warranted.

    Delegates to :func:`backend.strategy.predictive_allocator.should_proactively_shift`.
    With no forecasts supplied this returns False — no signal to act on, so
    existing callers that never populate ``industry_forecasts`` never fire
    this trigger. Pure boolean; deterministic.
    """
    if not industry_forecasts:
        return False
    from .predictive_allocator import (
        PROACTIVE_SHIFT_THRESHOLD_DEFAULT,
        should_proactively_shift,
    )
    eff_threshold = (
        PROACTIVE_SHIFT_THRESHOLD_DEFAULT if threshold is None else threshold
    )
    return should_proactively_shift(industry_forecasts, threshold=eff_threshold)


def check_bandit_divergence(
    current_bandit_stats: dict[str, dict[str, float]],
    prev_bandit_stats: dict[str, dict[str, float]] | None,
) -> bool:
    """True when the top-1 arm changes in ANY family.

    Adding a new family also counts as divergence (the family didn't exist
    in the prev snapshot, so its top arm definitionally "changed" from
    nothing to something). On first call (prev=None) returns False so we
    don't fire on bootstrap.
    """
    if prev_bandit_stats is None:
        return False
    cur_top = _bandit_top_arms(current_bandit_stats)
    prev_top = _bandit_top_arms(prev_bandit_stats)
    # Any family present in cur whose top-1 differs (or didn't exist before)
    # counts. We don't fire on family-disappearance (i.e. a family that was
    # in prev but not in cur) — that's a downstream cleanup, not a signal
    # change worth a $0.005 LLM call.
    for family, arm in cur_top.items():
        if prev_top.get(family) != arm:
            return True
    return False


def check_new_cohort(
    prospects: list[dict[str, Any]],
    prev_snapshot: PortfolioSnapshot | None,
    pipeline_median_score: float,
) -> bool:
    """True when a NEW cohort's combined lead_score exceeds the pipeline median.

    "New cohort" = prospects whose ids are not in the previous snapshot's
    ``prospect_ids`` list. "Combined" = sum of their lead_scores. Compared
    against the (positive) current pipeline median; if median is 0 we fall
    back to "any new cohort with combined lead > 0".

    On first call (no prev snapshot) returns False — first observation
    can't be a "new" cohort by definition.
    """
    if prev_snapshot is None:
        return False
    prev_ids = set(prev_snapshot.prospect_ids or ())
    new_cohort = [
        p for p in prospects
        if (str(p.get("prospect_id") or p.get("id") or "")) not in prev_ids
        and (str(p.get("prospect_id") or p.get("id") or ""))
    ]
    if not new_cohort:
        return False
    combined_lead = sum(float(p.get("lead_score", 0.0) or 0.0) for p in new_cohort)
    if pipeline_median_score <= 0.0:
        return combined_lead > 0.0
    return combined_lead > pipeline_median_score


def check_operator_manual(rate_limiter: RateLimiter | None = None) -> bool:
    """True when the manual signal is armed AND the rate limit allows.

    Rate-limiting consumes a token only when the signal is armed AND the
    limiter says yes — otherwise an idle process steadily drains tokens.
    The signal flag is cleared by ``check_and_fire`` after the fire so
    one ``manual_signal()`` call yields exactly one fire (or zero, if the
    rate limit blocks).
    """
    if not _manual_signal_armed():
        return False
    limiter = rate_limiter or _MANUAL_RATE_LIMITER
    return limiter.try_acquire()


def check_budget_recovery(
    efficiency_ema_by_workcell: dict[str, float],
    *,
    threshold: float = 0.4,
    watch_workcells: tuple[str, ...] = ("prospecting", "seo"),
) -> bool:
    """True when ``efficiency_ema`` < ``threshold`` on ANY watched workcell.

    Re-rank the portfolio to reduce wasted spend on the failing workcell's
    arms. Default watch list = prospecting + seo because those two carry
    the most expensive per-call cost; missing keys are treated as "no
    signal yet" (don't fire — wait for real data).
    """
    for workcell in watch_workcells:
        ema = efficiency_ema_by_workcell.get(workcell)
        if ema is None:
            continue
        if float(ema) < float(threshold):
            return True
    return False


# ---------------------------------------------------------------------------
# Walker: check_and_fire
# ---------------------------------------------------------------------------

def _do_fire(
    ctx: TriggerContext,
    *,
    name: str,
    detail: dict[str, Any],
    api_key: str | None,
    propose_fn: Callable[..., Any] | None,
) -> TriggerEvent:
    """Call propose_allocation, record the audit event, return it."""
    from .portfolio_manager import PortfolioState
    fn = propose_fn
    if fn is None:
        from .portfolio_manager import propose_allocation as _pa
        fn = _pa

    state = PortfolioState(
        budget_remaining=float(ctx.budget_remaining),
        avg_conversion_rate=float(ctx.avg_conversion_rate),
        pipeline_value=float(ctx.pipeline_value),
        active_prospects=len(ctx.prospects),
        prospects=list(ctx.prospects),
    )

    decision = fn(state, ctx.market_signals, api_key=api_key)
    budget_denied = bool(getattr(decision, "parse_error", False)) and not getattr(
        decision, "raw_text", "",
    )

    enriched_detail = dict(detail)
    enriched_detail.setdefault("priorities_count", len(getattr(decision, "priorities", []) or []))
    enriched_detail.setdefault(
        "deprioritize_count", len(getattr(decision, "deprioritize", []) or []),
    )
    enriched_detail.setdefault("actions_count", len(getattr(decision, "actions", []) or []))
    enriched_detail.setdefault("parse_error", bool(getattr(decision, "parse_error", False)))

    event = TriggerEvent(
        name=name,
        fired_at=_iso_now(),
        detail=enriched_detail,
        budget_denied=budget_denied,
    )
    _append_trigger_event(event)
    _LOG.info(
        "triggers: fired name=%s parse_error=%s budget_denied=%s",
        name,
        enriched_detail["parse_error"],
        budget_denied,
    )
    return event


def check_and_fire(
    ctx: TriggerContext,
    *,
    store: SnapshotStore | None = None,
    min_signal_change: float = 0.15,
    api_key: str | None = None,
    propose_fn: Callable[..., Any] | None = None,
    rate_limiter: RateLimiter | None = None,
) -> TriggerEvent | None:
    """Walk the 6 triggers in order; fire on the first true; persist new snapshot.

    Returns the :class:`TriggerEvent` on fire, or None when no trigger
    matched. Short-circuits on the first true trigger (subsequent checks
    are not evaluated).

    The new snapshot is persisted regardless of whether a fire occurred,
    so the next tick has fresh prev-state to compare against. Operator-
    manual signal is consumed (cleared) whenever the manual check is
    evaluated as true, even if the rate limiter blocks the fire — this
    matches operator intent: "I pressed the button, I expect the result
    or a 'rate-limited' message, not a delayed-fire surprise an hour
    later."
    """
    store = store or get_default_store()
    prev = store.load_latest()
    cur = derive_snapshot(ctx)

    fired: TriggerEvent | None = None

    # 1. pipeline_ev_step
    if check_pipeline_ev_step(cur, prev, min_change=min_signal_change):
        prev_total = float(prev.ev_total) if prev else 0.0
        delta = cur.ev_total - prev_total
        fired = _do_fire(
            ctx,
            name="pipeline_ev_step",
            detail={
                "ev_total": cur.ev_total,
                "prev_ev_total": prev_total,
                "delta": delta,
                "min_change": min_signal_change,
            },
            api_key=api_key,
            propose_fn=propose_fn,
        )

    # 2. forecast_density — predictive trigger, walked BEFORE bandit_divergence
    #    so a forward-looking shift beats the reactive bandit's regret.
    if fired is None and check_forecast_density(ctx.industry_forecasts):
        from .predictive_allocator import forecast_score
        fired = _do_fire(
            ctx,
            name="forecast_density",
            detail={
                "forecast_count": len(ctx.industry_forecasts),
                "top_forecast_score": max(
                    (float(forecast_score(f)) for f in ctx.industry_forecasts),
                    default=0.0,
                ),
            },
            api_key=api_key,
            propose_fn=propose_fn,
        )

    # 3. bandit_divergence
    if fired is None and prev is not None and check_bandit_divergence_against_top(
        ctx.bandit_stats, prev.bandit_top_arms,
    ):
        fired = _do_fire(
            ctx,
            name="bandit_divergence",
            detail={
                "current_top_arms": _bandit_top_arms(ctx.bandit_stats),
                "previous_top_arms": dict(prev.bandit_top_arms),
            },
            api_key=api_key,
            propose_fn=propose_fn,
        )

    # 4. new_cohort
    if fired is None and check_new_cohort(
        ctx.prospects, prev, cur.pipeline_median_score,
    ):
        prev_ids = set(prev.prospect_ids or ()) if prev else set()
        new_count = sum(
            1
            for p in ctx.prospects
            if (str(p.get("prospect_id") or p.get("id") or "")) and (
                str(p.get("prospect_id") or p.get("id") or "")
            ) not in prev_ids
        )
        fired = _do_fire(
            ctx,
            name="new_cohort",
            detail={
                "new_prospect_count": new_count,
                "pipeline_median_score": cur.pipeline_median_score,
            },
            api_key=api_key,
            propose_fn=propose_fn,
        )

    # 5. operator_manual
    if fired is None and check_operator_manual(rate_limiter):
        _consume_manual_signal()
        fired = _do_fire(
            ctx,
            name="operator_manual",
            detail={"reason": "operator-initiated"},
            api_key=api_key,
            propose_fn=propose_fn,
        )
    elif _manual_signal_armed() and fired is None:
        # Manual flag was armed but rate-limited; consume so the user gets
        # a "rate-limited" experience rather than a delayed surprise fire.
        _consume_manual_signal()
        _LOG.info("triggers: operator_manual rate-limited; signal consumed")

    # 6. budget_recovery
    if fired is None and check_budget_recovery(ctx.efficiency_ema_by_workcell):
        fired = _do_fire(
            ctx,
            name="budget_recovery",
            detail={"efficiency_ema": dict(ctx.efficiency_ema_by_workcell)},
            api_key=api_key,
            propose_fn=propose_fn,
        )

    # Persist the new snapshot AFTER all checks so prev is the right shape.
    store.save(cur)
    return fired


def check_bandit_divergence_against_top(
    current_bandit_stats: dict[str, dict[str, float]],
    prev_top_arms: dict[str, str] | None,
) -> bool:
    """Variant of bandit_divergence comparing live stats vs persisted top-arms.

    The snapshot only persists top-arms (not full bandit stats), so this
    is the path used in the walker. Same firing semantics as
    :func:`check_bandit_divergence`: returns True when any family's top-1
    arm differs, False on bootstrap (prev=None).
    """
    if prev_top_arms is None:
        return False
    cur_top = _bandit_top_arms(current_bandit_stats)
    for family, arm in cur_top.items():
        if prev_top_arms.get(family) != arm:
            return True
    return False


# ---------------------------------------------------------------------------
# Worker tick — daemon Timer chain
# ---------------------------------------------------------------------------

class _TickHandle:
    """Returned by :func:`start_tick_loop`. Call ``.stop()`` to cancel the chain."""

    def __init__(self) -> None:
        self._timer: threading.Timer | None = None
        self._lock = threading.Lock()
        self._stopped = False

    def _set(self, timer: threading.Timer) -> None:
        with self._lock:
            if self._stopped:
                timer.cancel()
                return
            self._timer = timer

    def stop(self) -> None:
        with self._lock:
            self._stopped = True
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None

    @property
    def stopped(self) -> bool:
        with self._lock:
            return self._stopped


def start_tick_loop(
    ctx_provider: Callable[[], TriggerContext],
    *,
    interval_sec: float = 900.0,
    store: SnapshotStore | None = None,
    api_key: str | None = None,
    propose_fn: Callable[..., Any] | None = None,
    on_event: Callable[[TriggerEvent | None], None] | None = None,
) -> _TickHandle:
    """Spawn a daemon Timer chain that calls ``check_and_fire`` every tick.

    ``ctx_provider`` is invoked once per tick to assemble the current
    :class:`TriggerContext`. Keeping it as a callable lets the caller
    decide how to gather prospects + bandit stats + efficiency EMAs
    without making this module aware of every workcell's data path.

    Returns a handle whose ``.stop()`` cancels the chain. Designed to be
    wired into FastAPI lifespan (``startup`` -> capture handle,
    ``shutdown`` -> ``handle.stop()``) or a CLI long-runner.
    """
    handle = _TickHandle()

    def _tick() -> None:
        if handle.stopped:
            return
        try:
            ctx = ctx_provider()
            event = check_and_fire(
                ctx,
                store=store,
                api_key=api_key,
                propose_fn=propose_fn,
            )
            if on_event is not None:
                try:
                    on_event(event)
                except Exception as exc:  # noqa: BLE001 - callback must not poison loop
                    _LOG.warning("triggers: on_event callback raised: %s", exc)
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("triggers: tick raised, continuing: %s", exc)
        # Re-arm
        if not handle.stopped:
            next_timer = threading.Timer(interval_sec, _tick)
            next_timer.daemon = True
            handle._set(next_timer)
            next_timer.start()

    first = threading.Timer(interval_sec, _tick)
    first.daemon = True
    handle._set(first)
    first.start()
    return handle
