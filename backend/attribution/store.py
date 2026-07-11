"""Per-variant attribution stats store (DDB row + local-JSON fallback).

Same persistence shape as ``backend.heat.store`` / ``backend.governance.karma``
(DDB primary, JSON fallback, lazy singleton) keyed by ``arm_id`` (the variant
tuple). **Fail-OPEN**: a read error returns ``None`` (treated as a cold arm),
a write error is swallowed — attribution is an optimization, never load-bearing.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import asdict, dataclass
from typing import Any

_LOG = logging.getLogger("samus.attribution.store")

_DEFAULT_JSON_PATH = "/opt/samus/data/attribution_variants.json"


@dataclass
class VariantStats:
    """Bandit stats for one email variant arm."""

    arm_id: str = ""
    trials: int = 0  # sends attributed to this variant
    reward_sum: float = 0.0  # accumulated [0,1] reward
    wins: int = 0  # closed deals
    last_updated: str = ""

    @property
    def mean_reward(self) -> float:
        return (self.reward_sum / self.trials) if self.trials > 0 else 0.0

    def to_item(self) -> dict[str, Any]:
        item = asdict(self)
        item["scope"] = self.arm_id
        return item

    @classmethod
    def from_item(cls, item: dict[str, Any]) -> "VariantStats":
        def _i(k: str) -> int:
            try:
                return int(item.get(k, 0) or 0)
            except (TypeError, ValueError):
                return 0

        def _f(k: str) -> float:
            try:
                return float(item.get(k, 0.0) or 0.0)
            except (TypeError, ValueError):
                return 0.0

        return cls(
            arm_id=str(item.get("arm_id", "") or ""),
            trials=_i("trials"),
            reward_sum=_f("reward_sum"),
            wins=_i("wins"),
            last_updated=str(item.get("last_updated", "") or ""),
        )


class _JsonBackend:
    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.Lock()

    def _read_all(self) -> dict[str, Any]:
        if not os.path.exists(self.path):
            return {}
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                return json.load(f) or {}
        except (OSError, ValueError) as exc:
            _LOG.warning("attribution json load failed: %s", exc)
            return {}

    def load(self, arm_id: str) -> VariantStats | None:
        with self._lock:
            entry = self._read_all().get(arm_id)
            return VariantStats.from_item(entry) if entry else None

    def save(self, stats: VariantStats) -> bool:
        with self._lock:
            data = self._read_all()
            data[stats.arm_id] = asdict(stats)
            try:
                os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
                tmp = f"{self.path}.tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2, default=str)
                os.replace(tmp, self.path)
                return True
            except OSError as exc:
                _LOG.warning("attribution json save failed: %s", exc)
                return False


class _DdbBackend:
    def __init__(self, table_name: str, region: str) -> None:
        self.table_name = table_name
        self.region = region

    def _table(self) -> Any:
        from backend.common import aws

        return aws.table(self.table_name, self.region)

    def load(self, arm_id: str) -> VariantStats | None:
        try:
            resp = self._table().get_item(Key={"scope": arm_id})
        except Exception as exc:  # noqa: BLE001 — fail-open
            _LOG.warning("attribution ddb load failed: %s", exc)
            return None
        item = resp.get("Item")
        return VariantStats.from_item(item) if item else None

    def save(self, stats: VariantStats) -> bool:
        try:
            from decimal import Decimal

            item = {
                k: (Decimal(str(v)) if isinstance(v, float) else v)
                for k, v in stats.to_item().items()
            }
            self._table().put_item(Item=item)
            return True
        except Exception as exc:  # noqa: BLE001 — fail-open
            _LOG.warning("attribution ddb save failed: %s", exc)
            return False


class VariantAttributionStore:
    """Process-wide per-variant stats store. Fail-OPEN."""

    def __init__(
        self, *, ddb_table: str | None, aws_region: str = "us-west-1", json_path: str | None = None
    ) -> None:
        self._ddb = _DdbBackend(ddb_table, aws_region) if ddb_table else None
        self._json = _JsonBackend(json_path or _DEFAULT_JSON_PATH)
        self._lock = threading.Lock()

    def load(self, arm_id: str) -> VariantStats | None:
        with self._lock:
            stats = self._ddb.load(arm_id) if self._ddb is not None else None
            if stats is None:
                stats = self._json.load(arm_id)
            return stats

    def save(self, stats: VariantStats) -> None:
        with self._lock:
            wrote = self._ddb.save(stats) if self._ddb is not None else False
            if not wrote:
                self._json.save(stats)


_STORE: VariantAttributionStore | None = None
_STORE_LOCK = threading.Lock()


def get_store() -> VariantAttributionStore:
    global _STORE
    with _STORE_LOCK:
        if _STORE is not None:
            return _STORE
        ddb_table = os.getenv("DDB_ATTRIBUTION_TABLE", "").strip() or None
        json_path = os.getenv("SAMUS_ATTRIBUTION_PATH", "").strip() or None
        region = os.getenv("AWS_REGION", "us-west-1")
        _STORE = VariantAttributionStore(
            ddb_table=ddb_table, aws_region=region, json_path=json_path
        )
        return _STORE


def reset_store() -> None:
    global _STORE
    with _STORE_LOCK:
        _STORE = None


__all__ = ["VariantStats", "VariantAttributionStore", "get_store", "reset_store"]
