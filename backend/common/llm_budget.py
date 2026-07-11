"""Per-workcell LLM token budget store with adaptive sliding-scale quota.

Every Anthropic call across the Samus stack books two events through this
module:

  - **pre-flight** ``can_spend(workcell, est_tokens)`` returns ``(allowed,
    reason)``. Callers that get ``allowed=False`` fall back to the templated /
    non-LLM path (mirrors how :mod:`backend.prospecting.callsheet` already
    degrades on Anthropic failure).
  - **post-flight** ``record_spend(workcell, usage, outcome)`` ingests the
    ``usage.input_tokens`` / ``usage.output_tokens`` Anthropic returns on
    every response, plus a task-outcome label.

Adaptive sizing
---------------
Each workcell has a base daily quota (``settings.llm_workcell_base_token_budget``,
default 100_000) scaled by an efficiency factor:

    factor   = 0.5 + 1.5 * efficiency_ema     # 0.0 -> 0.5x, 1.0 -> 2.0x
    quota    = max(base * 0.1, base * factor) # 10% floor, ~2x ceiling

``efficiency_ema`` is an exponentially-weighted moving average of "did the
caller mark this call as a success?" (alpha = ``settings.llm_budget_ema_alpha``,
default 0.05 -> ~60 calls to fully respond to a step change). LLM-call errors
(network, 5xx, parse failure) are tracked separately and **do not** affect
the EMA — a transient outage shouldn't punish the workcell's future quota.

Storage
-------
Single row per workcell in DDB table ``samus_llm_budgets`` (PK=workcell).
Daily counters reset by checking ``bucket_day`` on every read — when today's
date != stored bucket_day, the daily fields are zeroed but the EMA + history
are preserved. Atomic DDB ``UPDATE...ADD`` semantics keep concurrent writes
from the HTTP-app + worker sidecar from clobbering each other.

Local fallback: when AWS isn't reachable (no creds, dev box, test) the store
degrades to a JSON file at ``SAMUS_LLM_BUDGET_PATH`` (default
``/opt/samus/data/llm_budgets.json``). In the worst case where neither AWS
nor the JSON path is writable, ``can_spend`` returns ``True`` — better to
let calls through than block work on persistence failure.

Control C — circuit breaker (token-cost-hardening 2026-05-18)
-------------------------------------------------------------
Two new fields per workcell: ``consecutive_errors`` and ``circuit_open_until``
(ISO8601 UTC). On every recorded ``outcome="error"`` we increment the
counter; once it reaches ``settings.llm_circuit_breaker_threshold``
(default 10) we set ``circuit_open_until = now + cooldown_sec`` (default
300s). Any ``can_spend`` while ``now < circuit_open_until`` is denied with
``reason="circuit_open_until_<iso>"`` — no HTTP call is made, no quota is
charged. ``outcome="success"`` or ``outcome="failure"`` clears both fields
(a single successful call closes the breaker).

Backwards-compat: both new fields default to ``0`` / ``""`` when loaded from
a DDB row written by the pre-hardening code, so the rollout doesn't require
a backfill migration.
"""
from __future__ import annotations

import calendar
import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from typing import Any, Callable


_LOG = logging.getLogger("samus.common.llm_budget")

_DEFAULT_JSON_PATH = "/opt/samus/data/llm_budgets.json"

# Sentinel "workcell" row that carries stack-wide control state (currently the
# freeze-nonessential window). Persisted through the exact same backends as a
# regular row so DDB + JSON fallback both carry it with zero schema work.
_GLOBAL_CONTROLS = "__global_controls__"

# Workcells whose LLM paths keep working through a freeze_nonessential()
# window — revenue-critical / operator-facing surfaces. Everything else is
# "nonessential" for the purposes of the entropy countermeasure.
ESSENTIAL_WORKCELLS: frozenset[str] = frozenset({
    "finance", "gateway", "cash_engine", "crm",
})

# HOTL enforcement clamps: a quota override may not push a workcell below
# 0.25x or above 2.0x of its base daily quota (matches the adaptive ceiling).
QUOTA_OVERRIDE_MIN_FACTOR = 0.25
QUOTA_OVERRIDE_MAX_FACTOR = 2.0
QUOTA_OVERRIDE_MAX_TTL_SEC = 24 * 3600


def _publish_call_counters(workcell: str, input_tokens: int,
                           output_tokens: int, outcome: str) -> None:
    """Increment cumulative Prometheus counters by this call's deltas."""
    try:
        from . import metrics as _metrics
        if input_tokens:
            _metrics.SAMUS_LLM_TOKENS_TOTAL.labels(
                workcell=workcell, kind="input",
            ).inc(input_tokens)
        if output_tokens:
            _metrics.SAMUS_LLM_TOKENS_TOTAL.labels(
                workcell=workcell, kind="output",
            ).inc(output_tokens)
        _metrics.SAMUS_LLM_CALLS_TOTAL.labels(
            workcell=workcell, outcome=outcome,
        ).inc()
    except Exception as exc:  # noqa: BLE001
        _LOG.debug("llm_budget call-counter publish skipped: %s", exc)


def _publish_circuit_trip(workcell: str) -> None:
    """Increment the circuit-breaker trip counter (Control C)."""
    try:
        from . import metrics as _metrics
        _metrics.SAMUS_LLM_CIRCUIT_TRIPS_TOTAL.labels(workcell=workcell).inc()
    except Exception as exc:  # noqa: BLE001
        _LOG.debug("llm_budget circuit-trip publish skipped: %s", exc)


def _publish_metrics(workcell: str, b: "WorkcellBudget", store: "LlmBudgetStore") -> None:
    """Update Prometheus gauges from the post-record state.

    Best-effort: a metrics publish failure must not poison the budget path.
    """
    try:
        from . import metrics as _metrics
        quota = compute_quota(
            store.base_token_budget,
            efficiency_ema=b.efficiency_ema,
            efficiency_call_count=b.efficiency_call_count,
            floor_pct=store.floor_pct,
        )
        _metrics.SAMUS_LLM_BUDGET_QUOTA.labels(workcell=workcell).set(quota)
        _metrics.SAMUS_LLM_BUDGET_USED.labels(workcell=workcell).set(b.total_tokens_today)
        _metrics.SAMUS_LLM_EFFICIENCY.labels(workcell=workcell).set(b.efficiency_ema)
    except Exception as exc:  # noqa: BLE001
        _LOG.debug("llm_budget gauge publish skipped: %s", exc)


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class WorkcellBudget:
    """In-memory shape of one workcell's row. Mirrors the DDB schema."""

    workcell: str
    bucket_day: str = ""                  # YYYY-MM-DD; reset trigger
    input_tokens_today: int = 0
    output_tokens_today: int = 0
    total_tokens_today: int = 0           # = input + output (denormalized)
    call_count_today: int = 0
    success_count_today: int = 0
    failure_count_today: int = 0
    error_count_today: int = 0            # LLM errors (network, 5xx, parse)
    efficiency_ema: float = 1.0           # 0.0 - 1.0; init to 1.0 (benefit of doubt)
    efficiency_call_count: int = 0        # non-error calls (for stat-significance gate)
    # Control C: circuit-breaker fields (token-cost-hardening 2026-05-18).
    # Default to zero / empty so older DDB rows load without migration.
    consecutive_errors: int = 0
    circuit_open_until: str = ""          # ISO8601 UTC; empty when closed
    # HOTL Tranche 1 enforcement fields (default 0/"" — no migration needed):
    # a TTL-bounded operator/controller quota override, and — on the
    # ``_GLOBAL_CONTROLS`` sentinel row only — the freeze-nonessential window.
    quota_override: int = 0               # 0 = no override in effect
    quota_override_expires_at: str = ""   # ISO8601 UTC; empty = no override
    freeze_nonessential_until: str = ""   # ISO8601 UTC; sentinel row only
    last_updated: str = ""

    def to_item(self) -> dict[str, Any]:
        """DDB serialization. Floats -> Decimal (DDB rejects float)."""
        item = asdict(self)
        item["efficiency_ema"] = Decimal(str(self.efficiency_ema))
        return item

    @classmethod
    def from_item(cls, item: dict[str, Any]) -> "WorkcellBudget":
        """DDB deserialization. Decimal -> int/float. Missing Control C fields
        default to 0/"" to keep pre-hardening rows loadable.
        """
        kwargs: dict[str, Any] = {"workcell": str(item["workcell"])}
        for f in (
            "bucket_day", "last_updated", "circuit_open_until",
            "quota_override_expires_at", "freeze_nonessential_until",
        ):
            kwargs[f] = str(item.get(f, "") or "")
        for f in (
            "input_tokens_today", "output_tokens_today", "total_tokens_today",
            "call_count_today", "success_count_today", "failure_count_today",
            "error_count_today", "efficiency_call_count",
            "consecutive_errors", "quota_override",
        ):
            kwargs[f] = int(item.get(f, 0) or 0)
        ema = item.get("efficiency_ema", 1.0)
        kwargs["efficiency_ema"] = float(ema) if ema is not None else 1.0
        return cls(**kwargs)


@dataclass
class QuotaDecision:
    """Result of ``can_spend``. ``allowed=False`` means caller should fall back."""

    allowed: bool
    quota: int
    used: int
    requested: int
    reason: str | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _today_utc(now_func: Callable[[], float] | None = None) -> str:
    t = (now_func or time.time)()
    return time.strftime("%Y-%m-%d", time.gmtime(t))


def _iso_now(now_func: Callable[[], float] | None = None) -> str:
    t = (now_func or time.time)()
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def _iso_at(epoch_seconds: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch_seconds))


def _iso_to_epoch(value: str) -> float:
    """Parse a ``%Y-%m-%dT%H:%M:%SZ`` UTC stamp to epoch seconds; 0.0 on junk."""
    try:
        return float(calendar.timegm(time.strptime(value, "%Y-%m-%dT%H:%M:%SZ")))
    except (ValueError, TypeError):
        return 0.0


def _reset_if_new_day(b: WorkcellBudget, now_func: Callable[[], float] | None = None) -> WorkcellBudget:
    """Zero today's counters when bucket_day rolls over. EMA + call_count preserved.

    Circuit-breaker state intentionally survives the rollover — a workcell
    that tripped at 23:59 UTC should still be in cooldown at 00:01 UTC,
    not magically reset by the date change.
    """
    today = _today_utc(now_func)
    if b.bucket_day != today:
        b.bucket_day = today
        b.input_tokens_today = 0
        b.output_tokens_today = 0
        b.total_tokens_today = 0
        b.call_count_today = 0
        b.success_count_today = 0
        b.failure_count_today = 0
        b.error_count_today = 0
    return b


def compute_quota(
    base_quota: int,
    *,
    efficiency_ema: float,
    efficiency_call_count: int,
    min_calls_for_adaptive: int = 10,
    floor_pct: float = 0.10,
) -> int:
    """Pure quota math. Exposed for tests; no I/O.

    Until a workcell has at least ``min_calls_for_adaptive`` non-error calls,
    quota stays at the full ``base_quota`` (don't punish brand-new workcells
    with insufficient signal). After that, ``factor = 0.5 + 1.5 * ema`` so
    100% efficient earns 2x, 0% earns 0.5x. Floor at ``base * floor_pct``
    ensures a workcell stuck in failure loops can still try a few calls.
    """
    if efficiency_call_count < min_calls_for_adaptive:
        return int(base_quota)
    ema = max(0.0, min(1.0, float(efficiency_ema)))
    factor = 0.5 + 1.5 * ema
    floor = int(base_quota * floor_pct)
    return max(floor, int(base_quota * factor))


# ---------------------------------------------------------------------------
# Storage backends
# ---------------------------------------------------------------------------

class _JsonBackend:
    """Local JSON-file backend. Process-local lock; safe across threads.

    Multi-process safety relies on the file being written atomically — we
    serialize the entire state on every save and rename into place. Not
    safe across multiple containers writing the same file at the same time;
    for that, use the DDB backend. This is the dev/test fallback.
    """

    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.Lock()

    def load(self, workcell: str) -> WorkcellBudget | None:
        with self._lock:
            if not os.path.exists(self.path):
                return None
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except (OSError, ValueError) as exc:
                _LOG.warning("llm_budget json load failed: %s", exc)
                return None
            entry = (data or {}).get(workcell)
            if not entry:
                return None
            entry["workcell"] = workcell
            # Use field defaults for any missing keys (e.g. older JSON files
            # written before Control C added consecutive_errors / circuit_open_until).
            template = WorkcellBudget(workcell=workcell).__dict__
            return WorkcellBudget(**{k: entry.get(k, v) for k, v in template.items()})

    def save(self, b: WorkcellBudget) -> bool:
        with self._lock:
            data: dict[str, Any] = {}
            if os.path.exists(self.path):
                try:
                    with open(self.path, "r", encoding="utf-8") as f:
                        data = json.load(f) or {}
                except (OSError, ValueError):
                    data = {}
            row = asdict(b)
            row.pop("workcell", None)
            data[b.workcell] = row
            try:
                os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
                tmp = f"{self.path}.tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2, default=str)
                os.replace(tmp, self.path)
                return True
            except OSError as exc:
                _LOG.warning("llm_budget json save failed: %s", exc)
                return False


class _DdbBackend:
    """DynamoDB backend. One row per workcell, atomic ADD for counter increments.

    DDB schema (PK=workcell, no SK). Daily-reset is handled in load() by
    checking bucket_day and rewriting if stale — keeps the read path simple
    and lets DDB's UPDATE...ADD do atomic increments without race windows.
    """

    def __init__(self, table_name: str, region: str) -> None:
        self.table_name = table_name
        self.region = region

    def _table(self) -> Any:
        from . import aws  # local import so the JSON backend works without boto3
        return aws.table(self.table_name, self.region)

    def load(self, workcell: str) -> WorkcellBudget | None:
        try:
            resp = self._table().get_item(Key={"workcell": workcell})
        except Exception as exc:  # noqa: BLE001 - degraded path
            _LOG.warning("llm_budget ddb load failed: %s", exc)
            return None
        item = resp.get("Item")
        if not item:
            return None
        try:
            return WorkcellBudget.from_item(item)
        except Exception as exc:  # noqa: BLE001 - malformed row -> treat as missing
            _LOG.warning("llm_budget ddb row malformed: %s", exc)
            return None

    def save(self, b: WorkcellBudget) -> bool:
        try:
            self._table().put_item(Item=b.to_item())
            return True
        except Exception as exc:  # noqa: BLE001 - degraded path
            _LOG.warning("llm_budget ddb save failed: %s", exc)
            return False


# ---------------------------------------------------------------------------
# Store (public API)
# ---------------------------------------------------------------------------

class LlmBudgetStore:
    """Public facade. Selects backend, applies daily reset, runs quota math.

    Construction does NO I/O — backends are lazily probed on first call.
    Designed so it can be instantiated at import time without blowing up
    in environments that don't have AWS reachable.

    ``now_func`` injection: tests pass a callable returning a float
    (epoch seconds) so circuit-breaker cooldown windows are deterministic
    without freezegun. Production code leaves it ``None`` and falls back
    to ``time.time``.
    """

    def __init__(
        self,
        *,
        base_token_budget: int,
        ema_alpha: float,
        floor_pct: float,
        ddb_table: str | None,
        aws_region: str = "us-west-1",
        json_path: str | None = None,
        circuit_breaker_threshold: int = 10,
        circuit_breaker_cooldown_sec: int = 300,
        now_func: Callable[[], float] | None = None,
    ) -> None:
        if not (0.0 < ema_alpha <= 1.0):
            raise ValueError("ema_alpha must be in (0, 1]")
        if not (0.0 <= floor_pct <= 1.0):
            raise ValueError("floor_pct must be in [0, 1]")
        if circuit_breaker_threshold < 1:
            raise ValueError("circuit_breaker_threshold must be >= 1")
        if circuit_breaker_cooldown_sec < 0:
            raise ValueError("circuit_breaker_cooldown_sec must be >= 0")
        self.base_token_budget = int(base_token_budget)
        self.ema_alpha = float(ema_alpha)
        self.floor_pct = float(floor_pct)
        self.circuit_breaker_threshold = int(circuit_breaker_threshold)
        self.circuit_breaker_cooldown_sec = int(circuit_breaker_cooldown_sec)
        self._now: Callable[[], float] = now_func or time.time
        self._ddb = _DdbBackend(ddb_table, aws_region) if ddb_table else None
        self._json = _JsonBackend(json_path or _DEFAULT_JSON_PATH)
        self._cache: dict[str, tuple[float, WorkcellBudget]] = {}
        self._cache_ttl = 60.0  # seconds; budgets don't need to be perfectly fresh
        self._lock = threading.Lock()

    # -- backend selection -------------------------------------------------

    def _load(self, workcell: str) -> WorkcellBudget:
        """Read-through cache. Tries DDB first, then JSON, then fresh."""
        now = self._now()
        with self._lock:
            entry = self._cache.get(workcell)
            if entry and (now - entry[0]) < self._cache_ttl:
                return entry[1]

        b: WorkcellBudget | None = None
        if self._ddb is not None:
            b = self._ddb.load(workcell)
        if b is None:
            b = self._json.load(workcell)
        if b is None:
            b = WorkcellBudget(workcell=workcell, bucket_day=_today_utc(self._now))

        b = _reset_if_new_day(b, self._now)
        with self._lock:
            self._cache[workcell] = (now, b)
        return b

    def _save(self, b: WorkcellBudget) -> None:
        """Write-through to DDB if configured; always mirror to JSON."""
        b.last_updated = _iso_now(self._now)
        wrote_ddb = False
        if self._ddb is not None:
            wrote_ddb = self._ddb.save(b)
        if not wrote_ddb:
            self._json.save(b)
        with self._lock:
            self._cache[b.workcell] = (self._now(), b)

    # -- public API --------------------------------------------------------

    def can_spend(self, workcell: str, est_tokens: int) -> QuotaDecision:
        """Pre-flight check. Returns ``allowed=True`` on persistence failure.

        Two denial paths:
          1. circuit breaker open (Control C) — denied with
             ``reason="circuit_open_until_<iso>"``.
          2. token quota exhausted — denied with ``reason="budget_exceeded:..."``.
        """
        try:
            b = self._load(workcell)
        except Exception as exc:  # noqa: BLE001 - never block on store failure
            _LOG.warning("llm_budget load failed (%s); allowing call", exc)
            return QuotaDecision(
                allowed=True, quota=self.base_token_budget,
                used=0, requested=int(est_tokens),
                reason=f"store_unavailable: {exc}",
            )

        # Control C: circuit-breaker gate runs BEFORE quota math.
        # If the cooldown is in the future, deny with the iso timestamp
        # so the operator/log line shows when it lifts.
        if b.circuit_open_until:
            try:
                # calendar.timegm is the inverse of time.gmtime — interprets
                # the struct_time as UTC, no local-time / DST contamination.
                open_until_epoch = calendar.timegm(
                    time.strptime(b.circuit_open_until, "%Y-%m-%dT%H:%M:%SZ")
                )
            except (ValueError, TypeError):
                open_until_epoch = 0.0
            if self._now() < open_until_epoch:
                return QuotaDecision(
                    allowed=False,
                    quota=self.base_token_budget,
                    used=int(b.total_tokens_today),
                    requested=max(0, int(est_tokens)),
                    reason=f"circuit_open_until_{b.circuit_open_until}",
                )
            # Cooldown lapsed -> clear the field opportunistically. We do
            # NOT reset consecutive_errors here; one success will do that.
            b.circuit_open_until = ""

        # HOTL enforcement: entropy freeze-nonessential window. Essential
        # workcells keep working; everything else is denied until it lifts.
        frozen_until = self.nonessential_frozen_until()
        if frozen_until and workcell not in ESSENTIAL_WORKCELLS:
            return QuotaDecision(
                allowed=False,
                quota=self.base_token_budget,
                used=int(b.total_tokens_today),
                requested=max(0, int(est_tokens)),
                reason=f"nonessential_frozen_until_{frozen_until}",
            )

        quota = self._effective_quota(b)
        used = int(b.total_tokens_today)
        requested = max(0, int(est_tokens))
        if used + requested > quota:
            return QuotaDecision(
                allowed=False, quota=quota, used=used, requested=requested,
                reason=f"budget_exceeded: used={used} + req={requested} > quota={quota}",
            )
        return QuotaDecision(
            allowed=True, quota=quota, used=used, requested=requested,
        )

    def record_spend(
        self,
        workcell: str,
        *,
        input_tokens: int,
        output_tokens: int,
        outcome: str,
    ) -> WorkcellBudget:
        """Post-flight bookkeeping. ``outcome`` in {success, failure, error}.

        - ``success`` -> efficiency EMA pulled toward 1.0, success_count++.
                          Resets consecutive_errors + clears circuit_open_until.
        - ``failure`` -> EMA pulled toward 0.0, failure_count++.
                          Resets consecutive_errors + clears circuit_open_until.
                          (A "failure" is a task-level miss — the model
                          answered, the answer was wrong — not an infra
                          fault, so the breaker doesn't care.)
        - ``error``   -> error_count++ AND consecutive_errors++.
                          EMA + efficiency_call_count untouched (LLM-call
                          errors don't punish future quota — they trip the
                          circuit instead, Control C).

        Always returns the post-update budget so callers can log it.
        """
        try:
            b = self._load(workcell)
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("llm_budget load failed during record (%s)", exc)
            return WorkcellBudget(workcell=workcell)

        inp = max(0, int(input_tokens))
        out = max(0, int(output_tokens))
        b.input_tokens_today += inp
        b.output_tokens_today += out
        b.total_tokens_today = b.input_tokens_today + b.output_tokens_today
        b.call_count_today += 1
        # Cumulative Prometheus counters — emit deltas from this single call.
        _publish_call_counters(workcell, inp, out, outcome)

        tripped = False
        if outcome == "success":
            b.success_count_today += 1
            b.efficiency_call_count += 1
            b.efficiency_ema = (1 - self.ema_alpha) * b.efficiency_ema + self.ema_alpha * 1.0
            # Control C: a single success closes the breaker.
            b.consecutive_errors = 0
            b.circuit_open_until = ""
        elif outcome == "failure":
            b.failure_count_today += 1
            b.efficiency_call_count += 1
            b.efficiency_ema = (1 - self.ema_alpha) * b.efficiency_ema + self.ema_alpha * 0.0
            # Task-level failure isn't an infra fault; reset the breaker.
            b.consecutive_errors = 0
            b.circuit_open_until = ""
        elif outcome == "error":
            b.error_count_today += 1
            # Control C: increment + trip if at threshold.
            b.consecutive_errors += 1
            if b.consecutive_errors >= self.circuit_breaker_threshold:
                open_until = self._now() + float(self.circuit_breaker_cooldown_sec)
                b.circuit_open_until = _iso_at(open_until)
                tripped = True
        else:
            raise ValueError(
                f"outcome must be one of {{success, failure, error}}, got {outcome!r}"
            )

        self._save(b)
        _publish_metrics(workcell, b, self)
        if tripped:
            _publish_circuit_trip(workcell)
            _LOG.warning(
                "llm circuit breaker tripped for workcell=%s "
                "(consecutive_errors=%s, cooldown_until=%s)",
                workcell, b.consecutive_errors, b.circuit_open_until,
            )
        return b

    # -- HOTL enforcement hooks (portfolio_controller / entropy) ------------

    def _effective_quota(self, b: WorkcellBudget) -> int:
        """Adaptive quota, superseded by an unexpired TTL override (clamped)."""
        if b.quota_override and b.quota_override_expires_at:
            if self._now() < _iso_to_epoch(b.quota_override_expires_at):
                return self._clamp_override(int(b.quota_override))
            # Expired — fall through to the adaptive quota (the stale fields
            # are cleared lazily on the next set/save; reads stay pure).
        return compute_quota(
            self.base_token_budget,
            efficiency_ema=b.efficiency_ema,
            efficiency_call_count=b.efficiency_call_count,
            floor_pct=self.floor_pct,
        )

    def _clamp_override(self, quota: int) -> int:
        lo = int(self.base_token_budget * QUOTA_OVERRIDE_MIN_FACTOR)
        hi = int(self.base_token_budget * QUOTA_OVERRIDE_MAX_FACTOR)
        return max(lo, min(hi, int(quota)))

    def set_quota_override(
        self, workcell: str, quota: int, ttl_seconds: float,
    ) -> WorkcellBudget:
        """Apply a TTL-bounded daily-quota override for ``workcell``.

        The override is clamped to [0.25x, 2.0x] of the base quota and the
        TTL to at most 24h, then persisted through the normal backend chain
        (DDB when configured, JSON fallback otherwise). Never raises — on a
        persistence hiccup the in-memory cache still carries the override.
        """
        ttl = max(1.0, min(float(ttl_seconds), float(QUOTA_OVERRIDE_MAX_TTL_SEC)))
        b = self._load(workcell)
        b.quota_override = self._clamp_override(int(quota))
        b.quota_override_expires_at = _iso_at(self._now() + ttl)
        self._save(b)
        return b

    def clear_quota_override(self, workcell: str) -> WorkcellBudget:
        """Remove any quota override for ``workcell`` (idempotent)."""
        b = self._load(workcell)
        b.quota_override = 0
        b.quota_override_expires_at = ""
        self._save(b)
        return b

    def freeze_nonessential(self, ttl_seconds: float) -> str:
        """Freeze nonessential LLM paths stack-wide for ``ttl_seconds``.

        Persists the window on the ``_GLOBAL_CONTROLS`` sentinel row. Returns
        the ISO expiry so callers can log/emit it. TTL clamped to 24h.
        """
        ttl = max(1.0, min(float(ttl_seconds), float(QUOTA_OVERRIDE_MAX_TTL_SEC)))
        row = self._load(_GLOBAL_CONTROLS)
        row.freeze_nonessential_until = _iso_at(self._now() + ttl)
        self._save(row)
        return row.freeze_nonessential_until

    def nonessential_frozen_until(self) -> str | None:
        """ISO expiry of an ACTIVE freeze window, else ``None``. Never raises."""
        try:
            row = self._load(_GLOBAL_CONTROLS)
        except Exception as exc:  # noqa: BLE001 — never block on store failure
            _LOG.warning("llm_budget controls load failed (%s); not frozen", exc)
            return None
        until = row.freeze_nonessential_until
        if until and self._now() < _iso_to_epoch(until):
            return until
        return None

    # -- introspection -----------------------------------------------------

    def snapshot(self, workcell: str) -> WorkcellBudget:
        """Read-only view for operator surfaces. Triggers daily-reset if stale."""
        return self._load(workcell)


# ---------------------------------------------------------------------------
# Process-global accessor
# ---------------------------------------------------------------------------

_STORE: LlmBudgetStore | None = None
_STORE_LOCK = threading.Lock()


def get_store() -> LlmBudgetStore:
    """Lazily build the process-wide LlmBudgetStore from settings."""
    global _STORE
    with _STORE_LOCK:
        if _STORE is not None:
            return _STORE
        from .config import get_settings
        s = get_settings()
        _STORE = LlmBudgetStore(
            base_token_budget=s.llm_workcell_base_token_budget,
            ema_alpha=s.llm_budget_ema_alpha,
            floor_pct=s.llm_budget_floor_pct,
            ddb_table=s.ddb_llm_budgets_table or None,
            aws_region=s.aws_region,
            json_path=os.getenv("SAMUS_LLM_BUDGET_PATH") or None,
            circuit_breaker_threshold=s.llm_circuit_breaker_threshold,
            circuit_breaker_cooldown_sec=s.llm_circuit_breaker_cooldown_sec,
        )
        return _STORE


def reset_store() -> None:
    """Drop the cached singleton (tests + secret-rotation)."""
    global _STORE
    with _STORE_LOCK:
        _STORE = None


def set_quota_override(workcell: str, quota: int, ttl_seconds: float) -> "WorkcellBudget":
    """Module-level convenience: TTL quota override on the process store."""
    return get_store().set_quota_override(workcell, quota, ttl_seconds)


def freeze_nonessential(ttl_seconds: float) -> str:
    """Module-level convenience: stack-wide nonessential-LLM freeze."""
    return get_store().freeze_nonessential(ttl_seconds)


__all__ = [
    "WorkcellBudget",
    "QuotaDecision",
    "LlmBudgetStore",
    "compute_quota",
    "get_store",
    "reset_store",
    "set_quota_override",
    "freeze_nonessential",
    "ESSENTIAL_WORKCELLS",
    "QUOTA_OVERRIDE_MIN_FACTOR",
    "QUOTA_OVERRIDE_MAX_FACTOR",
]
