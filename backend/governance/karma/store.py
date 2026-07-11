"""Per-actor karma vector store (DDB row + local-JSON fallback).

Same persistence shape as ``backend.common.stake_sentence_budget`` /
``backend.heat.store`` (DDB primary, JSON fallback, lazy singleton) — but keyed
by *actor* (PK=``actor``), not a single daily bucket. **Fail-OPEN**: a read
error returns the innocent baseline vector so a store outage can only *loosen*
the gate (never halt a revenue-critical send); a write error is swallowed.

The vector's four fields are exactly ``trust_scorer.TrustInputs`` so the scorer
composes it directly. ``updated_ts`` (epoch seconds) drives time-decay toward
the baseline (see ``engine``).
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass
from typing import Any, Callable

_LOG = logging.getLogger("samus.governance.karma.store")

# Innocent default for every dimension. 0.85 -> composite 0.85 -> AUTONOMOUS,
# so an unknown/recovered actor is fully trusted until penalties pull it down.
DEFAULT_BASELINE: float = 0.85
_DEFAULT_JSON_PATH = "/opt/samus/data/karma.json"
_DIMS = ("success_rate", "policy_compliance", "resource_efficiency", "stability_score")


def _clamp(v: float) -> float:
    return max(0.0, min(1.0, float(v)))


@dataclass
class KarmaVector:
    """A single actor's running karma. Fields mirror ``trust_scorer.TrustInputs``."""

    actor: str = ""
    success_rate: float = DEFAULT_BASELINE
    policy_compliance: float = DEFAULT_BASELINE
    resource_efficiency: float = DEFAULT_BASELINE
    stability_score: float = DEFAULT_BASELINE
    updated_ts: float = 0.0

    def to_item(self) -> dict[str, Any]:
        item = asdict(self)
        # DynamoDB stores floats as Decimal; the store coerces on read.
        return item

    @classmethod
    def baseline(cls, actor: str, *, baseline: float = DEFAULT_BASELINE) -> "KarmaVector":
        b = _clamp(baseline)
        return cls(actor=actor, success_rate=b, policy_compliance=b,
                   resource_efficiency=b, stability_score=b, updated_ts=0.0)

    @classmethod
    def from_item(cls, item: dict[str, Any], *, baseline: float = DEFAULT_BASELINE) -> "KarmaVector":
        def _f(key: str) -> float:
            try:
                return _clamp(float(item.get(key, baseline)))
            except (TypeError, ValueError):
                return _clamp(baseline)
        try:
            ts = float(item.get("updated_ts", 0.0) or 0.0)
        except (TypeError, ValueError):
            ts = 0.0
        return cls(
            actor=str(item.get("actor", "") or ""),
            success_rate=_f("success_rate"),
            policy_compliance=_f("policy_compliance"),
            resource_efficiency=_f("resource_efficiency"),
            stability_score=_f("stability_score"),
            updated_ts=ts,
        )

    def dims(self) -> dict[str, float]:
        return {d: getattr(self, d) for d in _DIMS}


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
            _LOG.warning("karma json load failed: %s", exc)
            return {}

    def load(self, actor: str) -> KarmaVector | None:
        with self._lock:
            entry = self._read_all().get(actor)
            return KarmaVector.from_item(entry) if entry else None

    def save(self, vec: KarmaVector) -> bool:
        with self._lock:
            data = self._read_all()
            data[vec.actor] = asdict(vec)
            try:
                os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
                tmp = f"{self.path}.tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2, default=str)
                os.replace(tmp, self.path)
                return True
            except OSError as exc:
                _LOG.warning("karma json save failed: %s", exc)
                return False


class _DdbBackend:
    def __init__(self, table_name: str, region: str) -> None:
        self.table_name = table_name
        self.region = region

    def _table(self) -> Any:
        from backend.common import aws
        return aws.table(self.table_name, self.region)

    def load(self, actor: str) -> KarmaVector | None:
        try:
            resp = self._table().get_item(Key={"actor": actor})
        except Exception as exc:  # noqa: BLE001 — fail-open
            _LOG.warning("karma ddb load failed: %s", exc)
            return None
        item = resp.get("Item")
        return KarmaVector.from_item(item) if item else None

    def save(self, vec: KarmaVector) -> bool:
        try:
            from decimal import Decimal
            item = {
                k: (Decimal(str(v)) if isinstance(v, float) else v)
                for k, v in vec.to_item().items()
            }
            self._table().put_item(Item=item)
            return True
        except Exception as exc:  # noqa: BLE001 — fail-open
            _LOG.warning("karma ddb save failed: %s", exc)
            return False


class KarmaStore:
    """Process-wide per-actor karma store. Fail-OPEN (read error -> baseline)."""

    def __init__(
        self,
        *,
        ddb_table: str | None,
        aws_region: str = "us-west-1",
        json_path: str | None = None,
        baseline: float = DEFAULT_BASELINE,
    ) -> None:
        self._ddb = _DdbBackend(ddb_table, aws_region) if ddb_table else None
        self._json = _JsonBackend(json_path or _DEFAULT_JSON_PATH)
        self.baseline = _clamp(baseline)
        self._lock = threading.Lock()

    def load(self, actor: str) -> KarmaVector:
        """Load an actor's vector, or the innocent baseline if unknown / on error."""
        with self._lock:
            vec: KarmaVector | None = None
            if self._ddb is not None:
                vec = self._ddb.load(actor)
            if vec is None:
                vec = self._json.load(actor)
            if vec is None:
                return KarmaVector.baseline(actor, baseline=self.baseline)
            return vec

    def save(self, vec: KarmaVector) -> None:
        with self._lock:
            wrote = False
            if self._ddb is not None:
                wrote = self._ddb.save(vec)
            if not wrote:
                self._json.save(vec)  # best-effort; fail-open


_STORE: KarmaStore | None = None
_STORE_LOCK = threading.Lock()


def get_store() -> KarmaStore:
    global _STORE
    with _STORE_LOCK:
        if _STORE is not None:
            return _STORE
        ddb_table = os.getenv("DDB_KARMA_TABLE", "").strip() or None
        json_path = os.getenv("SAMUS_KARMA_PATH", "").strip() or None
        region = os.getenv("AWS_REGION", "us-west-1")
        _STORE = KarmaStore(ddb_table=ddb_table, aws_region=region, json_path=json_path)
        return _STORE


def reset_store() -> None:
    global _STORE
    with _STORE_LOCK:
        _STORE = None


__all__ = [
    "KarmaVector",
    "KarmaStore",
    "DEFAULT_BASELINE",
    "get_store",
    "reset_store",
]
