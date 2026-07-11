"""Global daily $-cap across ALL workcells.

Control A from the token-cost-hardening 2026-05-18 brief. Defense in
depth on top of :mod:`backend.common.llm_budget`: where that module
caps each workcell at a per-workcell token budget, this module caps
the *aggregate dollar spend* across every workcell at a single number.

Dynamic scaling (2026-06-24)
----------------------------
When ``SAMUS_LLM_DYNAMIC_CAP=true`` (default on OpenAI backend), the
daily cap is **not a static constant** — it scales with Samus's own MRR
(monthly recurring revenue). API inference cost is real CODB; more earned
revenue unlocks more inference capacity. See
:mod:`backend.common.llm_budget_scaler` for the formula and knobs.

When dynamic scaling is off (LM Studio / no Stripe key), the static
``daily_dollar_cap`` from settings applies as before.

Storage
-------
Single DDB row in the same ``samus_llm_budgets`` table, PK = ``"GLOBAL"``.
Reuses the same daily-reset semantics: ``bucket_day`` is checked on
read, today's dollar counter is zeroed on rollover, prior days are
not retained (Prometheus owns the historical view).

Persistence philosophy
----------------------
**Allow on persistence failure**, identical to the per-workcell store.
The brief is explicit: a transient DDB outage must not block all LLM
calls. The risk is over-spend if the row vanishes mid-day; the
benefit is the system stays useful when AWS hiccups. Operators
should monitor ``SAMUS_LLM_DOLLAR_USED_TODAY{scope="global"}`` for
flat-line gaps as an indirect health check.

No new external deps — stdlib (`json`, `os`, `threading`, `time`)
plus the existing ``backend.common.aws`` factory for DDB.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Any, Callable

from .llm_pricing import (
    UnknownModelPricing,
    compute_cost_usd,
)


_LOG = logging.getLogger("samus.common.llm_global_budget")

_GLOBAL_PK = "GLOBAL"
_DEFAULT_JSON_PATH = "/opt/samus/data/llm_global_budget.json"


def _today_utc(now_func: Callable[[], float] | None = None) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime((now_func or time.time)()))


def _iso_now(now_func: Callable[[], float] | None = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime((now_func or time.time)()))


def _publish_dollar_gauge(scope: str, used: float, cap: float) -> None:
    """Best-effort gauge publish — never blocks the budget path."""
    try:
        from . import metrics as _metrics
        _metrics.SAMUS_LLM_DOLLAR_USED_TODAY.labels(scope=scope).set(used)
        _metrics.SAMUS_LLM_DOLLAR_CAP.labels(scope=scope).set(cap)
    except Exception as exc:  # noqa: BLE001
        _LOG.debug("llm_global_budget gauge publish skipped: %s", exc)


@dataclass
class GlobalBudget:
    """In-memory shape of the GLOBAL row.

    Stored on the same DDB table as the per-workcell rows; PK = "GLOBAL"
    is reserved (no workcell may be named "GLOBAL"). Dollar fields are
    plain floats here but serialize to Decimal for DDB compatibility.
    """

    bucket_day: str = ""
    dollars_today: float = 0.0
    call_count_today: int = 0
    last_updated: str = ""

    def to_item(self) -> dict[str, Any]:
        item = asdict(self)
        item["workcell"] = _GLOBAL_PK
        item["dollars_today"] = Decimal(str(self.dollars_today))
        return item

    @classmethod
    def from_item(cls, item: dict[str, Any]) -> "GlobalBudget":
        d = item.get("dollars_today", 0.0)
        try:
            dollars = float(d) if d is not None else 0.0
        except (TypeError, ValueError):
            dollars = 0.0
        return cls(
            bucket_day=str(item.get("bucket_day", "") or ""),
            dollars_today=dollars,
            call_count_today=int(item.get("call_count_today", 0) or 0),
            last_updated=str(item.get("last_updated", "") or ""),
        )


@dataclass
class GlobalDecision:
    """Result of ``can_spend_global``.

    ``allowed=False`` means the global cap would be breached; the caller
    must NOT make the HTTP request. ``allowed=True`` with non-empty
    ``reason`` is the "store unavailable" path (we allow but log why).
    """

    allowed: bool
    cap_usd: float
    used_usd: float
    estimated_usd: float
    reason: str | None = None


class _JsonBackend:
    """Local JSON fallback (dev/test). Process-local lock."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.Lock()

    def load(self) -> GlobalBudget | None:
        with self._lock:
            if not os.path.exists(self.path):
                return None
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    data = json.load(f) or {}
            except (OSError, ValueError) as exc:
                _LOG.warning("llm_global_budget json load failed: %s", exc)
                return None
            entry = data.get(_GLOBAL_PK)
            if not entry:
                return None
            return GlobalBudget(
                bucket_day=str(entry.get("bucket_day", "") or ""),
                dollars_today=float(entry.get("dollars_today", 0.0) or 0.0),
                call_count_today=int(entry.get("call_count_today", 0) or 0),
                last_updated=str(entry.get("last_updated", "") or ""),
            )

    def save(self, b: GlobalBudget) -> bool:
        with self._lock:
            data: dict[str, Any] = {}
            if os.path.exists(self.path):
                try:
                    with open(self.path, "r", encoding="utf-8") as f:
                        data = json.load(f) or {}
                except (OSError, ValueError):
                    data = {}
            row = asdict(b)
            data[_GLOBAL_PK] = row
            try:
                os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
                tmp = f"{self.path}.tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2, default=str)
                os.replace(tmp, self.path)
                return True
            except OSError as exc:
                _LOG.warning("llm_global_budget json save failed: %s", exc)
                return False


class _DdbBackend:
    """DDB backend for the GLOBAL row. Same table as per-workcell rows."""

    def __init__(self, table_name: str, region: str) -> None:
        self.table_name = table_name
        self.region = region

    def _table(self) -> Any:
        from . import aws
        return aws.table(self.table_name, self.region)

    def load(self) -> GlobalBudget | None:
        try:
            resp = self._table().get_item(Key={"workcell": _GLOBAL_PK})
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("llm_global_budget ddb load failed: %s", exc)
            return None
        item = resp.get("Item")
        if not item:
            return None
        try:
            return GlobalBudget.from_item(item)
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("llm_global_budget ddb row malformed: %s", exc)
            return None

    def save(self, b: GlobalBudget) -> bool:
        try:
            self._table().put_item(Item=b.to_item())
            return True
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("llm_global_budget ddb save failed: %s", exc)
            return False


class LlmGlobalBudgetStore:
    """Process-wide global $-cap store. Mirrors LlmBudgetStore patterns.

    Designed to be cheap to construct (no I/O until first call). All
    HTTP-blocking calls go through ``can_spend_global`` first, so this
    is on the absolute hot path — keep it allocation-light.
    """

    def __init__(
        self,
        *,
        daily_dollar_cap: float,
        ddb_table: str | None,
        aws_region: str = "us-west-1",
        json_path: str | None = None,
        now_func: Callable[[], float] | None = None,
        dynamic_cap: bool = False,
    ) -> None:
        if daily_dollar_cap < 0:
            raise ValueError("daily_dollar_cap must be >= 0")
        self._static_cap = float(daily_dollar_cap)
        self._dynamic_cap = dynamic_cap
        self._ddb = _DdbBackend(ddb_table, aws_region) if ddb_table else None
        self._json = _JsonBackend(json_path or _DEFAULT_JSON_PATH)
        self._now: Callable[[], float] = now_func or time.time
        self._cache: tuple[float, GlobalBudget] | None = None
        self._cache_ttl = 30.0  # tighter than per-workcell — global needs faster freshness
        self._lock = threading.Lock()

    @property
    def daily_dollar_cap(self) -> float:
        """Resolve the active daily cap.

        When dynamic scaling is on, the cap tracks MRR via the budget
        scaler. When off (LM Studio / tests), the static cap from
        settings applies.
        """
        if not self._dynamic_cap:
            return self._static_cap
        try:
            from . import llm_budget_scaler
            return llm_budget_scaler.compute_daily_cap()
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("dynamic cap failed, using static $%.2f: %s", self._static_cap, exc)
            return self._static_cap

    # -- internal --------------------------------------------------------

    def _load(self) -> GlobalBudget:
        now = self._now()
        with self._lock:
            if self._cache is not None and (now - self._cache[0]) < self._cache_ttl:
                return self._cache[1]

        b: GlobalBudget | None = None
        if self._ddb is not None:
            b = self._ddb.load()
        if b is None:
            b = self._json.load()
        if b is None:
            b = GlobalBudget(bucket_day=_today_utc(self._now))

        # Daily reset.
        today = _today_utc(self._now)
        if b.bucket_day != today:
            b.bucket_day = today
            b.dollars_today = 0.0
            b.call_count_today = 0

        with self._lock:
            self._cache = (now, b)
        return b

    def _save(self, b: GlobalBudget) -> None:
        b.last_updated = _iso_now(self._now)
        wrote_ddb = False
        if self._ddb is not None:
            wrote_ddb = self._ddb.save(b)
        if not wrote_ddb:
            self._json.save(b)
        with self._lock:
            self._cache = (self._now(), b)

    # -- public API ------------------------------------------------------

    def can_spend_global(
        self,
        model: str,
        est_input_tokens: int,
        est_output_tokens: int,
    ) -> GlobalDecision:
        """Estimate this call's $ via the pricing table; deny if cap would be breached.

        **Allow-on-persistence-failure**: see module docstring. If the
        underlying store throws (DDB outage, JSON path unwritable) we
        return ``allowed=True`` with ``reason="store_unavailable:..."``
        so the caller can log it but still serve the request.
        """
        # Estimate $. UnknownModelPricing -> allow but log; we don't want
        # a typo in the model id to take down all LLM calls.
        try:
            estimated_usd = compute_cost_usd(
                model,
                input_tokens=int(est_input_tokens),
                output_tokens=int(est_output_tokens),
            )
        except UnknownModelPricing as exc:
            _LOG.warning("llm_global_budget unknown model pricing (%s); allowing call", exc)
            return GlobalDecision(
                allowed=True,
                cap_usd=self.daily_dollar_cap,
                used_usd=0.0,
                estimated_usd=0.0,
                reason=f"unknown_model_pricing: {exc}",
            )

        try:
            b = self._load()
        except Exception as exc:  # noqa: BLE001 - allow on persistence failure
            _LOG.warning("llm_global_budget load failed (%s); allowing call", exc)
            return GlobalDecision(
                allowed=True,
                cap_usd=self.daily_dollar_cap,
                used_usd=0.0,
                estimated_usd=estimated_usd,
                reason=f"store_unavailable: {exc}",
            )

        if (b.dollars_today + estimated_usd) > self.daily_dollar_cap:
            return GlobalDecision(
                allowed=False,
                cap_usd=self.daily_dollar_cap,
                used_usd=b.dollars_today,
                estimated_usd=estimated_usd,
                reason=(
                    f"global_cap_exceeded: used=${b.dollars_today:.4f} + "
                    f"est=${estimated_usd:.4f} > cap=${self.daily_dollar_cap:.2f}"
                ),
            )
        return GlobalDecision(
            allowed=True,
            cap_usd=self.daily_dollar_cap,
            used_usd=b.dollars_today,
            estimated_usd=estimated_usd,
        )

    def record_spend_global(
        self,
        model: str,
        *,
        actual_input: int,
        actual_output: int,
        cache_write_tokens: int = 0,
        cache_read_tokens: int = 0,
    ) -> GlobalBudget:
        """Convert actuals to $ and add to today's running total.

        Cache token fields default to 0 so non-cached callers don't need
        to know about Control D. When cache is in use the savings show
        up automatically (cache_read_per_mtok is ~10% of input).
        """
        try:
            cost = compute_cost_usd(
                model,
                input_tokens=int(actual_input),
                output_tokens=int(actual_output),
                cache_write_tokens=int(cache_write_tokens),
                cache_read_tokens=int(cache_read_tokens),
            )
        except UnknownModelPricing as exc:
            _LOG.warning(
                "llm_global_budget cannot record unknown model %s (%s); skipping",
                model, exc,
            )
            return GlobalBudget(bucket_day=_today_utc(self._now))

        try:
            b = self._load()
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("llm_global_budget load failed during record (%s)", exc)
            return GlobalBudget(bucket_day=_today_utc(self._now))

        b.dollars_today = round(b.dollars_today + cost, 4)
        b.call_count_today += 1
        self._save(b)
        _publish_dollar_gauge("global", b.dollars_today, self.daily_dollar_cap)
        return b

    def snapshot(self) -> GlobalBudget:
        """Read-only view for operator surfaces / tests."""
        return self._load()


# ---------------------------------------------------------------------------
# Process-global accessor
# ---------------------------------------------------------------------------

_STORE: LlmGlobalBudgetStore | None = None
_STORE_LOCK = threading.Lock()


def get_global_store() -> LlmGlobalBudgetStore:
    """Lazily build the singleton from settings."""
    global _STORE
    with _STORE_LOCK:
        if _STORE is not None:
            return _STORE
        from .config import get_settings
        s = get_settings()
        dynamic = os.getenv("SAMUS_LLM_DYNAMIC_CAP", "").lower() in ("1", "true", "yes")
        if not dynamic:
            from .llm_client import _USING_OPENAI
            dynamic = _USING_OPENAI
        _STORE = LlmGlobalBudgetStore(
            daily_dollar_cap=s.llm_global_daily_dollar_cap,
            ddb_table=s.ddb_llm_budgets_table or None,
            aws_region=s.aws_region,
            json_path=os.getenv("SAMUS_LLM_GLOBAL_BUDGET_PATH") or None,
            dynamic_cap=dynamic,
        )
        if dynamic:
            from . import llm_budget_scaler as _scaler
            _LOG.info(
                "llm_global_budget: dynamic cap ENABLED (MRR-scaled, "
                "floor=$%.2f, ceiling=$%.2f, reinvest=%.0f%%)",
                _scaler.FLOOR_USD, _scaler.CEILING_USD,
                _scaler.REINVEST_PCT * 100,
            )
        return _STORE


def reset_global_store() -> None:
    """Drop the cached singleton (tests + rotation tooling)."""
    global _STORE
    with _STORE_LOCK:
        _STORE = None


__all__ = [
    "GlobalBudget",
    "GlobalDecision",
    "LlmGlobalBudgetStore",
    "get_global_store",
    "reset_global_store",
]
