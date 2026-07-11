"""Hard daily cap on operator-authored stake_sentence creations.

This is Samus's *actual* outbound ceiling. The brief: every outgoing
Opportunity carries one stake_sentence Alex types himself. The cap on
how many he can author per day IS the cap on how many prospects can
move into outreach per day.

Mirrors :mod:`backend.common.llm_global_budget` structure (DDB row +
local JSON fallback, daily bucket_day reset, lazy singleton accessor)
with one inverted semantic: **fail-CLOSED on persistence failure**.

Where the LLM budget allows-on-failure (a DDB hiccup must not block
all LLM calls), the stake_sentence cap fails-CLOSED — the whole point
of the cap is to be unbypassable, so an unreachable ledger means
"refuse" rather than "permit." :class:`StakeSentenceBudgetUnavailable`
is raised in that case.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass
from typing import Any, Callable


_LOG = logging.getLogger("samus.common.stake_sentence_budget")

_CAP_PK = "STAKE_SENTENCE_CAP"
_DEFAULT_JSON_PATH = "/opt/samus/data/stake_sentence_budget.json"
_DEFAULT_DAILY_CAP = 10


class StakeSentenceBudgetUnavailable(RuntimeError):
    """Raised when neither DDB nor the JSON fallback is readable/writable.

    Fail-closed: callers MUST treat this as "no stake_sentence may be
    recorded right now," not as "permit the write and move on."
    """


def _today_utc(now_func: Callable[[], float] | None = None) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime((now_func or time.time)()))


def _iso_now(now_func: Callable[[], float] | None = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime((now_func or time.time)()))


@dataclass
class StakeSentenceBudget:
    """In-memory shape of the cap row."""

    bucket_day: str = ""
    used_today: int = 0
    last_opportunity_id: str = ""
    last_updated: str = ""

    def to_item(self) -> dict[str, Any]:
        item = asdict(self)
        item["scope"] = _CAP_PK
        return item

    @classmethod
    def from_item(cls, item: dict[str, Any]) -> "StakeSentenceBudget":
        return cls(
            bucket_day=str(item.get("bucket_day", "") or ""),
            used_today=int(item.get("used_today", 0) or 0),
            last_opportunity_id=str(item.get("last_opportunity_id", "") or ""),
            last_updated=str(item.get("last_updated", "") or ""),
        )


class _JsonBackend:
    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.Lock()

    def load(self) -> StakeSentenceBudget | None:
        with self._lock:
            if not os.path.exists(self.path):
                return None
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    data = json.load(f) or {}
            except (OSError, ValueError) as exc:
                _LOG.warning("stake_sentence_budget json load failed: %s", exc)
                raise StakeSentenceBudgetUnavailable(
                    f"json_load_failed: {exc}",
                ) from exc
            entry = data.get(_CAP_PK)
            if not entry:
                return None
            return StakeSentenceBudget.from_item(entry)

    def save(self, b: StakeSentenceBudget) -> None:
        with self._lock:
            data: dict[str, Any] = {}
            if os.path.exists(self.path):
                try:
                    with open(self.path, "r", encoding="utf-8") as f:
                        data = json.load(f) or {}
                except (OSError, ValueError) as exc:
                    raise StakeSentenceBudgetUnavailable(
                        f"json_pre_read_failed: {exc}",
                    ) from exc
            data[_CAP_PK] = asdict(b)
            try:
                os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
                tmp = f"{self.path}.tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2, default=str)
                os.replace(tmp, self.path)
            except OSError as exc:
                raise StakeSentenceBudgetUnavailable(
                    f"json_save_failed: {exc}",
                ) from exc


class _DdbBackend:
    def __init__(self, table_name: str, region: str) -> None:
        self.table_name = table_name
        self.region = region

    def _table(self) -> Any:
        from . import aws

        return aws.table(self.table_name, self.region)

    def load(self) -> StakeSentenceBudget | None:
        try:
            resp = self._table().get_item(Key={"scope": _CAP_PK})
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("stake_sentence_budget ddb load failed: %s", exc)
            return None
        item = resp.get("Item")
        if not item:
            return None
        try:
            return StakeSentenceBudget.from_item(item)
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("stake_sentence_budget ddb row malformed: %s", exc)
            return None

    def save(self, b: StakeSentenceBudget) -> bool:
        try:
            self._table().put_item(Item=b.to_item())
            return True
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("stake_sentence_budget ddb save failed: %s", exc)
            return False


class StakeSentenceBudgetStore:
    """Process-wide daily-cap store. Fail-closed on persistence failure."""

    def __init__(
        self,
        *,
        daily_cap: int,
        ddb_table: str | None,
        aws_region: str = "us-west-1",
        json_path: str | None = None,
        now_func: Callable[[], float] | None = None,
    ) -> None:
        if daily_cap < 0:
            raise ValueError("daily_cap must be >= 0")
        self.daily_cap = int(daily_cap)
        self._ddb = _DdbBackend(ddb_table, aws_region) if ddb_table else None
        self._json = _JsonBackend(json_path or _DEFAULT_JSON_PATH)
        self._now: Callable[[], float] = now_func or time.time
        self._lock = threading.Lock()

    def _load_or_init(self) -> StakeSentenceBudget:
        b: StakeSentenceBudget | None = None
        ddb_attempted = False
        if self._ddb is not None:
            ddb_attempted = True
            b = self._ddb.load()
        if b is None:
            try:
                b = self._json.load()
            except StakeSentenceBudgetUnavailable:
                if not ddb_attempted:
                    raise
                b = None
        if b is None:
            b = StakeSentenceBudget(bucket_day=_today_utc(self._now))

        today = _today_utc(self._now)
        if b.bucket_day != today:
            b.bucket_day = today
            b.used_today = 0
            b.last_opportunity_id = ""
        return b

    def _persist(self, b: StakeSentenceBudget) -> None:
        b.last_updated = _iso_now(self._now)
        wrote_ddb = False
        if self._ddb is not None:
            wrote_ddb = self._ddb.save(b)
        if not wrote_ddb:
            self._json.save(b)

    def count_today(self) -> int:
        with self._lock:
            return self._load_or_init().used_today

    def remaining_today(self) -> int:
        with self._lock:
            used = self._load_or_init().used_today
            return max(0, self.daily_cap - used)

    def record_use(self, opportunity_id: str) -> None:
        opp = (opportunity_id or "").strip()
        if not opp:
            raise ValueError("opportunity_id is required to record a stake_sentence use")
        with self._lock:
            b = self._load_or_init()
            if b.used_today >= self.daily_cap:
                raise StakeSentenceBudgetUnavailable(
                    f"daily_cap_exhausted: used={b.used_today} cap={self.daily_cap}",
                )
            b.used_today += 1
            b.last_opportunity_id = opp
            self._persist(b)

    def reset_today(self) -> None:
        with self._lock:
            b = self._load_or_init()
            b.used_today = 0
            b.last_opportunity_id = ""
            self._persist(b)

    def snapshot(self) -> StakeSentenceBudget:
        with self._lock:
            return self._load_or_init()


_STORE: StakeSentenceBudgetStore | None = None
_STORE_LOCK = threading.Lock()


def _resolve_cap_from_env() -> int:
    raw = os.getenv("SAMUS_STAKE_SENTENCE_DAILY_CAP", "")
    if not raw.strip():
        return _DEFAULT_DAILY_CAP
    try:
        value = int(raw)
    except ValueError as exc:
        raise StakeSentenceBudgetUnavailable(
            f"SAMUS_STAKE_SENTENCE_DAILY_CAP invalid: {raw!r}",
        ) from exc
    if value < 0:
        raise StakeSentenceBudgetUnavailable(
            f"SAMUS_STAKE_SENTENCE_DAILY_CAP must be >= 0 (got {value})",
        )
    return value


def get_store() -> StakeSentenceBudgetStore:
    global _STORE
    with _STORE_LOCK:
        if _STORE is not None:
            return _STORE
        ddb_table = os.getenv("DDB_STAKE_SENTENCE_BUDGETS_TABLE", "").strip() or None
        json_path = os.getenv("SAMUS_STAKE_SENTENCE_BUDGET_PATH", "").strip() or None
        region = os.getenv("AWS_REGION", "us-west-1")
        _STORE = StakeSentenceBudgetStore(
            daily_cap=_resolve_cap_from_env(),
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
    "StakeSentenceBudget",
    "StakeSentenceBudgetStore",
    "StakeSentenceBudgetUnavailable",
    "get_store",
    "reset_store",
]
