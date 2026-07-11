"""Daily heat-state counter store (DDB row + local-JSON fallback, bucket reset).

Structurally mirrors :mod:`backend.common.stake_sentence_budget` (DDB primary +
JSON fallback, ``bucket_day`` daily reset, lazy singleton) with one inverted
semantic: **fail-OPEN**. The stake cap fails closed because its whole job is to
be unbypassable; the heat store is the opposite — a persistence hiccup must
neither block a send nor *falsely* trip a throttle, so an unreachable backend
degrades to an empty (cool) snapshot and best-effort writes.

This is also the canonical **"emails sent today"** counter — before this,
nothing in the codebase tracked outbound send volume (the live cash-engine send
path never ticked any ramp counter). ``record_send`` is the single tick; the
SendGrid event webhook ticks the delivery-outcome counters.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass
from typing import Any, Callable

from backend.heat.metrics import HeatInputs

_LOG = logging.getLogger("samus.heat.store")

_HEAT_PK = "HEAT_STATE"
_DEFAULT_JSON_PATH = "/opt/samus/data/heat_state.json"

# Below this many sends today, rates are too noisy to act on — the service
# treats the day as cool regardless of a stray bounce/complaint.
MIN_SAMPLE_FOR_HEAT: int = 20

# SendGrid event "event" field -> counter name. Unknown events are ignored.
_EVENT_TO_COUNTER: dict[str, str] = {
    "delivered": "delivered",
    "bounce": "bounced",
    "blocked": "blocked",
    "dropped": "blocked",
    "deferred": "deferred",
    "spamreport": "complained",
    "spam_report": "complained",
    "open": "opened",
    "click": "clicked",
    "unsubscribe": "unsubscribed",
    "group_unsubscribe": "unsubscribed",
    "processed": "processed",
}

_COUNTERS = (
    "sent",
    "processed",
    "delivered",
    "bounced",
    "blocked",
    "deferred",
    "complained",
    "opened",
    "clicked",
    "unsubscribed",
)


def _today_utc(now_func: Callable[[], float] | None = None) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime((now_func or time.time)()))


def _iso_now(now_func: Callable[[], float] | None = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime((now_func or time.time)()))


@dataclass
class HeatState:
    """In-memory shape of the daily heat-state row."""

    bucket_day: str = ""
    sent: int = 0
    processed: int = 0
    delivered: int = 0
    bounced: int = 0
    blocked: int = 0
    deferred: int = 0
    complained: int = 0
    opened: int = 0
    clicked: int = 0
    unsubscribed: int = 0
    last_updated: str = ""

    def to_item(self) -> dict[str, Any]:
        item = asdict(self)
        item["scope"] = _HEAT_PK
        return item

    @classmethod
    def from_item(cls, item: dict[str, Any]) -> "HeatState":
        def _i(key: str) -> int:
            try:
                return int(item.get(key, 0) or 0)
            except (TypeError, ValueError):
                return 0

        return cls(
            bucket_day=str(item.get("bucket_day", "") or ""),
            sent=_i("sent"),
            processed=_i("processed"),
            delivered=_i("delivered"),
            bounced=_i("bounced"),
            blocked=_i("blocked"),
            deferred=_i("deferred"),
            complained=_i("complained"),
            opened=_i("opened"),
            clicked=_i("clicked"),
            unsubscribed=_i("unsubscribed"),
            last_updated=str(item.get("last_updated", "") or ""),
        )

    def to_inputs(self) -> HeatInputs:
        """Derive reputation-stress rates. Denominator prefers delivered (the
        real exposure surface) but falls back to sent; rates are 0 below the
        min-sample floor so a tiny sample never trips a throttle."""
        denom = max(self.delivered, self.sent)
        if denom < MIN_SAMPLE_FOR_HEAT:
            return HeatInputs()
        return HeatInputs(
            complaint_rate=self.complained / denom,
            bounce_rate=self.bounced / denom,
            block_rate=self.blocked / denom,
            deferral_rate=self.deferred / denom,
        )


class _JsonBackend:
    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.Lock()

    def load(self) -> HeatState | None:
        with self._lock:
            if not os.path.exists(self.path):
                return None
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    data = json.load(f) or {}
            except (OSError, ValueError) as exc:
                _LOG.warning("heat_state json load failed: %s", exc)
                return None  # fail-open
            entry = data.get(_HEAT_PK)
            return HeatState.from_item(entry) if entry else None

    def save(self, state: HeatState) -> bool:
        with self._lock:
            data: dict[str, Any] = {}
            if os.path.exists(self.path):
                try:
                    with open(self.path, "r", encoding="utf-8") as f:
                        data = json.load(f) or {}
                except (OSError, ValueError):
                    data = {}
            data[_HEAT_PK] = asdict(state)
            try:
                os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
                tmp = f"{self.path}.tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2, default=str)
                os.replace(tmp, self.path)
                return True
            except OSError as exc:
                _LOG.warning("heat_state json save failed: %s", exc)
                return False


class _DdbBackend:
    def __init__(self, table_name: str, region: str) -> None:
        self.table_name = table_name
        self.region = region

    def _table(self) -> Any:
        from backend.common import aws

        return aws.table(self.table_name, self.region)

    def load(self) -> HeatState | None:
        try:
            resp = self._table().get_item(Key={"scope": _HEAT_PK})
        except Exception as exc:  # noqa: BLE001 — fail-open
            _LOG.warning("heat_state ddb load failed: %s", exc)
            return None
        item = resp.get("Item")
        if not item:
            return None
        try:
            return HeatState.from_item(item)
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("heat_state ddb row malformed: %s", exc)
            return None

    def save(self, state: HeatState) -> bool:
        try:
            self._table().put_item(Item=state.to_item())
            return True
        except Exception as exc:  # noqa: BLE001 — fail-open
            _LOG.warning("heat_state ddb save failed: %s", exc)
            return False


class HeatStateStore:
    """Process-wide daily heat-state store. Fail-OPEN on persistence failure."""

    def __init__(
        self,
        *,
        ddb_table: str | None,
        aws_region: str = "us-west-1",
        json_path: str | None = None,
        now_func: Callable[[], float] | None = None,
    ) -> None:
        self._ddb = _DdbBackend(ddb_table, aws_region) if ddb_table else None
        self._json = _JsonBackend(json_path or _DEFAULT_JSON_PATH)
        self._now: Callable[[], float] = now_func or time.time
        self._lock = threading.Lock()

    def _load_or_init(self) -> HeatState:
        state: HeatState | None = None
        if self._ddb is not None:
            state = self._ddb.load()
        if state is None:
            state = self._json.load()
        if state is None:
            state = HeatState(bucket_day=_today_utc(self._now))

        today = _today_utc(self._now)
        if state.bucket_day != today:
            # New day — reset every counter (yesterday's reputation is yesterday's).
            state = HeatState(bucket_day=today)
        return state

    def _persist(self, state: HeatState) -> None:
        state.last_updated = _iso_now(self._now)
        wrote = False
        if self._ddb is not None:
            wrote = self._ddb.save(state)
        if not wrote:
            self._json.save(state)  # best-effort; fail-open

    def record_send(self, count: int = 1) -> None:
        if count <= 0:
            return
        with self._lock:
            state = self._load_or_init()
            state.sent += int(count)
            self._persist(state)

    def record_event(self, event_type: str, count: int = 1) -> str | None:
        """Tick the counter for a SendGrid event. Returns the counter name hit,
        or None for an unrecognised event. Best-effort (never raises)."""
        counter = _EVENT_TO_COUNTER.get((event_type or "").strip().lower())
        if counter is None or count <= 0:
            return None
        with self._lock:
            state = self._load_or_init()
            setattr(state, counter, getattr(state, counter, 0) + int(count))
            self._persist(state)
        return counter

    def snapshot(self) -> HeatState:
        with self._lock:
            return self._load_or_init()

    def reset_today(self) -> None:
        with self._lock:
            self._persist(HeatState(bucket_day=_today_utc(self._now)))


_STORE: HeatStateStore | None = None
_STORE_LOCK = threading.Lock()


def get_store() -> HeatStateStore:
    global _STORE
    with _STORE_LOCK:
        if _STORE is not None:
            return _STORE
        ddb_table = os.getenv("DDB_HEAT_STATE_TABLE", "").strip() or None
        json_path = os.getenv("SAMUS_HEAT_STATE_PATH", "").strip() or None
        region = os.getenv("AWS_REGION", "us-west-1")
        _STORE = HeatStateStore(
            ddb_table=ddb_table,
            aws_region=region,
            json_path=json_path,
        )
        return _STORE


def reset_store() -> None:
    global _STORE
    with _STORE_LOCK:
        _STORE = None


__all__ = [
    "HeatState",
    "HeatStateStore",
    "MIN_SAMPLE_FOR_HEAT",
    "get_store",
    "reset_store",
]
