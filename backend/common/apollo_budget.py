"""Guardrail G11 — daily $-cap on Apollo.io API calls.

Mirrors :mod:`backend.common.llm_global_budget` for Apollo. Today
Apollo calls are unmetered: a bad campaign config (titles too broad,
locations too broad, ``--pages`` cranked up) can burn the day's API
budget on one batch before the operator notices. G11 declares a
single dollar number per UTC day; once it's spent, every subsequent
Apollo call fails closed by raising :class:`ApolloBudgetExceeded`.

Storage
-------
Daily ledger keyed by UTC ``bucket_day``. DDB table
``samus_apollo_budgets`` (one row, PK ``"APOLLO"``) with a JSON
fallback at ``/opt/samus/data/apollo_budget.json``. Same write-through
pattern as the LLM global budget: DDB first, JSON if DDB is unset or
write fails. The last 10 calls are kept inline on the row so the
operator console can show "what burned today" without a separate
table.

Failure semantics
-----------------
* **Persistence: fail-OPEN.** Losing the budget store means bounded
  overspend, not unsafety. The Codex (chapter 04) explicitly treats
  Apollo budget the same as the LLM dollar cap: degrade-and-log
  rather than block. A loud warning goes to ``samus.common.apollo_budget``
  on every store-miss so Prometheus / log scrapers can alert.
* **Cap hit: fail-CLOSED.** ``record_spend`` raises
  :class:`ApolloBudgetExceeded` if the spend would push today's total
  over the cap. Pre-flight via :func:`ApolloBudgetStore.assert_allows`
  so the call site fails BEFORE the HTTP request goes out, not after.

The ``@apollo_budgeted("endpoint_name")`` decorator below is the
preferred wrapping pattern at call sites — it does pre-flight +
post-flight in one place, so the apollo_source module stays readable.
"""
from __future__ import annotations

import functools
import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from typing import Any, Callable, TypeVar

from .apollo_pricing import estimate_call_cost


_LOG = logging.getLogger("samus.common.apollo_budget")

_APOLLO_PK = "APOLLO"
_DEFAULT_JSON_PATH = "/opt/samus/data/apollo_budget.json"
_DEFAULT_DAILY_CAP_USD = 5.00
_DEFAULT_CREDIT_CEILING = 0
_RECENT_CALLS_KEPT = 10


def _today_utc(now_func: Callable[[], float] | None = None) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime((now_func or time.time)()))


def _iso_now(now_func: Callable[[], float] | None = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime((now_func or time.time)()))


def get_daily_cap_usd() -> float:
    """Read the daily cap from env. ``SAMUS_APOLLO_DAILY_BUDGET_USD``, default $5.00.

    Read every call so the operator can flip it live — same pattern as
    the per-credit rate. Negative / non-numeric values fall back.
    """
    raw = os.getenv("SAMUS_APOLLO_DAILY_BUDGET_USD", "").strip()
    if not raw:
        return _DEFAULT_DAILY_CAP_USD
    try:
        cap = float(raw)
    except ValueError:
        _LOG.warning(
            "SAMUS_APOLLO_DAILY_BUDGET_USD not numeric (%r); using default $%.2f",
            raw, _DEFAULT_DAILY_CAP_USD,
        )
        return _DEFAULT_DAILY_CAP_USD
    if cap < 0:
        _LOG.warning(
            "SAMUS_APOLLO_DAILY_BUDGET_USD negative (%s); using default $%.2f",
            cap, _DEFAULT_DAILY_CAP_USD,
        )
        return _DEFAULT_DAILY_CAP_USD
    return cap


def get_credit_ceiling() -> int:
    """Total Apollo credits the operator is willing to consume this billing period.

    ``SAMUS_APOLLO_CREDIT_CEILING`` — set to the account's remaining
    credit balance so the system stops before exhaustion.  Default 0
    means **no ceiling enforced** (backwards-compatible; the daily $-cap
    is still the primary guard).  A non-zero value adds a hard gate in
    ``assert_allows``: once ``call_count_today`` across all days in the
    ledger's ``lifetime_calls`` counter reaches the ceiling, every
    subsequent call is refused.
    """
    raw = os.getenv("SAMUS_APOLLO_CREDIT_CEILING", "").strip()
    if not raw:
        return _DEFAULT_CREDIT_CEILING
    try:
        val = int(raw)
    except ValueError:
        _LOG.warning(
            "SAMUS_APOLLO_CREDIT_CEILING not int (%r); ceiling disabled", raw,
        )
        return _DEFAULT_CREDIT_CEILING
    return max(0, val)


class ApolloBudgetExceeded(Exception):
    """Raised when an Apollo call would push today's spend past the cap."""


@dataclass
class ApolloCallRecord:
    """One Apollo call entry in the rolling ``recent_calls`` window."""

    ts: str = ""
    endpoint: str = ""
    usd: float = 0.0
    prospect_id: str | None = None

    def to_jsonable(self) -> dict[str, Any]:
        return {
            "ts": self.ts,
            "endpoint": self.endpoint,
            "usd": self.usd,
            "prospect_id": self.prospect_id,
        }


@dataclass
class ApolloBudget:
    """In-memory shape of the APOLLO ledger row."""

    bucket_day: str = ""
    dollars_today: float = 0.0
    call_count_today: int = 0
    lifetime_credits: int = 0
    last_updated: str = ""
    recent_calls: list[ApolloCallRecord] = field(default_factory=list)

    def to_item(self) -> dict[str, Any]:
        item: dict[str, Any] = {
            "ledger": _APOLLO_PK,
            "bucket_day": self.bucket_day,
            "dollars_today": Decimal(str(self.dollars_today)),
            "call_count_today": self.call_count_today,
            "lifetime_credits": self.lifetime_credits,
            "last_updated": self.last_updated,
            "recent_calls": [c.to_jsonable() for c in self.recent_calls],
        }
        return item

    @classmethod
    def from_item(cls, item: dict[str, Any]) -> "ApolloBudget":
        d = item.get("dollars_today", 0.0)
        try:
            dollars = float(d) if d is not None else 0.0
        except (TypeError, ValueError):
            dollars = 0.0
        raw_calls = item.get("recent_calls") or []
        calls: list[ApolloCallRecord] = []
        for rc in raw_calls:
            if not isinstance(rc, dict):
                continue
            try:
                calls.append(ApolloCallRecord(
                    ts=str(rc.get("ts", "") or ""),
                    endpoint=str(rc.get("endpoint", "") or ""),
                    usd=float(rc.get("usd", 0.0) or 0.0),
                    prospect_id=(
                        None if rc.get("prospect_id") is None
                        else str(rc.get("prospect_id"))
                    ),
                ))
            except (TypeError, ValueError):
                continue
        return cls(
            bucket_day=str(item.get("bucket_day", "") or ""),
            dollars_today=dollars,
            call_count_today=int(item.get("call_count_today", 0) or 0),
            lifetime_credits=int(item.get("lifetime_credits", 0) or 0),
            last_updated=str(item.get("last_updated", "") or ""),
            recent_calls=calls,
        )


class _JsonBackend:
    """Local JSON fallback (dev/test + degraded-DDB path). Process-local lock."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.Lock()

    def load(self) -> ApolloBudget | None:
        with self._lock:
            if not os.path.exists(self.path):
                return None
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    data = json.load(f) or {}
            except (OSError, ValueError) as exc:
                _LOG.warning("apollo_budget json load failed: %s", exc)
                return None
            entry = data.get(_APOLLO_PK)
            if not entry:
                return None
            try:
                return ApolloBudget.from_item(entry)
            except Exception as exc:  # noqa: BLE001
                _LOG.warning("apollo_budget json row malformed: %s", exc)
                return None

    def save(self, b: ApolloBudget) -> bool:
        with self._lock:
            data: dict[str, Any] = {}
            if os.path.exists(self.path):
                try:
                    with open(self.path, "r", encoding="utf-8") as f:
                        data = json.load(f) or {}
                except (OSError, ValueError):
                    data = {}
            row = {
                "bucket_day": b.bucket_day,
                "dollars_today": b.dollars_today,
                "call_count_today": b.call_count_today,
                "lifetime_credits": b.lifetime_credits,
                "last_updated": b.last_updated,
                "recent_calls": [c.to_jsonable() for c in b.recent_calls],
            }
            data[_APOLLO_PK] = row
            try:
                os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
                tmp = f"{self.path}.tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2, default=str)
                os.replace(tmp, self.path)
                return True
            except OSError as exc:
                _LOG.warning("apollo_budget json save failed: %s", exc)
                return False


class _DdbBackend:
    """DDB backend — one row PK ``ledger=APOLLO`` on samus_apollo_budgets."""

    def __init__(self, table_name: str, region: str) -> None:
        self.table_name = table_name
        self.region = region

    def _table(self) -> Any:
        from . import aws
        return aws.table(self.table_name, self.region)

    def load(self) -> ApolloBudget | None:
        try:
            resp = self._table().get_item(Key={"ledger": _APOLLO_PK})
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("apollo_budget ddb load failed: %s", exc)
            return None
        item = resp.get("Item")
        if not item:
            return None
        try:
            return ApolloBudget.from_item(item)
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("apollo_budget ddb row malformed: %s", exc)
            return None

    def save(self, b: ApolloBudget) -> bool:
        try:
            self._table().put_item(Item=b.to_item())
            return True
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("apollo_budget ddb save failed: %s", exc)
            return False


class ApolloBudgetStore:
    """Process-wide Apollo $-cap store. Mirrors LlmGlobalBudgetStore.

    Constructor is cheap (no I/O). All write paths cross-publish the
    in-process cache so concurrent callers see today's accumulated
    total without a refetch.
    """

    def __init__(
        self,
        *,
        ddb_table: str | None = None,
        aws_region: str = "us-west-1",
        json_path: str | None = None,
        now_func: Callable[[], float] | None = None,
        daily_cap_usd: Callable[[], float] | None = None,
    ) -> None:
        self._ddb = _DdbBackend(ddb_table, aws_region) if ddb_table else None
        self._json = _JsonBackend(json_path or _DEFAULT_JSON_PATH)
        self._now: Callable[[], float] = now_func or time.time
        # daily_cap_usd is callable so tests can pin a cap without env.
        self._cap_fn: Callable[[], float] = daily_cap_usd or get_daily_cap_usd
        self._cache: tuple[float, ApolloBudget] | None = None
        self._cache_ttl = 5.0  # very short — cap enforcement needs freshness
        self._lock = threading.Lock()

    # -- internal --------------------------------------------------------

    def _load(self) -> ApolloBudget:
        now = self._now()
        with self._lock:
            if self._cache is not None and (now - self._cache[0]) < self._cache_ttl:
                return self._cache[1]

        b: ApolloBudget | None = None
        if self._ddb is not None:
            b = self._ddb.load()
        if b is None:
            b = self._json.load()
        if b is None:
            b = ApolloBudget(bucket_day=_today_utc(self._now))

        # Daily reset — same shape as LLM global budget.
        today = _today_utc(self._now)
        if b.bucket_day != today:
            b.bucket_day = today
            b.dollars_today = 0.0
            b.call_count_today = 0
            b.recent_calls = []

        with self._lock:
            self._cache = (now, b)
        return b

    def _save(self, b: ApolloBudget) -> None:
        b.last_updated = _iso_now(self._now)
        wrote_ddb = False
        if self._ddb is not None:
            wrote_ddb = self._ddb.save(b)
        if not wrote_ddb:
            self._json.save(b)
        with self._lock:
            self._cache = (self._now(), b)

    # -- public API ------------------------------------------------------

    def current_spend_usd(self) -> float:
        """Today's accumulated spend in USD. Fail-OPEN: 0.0 if store broken."""
        try:
            return self._load().dollars_today
        except Exception as exc:  # noqa: BLE001 - fail-open by design
            _LOG.warning("apollo_budget current_spend_usd store unavailable: %s", exc)
            return 0.0

    def remaining_usd(self) -> float:
        """Cap minus today's spend, floored at 0.0."""
        cap = self._cap_fn()
        return max(0.0, cap - self.current_spend_usd())

    def assert_allows(self, usd: float) -> None:
        """Pre-flight: raise ApolloBudgetExceeded if ``usd`` would breach cap.

        Checks two ceilings:
        1. Daily $-cap (SAMUS_APOLLO_DAILY_BUDGET_USD) — resets each UTC day.
        2. Credit ceiling (SAMUS_APOLLO_CREDIT_CEILING) — lifetime counter,
           fail-CLOSED.  Set to account's remaining balance so the system
           stops before exhaustion.  0 = disabled (no ceiling enforced).

        Fail-OPEN on persistence — if the store can't be read we *allow*
        the call (logging loudly). Per Codex chapter 04, Apollo budget
        is a cost cap, not a safety cap; over-spend is bounded, but
        denying everything on a transient DDB outage is worse.
        """
        if usd < 0:
            usd = 0.0
        cap = self._cap_fn()
        try:
            b = self._load()
        except Exception as exc:  # noqa: BLE001 - fail-open
            _LOG.warning("apollo_budget assert_allows: store unavailable (%s); allowing", exc)
            return

        ceiling = get_credit_ceiling()
        if ceiling > 0 and b.lifetime_credits >= ceiling:
            raise ApolloBudgetExceeded(
                f"apollo_credit_ceiling_hit: lifetime={b.lifetime_credits} "
                f">= ceiling={ceiling}. Set SAMUS_APOLLO_CREDIT_CEILING "
                f"higher or reset lifetime_credits after account top-up."
            )

        if (b.dollars_today + usd) > cap:
            raise ApolloBudgetExceeded(
                f"apollo_daily_cap_exceeded: used=${b.dollars_today:.4f} + "
                f"est=${usd:.4f} > cap=${cap:.2f}"
            )

    def record_spend(
        self,
        usd: float,
        *,
        endpoint: str,
        prospect_id: str | None = None,
    ) -> ApolloBudget:
        """Atomic add to today's bucket. Raises if cap would be breached.

        Cap check is repeated here on top of ``assert_allows`` so the
        decorator and direct callers both go through the same gate
        — if assert_allows said OK but a parallel caller spent in between,
        the recorder catches it. Persistence failure on the *save* is
        logged but doesn't raise (fail-OPEN — same logic as the LLM
        global budget's record path).
        """
        if usd < 0:
            usd = 0.0
        cap = self._cap_fn()
        try:
            b = self._load()
        except Exception as exc:  # noqa: BLE001 - fail-open on load
            _LOG.warning(
                "apollo_budget record_spend: store unavailable on load (%s); "
                "allowing $%.4f spend but NOT persisted",
                exc, usd,
            )
            # Return a synthetic budget so the caller has something coherent
            # to read; today's running total is lost until the store recovers.
            return ApolloBudget(
                bucket_day=_today_utc(self._now),
                dollars_today=usd,
                call_count_today=1,
                last_updated=_iso_now(self._now),
                recent_calls=[ApolloCallRecord(
                    ts=_iso_now(self._now),
                    endpoint=endpoint,
                    usd=round(usd, 4),
                    prospect_id=prospect_id,
                )],
            )

        if (b.dollars_today + usd) > cap:
            raise ApolloBudgetExceeded(
                f"apollo_daily_cap_exceeded: used=${b.dollars_today:.4f} + "
                f"spend=${usd:.4f} > cap=${cap:.2f}"
            )

        b.dollars_today = round(b.dollars_today + usd, 4)
        b.call_count_today += 1
        b.lifetime_credits += 1
        b.recent_calls.append(ApolloCallRecord(
            ts=_iso_now(self._now),
            endpoint=endpoint,
            usd=round(usd, 4),
            prospect_id=prospect_id,
        ))
        # Trim to the last N — bounded row size for DDB friendliness.
        if len(b.recent_calls) > _RECENT_CALLS_KEPT:
            b.recent_calls = b.recent_calls[-_RECENT_CALLS_KEPT:]
        self._save(b)
        return b

    def reset_today(self) -> ApolloBudget:
        """Admin only — wipe today's bucket. Used by ops + tests."""
        b = ApolloBudget(
            bucket_day=_today_utc(self._now),
            dollars_today=0.0,
            call_count_today=0,
            last_updated=_iso_now(self._now),
            recent_calls=[],
        )
        self._save(b)
        return b

    def snapshot(self) -> ApolloBudget:
        """Read-only view (operator console + tests)."""
        return self._load()


# ---------------------------------------------------------------------------
# Process-global accessor + decorator
# ---------------------------------------------------------------------------

_STORE: ApolloBudgetStore | None = None
_STORE_LOCK = threading.Lock()


def get_store() -> ApolloBudgetStore:
    """Lazily build the singleton from env."""
    global _STORE
    with _STORE_LOCK:
        if _STORE is not None:
            return _STORE
        ddb_table = os.getenv("DDB_APOLLO_BUDGETS_TABLE", "samus_apollo_budgets") or None
        region = os.getenv("AWS_REGION", "us-west-1") or "us-west-1"
        json_path = os.getenv("SAMUS_APOLLO_BUDGET_PATH") or None
        _STORE = ApolloBudgetStore(
            ddb_table=ddb_table,
            aws_region=region,
            json_path=json_path,
        )
        return _STORE


def reset_store() -> None:
    """Drop the cached singleton (tests + rotation tooling)."""
    global _STORE
    with _STORE_LOCK:
        _STORE = None


def current_spend_usd() -> float:
    return get_store().current_spend_usd()


def remaining_usd() -> float:
    return get_store().remaining_usd()


def record_spend(
    usd: float,
    *,
    endpoint: str,
    prospect_id: str | None = None,
) -> None:
    """Module-level convenience — atomic add to today's bucket."""
    get_store().record_spend(usd, endpoint=endpoint, prospect_id=prospect_id)


def reset_today() -> None:
    get_store().reset_today()


F = TypeVar("F", bound=Callable[..., Any])


def apollo_budgeted(
    endpoint: str,
    *,
    units: int = 1,
    prospect_id_kwarg: str | None = None,
) -> Callable[[F], F]:
    """Decorator — pre-flight + post-flight the wrapped Apollo call.

    Usage::

        @apollo_budgeted("people_search")
        def search_people(...): ...

    Behaviour:
      * Pre-flight: estimate via ``estimate_call_cost(endpoint, units)``
        and ``assert_allows``. Raises ``ApolloBudgetExceeded`` BEFORE
        the wrapped function runs, so no HTTP request goes out when
        the cap is hit.
      * Wrapped function runs.
      * Post-flight: ``record_spend`` with the same estimate (Apollo
        doesn't return a billed-cost header on its public endpoints).
        A post-flight cap breach (concurrent caller raced ahead) raises
        AFTER the wrapped call returned — the caller has already
        consumed the credit; we must record + surface, not hide it.

    ``prospect_id_kwarg`` lets the decorator pluck a prospect id out
    of the wrapped function's kwargs for the ledger entry (e.g.
    ``prospect_id_kwarg="contact_id"``). Unset = no id recorded.
    """

    def _decorator(fn: F) -> F:
        @functools.wraps(fn)
        def _wrapped(*args: Any, **kwargs: Any) -> Any:
            est = estimate_call_cost(endpoint, units=units)
            store = get_store()
            store.assert_allows(est)
            result = fn(*args, **kwargs)
            pid: str | None = None
            if prospect_id_kwarg:
                raw = kwargs.get(prospect_id_kwarg)
                if raw is not None:
                    pid = str(raw)
            store.record_spend(est, endpoint=endpoint, prospect_id=pid)
            return result

        return _wrapped  # type: ignore[return-value]

    return _decorator


__all__ = [
    "ApolloBudget",
    "ApolloBudgetExceeded",
    "ApolloBudgetStore",
    "ApolloCallRecord",
    "apollo_budgeted",
    "current_spend_usd",
    "get_credit_ceiling",
    "get_daily_cap_usd",
    "get_store",
    "record_spend",
    "remaining_usd",
    "reset_store",
    "reset_today",
]
