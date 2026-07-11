"""needs_warm_path — diversion queue for cold-cold prospects (G8).

Prospects with no legitimacy signal at outreach pre-flight are diverted
here for operator-only review. They are NOT lost: the operator console
exposes the queue, and a manual ``promote`` re-attaches a free-text
warmth signal so the prospect can be released back into the active pool.

Persistence: DDB table named by ``settings.ddb_needs_warm_path_table``
when set, otherwise a JSON fallback at
``<SAMUS_ARTIFACT_ROOT>/needs_warm_path.json`` (default
``/opt/samus/data/needs_warm_path.json``).

Fail-CLOSED on write — a divert that cannot persist raises
:class:`NeedsWarmPathPersistError` so the outreach pre-flight bails the
batch rather than silently dropping prospects on the floor.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from backend.common.config import get_settings

_LOG = logging.getLogger("samus.crm.needs_warm_path")

_DEFAULT_ARTIFACT_ROOT = "/opt/samus/data"


class NeedsWarmPathPersistError(RuntimeError):
    """Raised when a divert/promote cannot persist (DDB AND JSON both failed)."""


class NeedsWarmPathRecord(BaseModel):
    """One row in the needs_warm_path queue."""

    model_config = ConfigDict(extra="ignore")

    prospect_id: str
    diverted_at: str
    reason: str = "no_legitimacy_signal"
    company: str = ""
    email: str = ""
    phone: str = ""
    city: str = ""
    industry: str = ""
    snapshot: dict[str, Any] = Field(default_factory=dict)
    status: str = "pending"  # pending | promoted
    promoted_at: str = ""
    promoted_by: str = ""
    promoted_signal_kind: str = ""
    promoted_signal_source: str = ""


def _artifact_root() -> str:
    return os.getenv("SAMUS_ARTIFACT_ROOT", _DEFAULT_ARTIFACT_ROOT)


def _json_path() -> str:
    return os.path.join(_artifact_root(), "needs_warm_path.json")


def _ddb_table_name() -> str:
    s = get_settings()
    return (getattr(s, "ddb_needs_warm_path_table", "") or "").strip()


def _ddb_table() -> Any | None:
    name = _ddb_table_name()
    if not name:
        return None
    try:
        from backend.common import aws
        s = get_settings()
        return aws.table(name, s.aws_region)
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("needs_warm_path DDB table acquire failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# JSON-fallback store (atomic-replace write so a crash mid-write is recoverable)
# ---------------------------------------------------------------------------

def _read_json() -> list[dict[str, Any]]:
    path = _json_path()
    if not os.path.isfile(path):
        return []
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
        if isinstance(raw, list):
            return [r for r in raw if isinstance(r, dict)]
    except (OSError, json.JSONDecodeError) as exc:
        _LOG.warning("needs_warm_path JSON read failed: %s", exc)
    return []


def _write_json(rows: list[dict[str, Any]]) -> None:
    path = _json_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        prefix="needs_warm_path-", suffix=".json.tmp",
        dir=os.path.dirname(path),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(rows, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except OSError as exc:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise NeedsWarmPathPersistError(
            f"needs_warm_path JSON write failed: {exc}",
        ) from exc


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _build_record(prospect_id: str, prospect: Any, reason: str) -> NeedsWarmPathRecord:
    def _g(*names: str) -> str:
        for name in names:
            v = None
            try:
                v = getattr(prospect, name)
            except AttributeError:
                v = None
            if v is None and isinstance(prospect, dict):
                v = prospect.get(name)
            if v:
                return str(v).strip()
        return ""

    snapshot: dict[str, Any] = {}
    if hasattr(prospect, "model_dump"):
        try:
            snapshot = prospect.model_dump()
        except Exception:  # noqa: BLE001
            snapshot = {}
    elif isinstance(prospect, dict):
        snapshot = dict(prospect)

    return NeedsWarmPathRecord(
        prospect_id=prospect_id,
        diverted_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        reason=reason,
        company=_g("company_name", "company"),
        email=_g("owner_email", "email"),
        phone=_g("phone"),
        city=_g("city"),
        industry=_g("industry"),
        snapshot=snapshot,
    )


def divert(
    prospect_id: str,
    *,
    prospect: Any = None,
    reason: str = "no_legitimacy_signal",
    store: Any = None,  # pragma: no cover — tests inject a mock table
) -> NeedsWarmPathRecord:
    """Persist a divert. Fail-CLOSED: raises NeedsWarmPathPersistError on write failure."""
    if not (prospect_id or "").strip():
        raise NeedsWarmPathPersistError("divert refused: prospect_id is required")

    record = _build_record(prospect_id, prospect, reason)
    item = record.model_dump()

    table = store if store is not None else _ddb_table()
    ddb_ok = False
    if table is not None:
        try:
            table.put_item(Item=item)
            ddb_ok = True
        except Exception as exc:  # noqa: BLE001
            _LOG.warning(
                "needs_warm_path DDB put failed (will fall back to JSON): %s", exc,
            )

    if not ddb_ok:
        rows = _read_json()
        rows = [r for r in rows if r.get("prospect_id") != prospect_id]
        rows.append(item)
        _write_json(rows)  # raises NeedsWarmPathPersistError on failure

    return record


def list_pending(limit: int = 50, *, store: Any = None) -> list[NeedsWarmPathRecord]:
    """Return pending diverts (status == 'pending'), DDB-first, JSON fallback."""
    rows: list[dict[str, Any]] = []
    table = store if store is not None else _ddb_table()
    if table is not None:
        try:
            resp = table.scan(Limit=max(1, min(int(limit), 500)))
            rows = list(resp.get("Items") or [])
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("needs_warm_path DDB scan failed: %s", exc)
            rows = []
    if not rows:
        rows = _read_json()

    pending: list[NeedsWarmPathRecord] = []
    for raw in rows:
        if str(raw.get("status") or "pending") != "pending":
            continue
        try:
            pending.append(NeedsWarmPathRecord.model_validate(raw))
        except Exception:  # noqa: BLE001
            continue
        if len(pending) >= limit:
            break
    return pending


def promote(
    prospect_id: str,
    *,
    signal_kind: str,
    signal_source: str,
    operator_id: str,
    store: Any = None,
) -> NeedsWarmPathRecord:
    """Mark a pending divert promoted with an operator-supplied warmth signal."""
    if not (prospect_id or "").strip():
        raise NeedsWarmPathPersistError("promote refused: prospect_id is required")
    if not (signal_kind or "").strip() or not (signal_source or "").strip():
        raise NeedsWarmPathPersistError(
            "promote refused: signal_kind and signal_source are required",
        )

    table = store if store is not None else _ddb_table()
    row: dict[str, Any] | None = None
    if table is not None:
        try:
            resp = table.get_item(Key={"prospect_id": prospect_id})
            row = resp.get("Item")
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("needs_warm_path DDB get failed: %s", exc)
            row = None

    rows_json = _read_json() if row is None else []
    if row is None:
        for r in rows_json:
            if r.get("prospect_id") == prospect_id:
                row = r
                break

    if row is None:
        raise NeedsWarmPathPersistError(
            f"promote refused: prospect_id {prospect_id!r} not found",
        )

    row["status"] = "promoted"
    row["promoted_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    row["promoted_by"] = operator_id
    row["promoted_signal_kind"] = signal_kind
    row["promoted_signal_source"] = signal_source

    persisted = False
    if table is not None:
        try:
            table.put_item(Item=row)
            persisted = True
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("needs_warm_path DDB promote put failed: %s", exc)

    if not persisted:
        rows = [r for r in rows_json if r.get("prospect_id") != prospect_id]
        rows.append(row)
        _write_json(rows)

    return NeedsWarmPathRecord.model_validate(row)


__all__ = [
    "NeedsWarmPathPersistError",
    "NeedsWarmPathRecord",
    "divert",
    "list_pending",
    "promote",
]
