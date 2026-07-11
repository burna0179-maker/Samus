"""TTL-bounded operator approval store — the HOTL approval queue.

One canonical place every subsystem submits "a human must say yes" requests
to: stake-sentence drafts (cash_engine/stake_drafter), non-auto-applied
entropy countermeasures (portfolio_controller/enforcer), and HIGH/CRITICAL
governance classifications (cash_engine gate, /autonomy/plan).

Severity + TTL semantics REUSE ADR-0019 (host repo — "emergency severity +
TTL for irreversible rung approvals", commit 52d393d9): every request carries
a ``severity`` (``emergency`` for high/critical risk, ``routine`` otherwise)
and a ``ttl_expires_at`` deadline. Expiry is FAIL-CLOSED: an expired request
never auto-passes — it flips to ``expired`` and whatever it was unblocking
stays blocked. This module widens the ADR's default deadlines for the
human-cadence queue (morning-brief driven) but keeps the scheme: emergency
requests get the short window, routine the long one, both env-tunable — it
does NOT fork a parallel severity scheme.

Storage follows the crm/persistence.py table pattern: DDB table
``samus_approvals`` (PK=id) attempted first when configured, with a JSON-file
fallback under the state root so a dev box / test run needs no AWS. Writes
never raise; readers auto-expire stale rows.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from typing import Any

from backend.common.dates import iso_now

_LOG = logging.getLogger("samus.common.approvals")

# ---------------------------------------------------------------------------
# Severity + TTL (ADR-0019 semantics)
# ---------------------------------------------------------------------------

SEVERITY_EMERGENCY = "emergency"
SEVERITY_ROUTINE = "routine"

# Risk levels come from backend.common.governance.classify_risk:
# {"critical", "high", "normal"}.
_EMERGENCY_RISK_LEVELS = frozenset({"critical", "high"})

STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
STATUS_EXPIRED = "expired"

_DEFAULT_TTL_EMERGENCY_SEC = 4 * 3600    # per-item, operator alerted same day
_DEFAULT_TTL_ROUTINE_SEC = 72 * 3600     # survives a weekend of morning briefs


def severity_for(risk_level: str) -> str:
    """ADR-0019 mapping: high/critical risk -> emergency, else routine."""
    return (
        SEVERITY_EMERGENCY
        if (risk_level or "").strip().lower() in _EMERGENCY_RISK_LEVELS
        else SEVERITY_ROUTINE
    )


def _default_ttl_sec(severity: str) -> float:
    if severity == SEVERITY_EMERGENCY:
        raw = os.getenv("SAMUS_APPROVAL_TTL_EMERGENCY_SEC", "")
        default = _DEFAULT_TTL_EMERGENCY_SEC
    else:
        raw = os.getenv("SAMUS_APPROVAL_TTL_ROUTINE_SEC", "")
        default = _DEFAULT_TTL_ROUTINE_SEC
    try:
        return float(raw) if raw else float(default)
    except ValueError:
        return float(default)


def _iso_at(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def _iso_to_epoch(value: str) -> float:
    import calendar

    try:
        return float(calendar.timegm(time.strptime(value, "%Y-%m-%dT%H:%M:%SZ")))
    except (ValueError, TypeError):
        return 0.0


# ---------------------------------------------------------------------------
# Storage — DDB first (crm/persistence.py pattern), JSON-file fallback
# ---------------------------------------------------------------------------

_JSON_LOCK = threading.Lock()


def _json_path() -> str:
    override = os.getenv("SAMUS_APPROVALS_PATH")
    if override:
        return override
    from backend.common.state_paths import state_path

    return str(state_path("approvals", "approvals.json"))


def _json_load_all() -> dict[str, dict[str, Any]]:
    path = _json_path()
    with _JSON_LOCK:
        if not os.path.exists(path):
            return {}
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return json.load(fh) or {}
        except (OSError, ValueError) as exc:
            _LOG.warning("approvals json load failed: %s", exc)
            return {}


def _json_save(row: dict[str, Any]) -> bool:
    path = _json_path()
    with _JSON_LOCK:
        data: dict[str, Any] = {}
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    data = json.load(fh) or {}
            except (OSError, ValueError):
                data = {}
        data[row["id"]] = row
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            tmp = f"{path}.tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2, default=str)
            os.replace(tmp, path)
            return True
        except (OSError, ValueError) as exc:
            _LOG.warning("approvals json save failed: %s", exc)
            return False


def _ddb_table() -> Any | None:
    """The samus_approvals DDB table, or None when unreachable/unconfigured."""
    try:
        from backend.common import aws
        from backend.common.config import get_settings

        s = get_settings()
        table_name = getattr(s, "ddb_approvals_table", "") or ""
        if not table_name:
            return None
        return aws.table(table_name, s.aws_region)
    except Exception as exc:  # noqa: BLE001 — fallback path
        _LOG.debug("approvals ddb table unavailable: %s", exc)
        return None


def _ddb_put(row: dict[str, Any]) -> bool:
    table = _ddb_table()
    if table is None:
        return False
    try:
        table.put_item(Item=row)
        return True
    except Exception as exc:  # noqa: BLE001 — never raise on persistence
        _LOG.warning("approvals ddb put failed: %s", exc)
        return False


def _ddb_scan() -> list[dict[str, Any]] | None:
    table = _ddb_table()
    if table is None:
        return None
    try:
        resp = table.scan()
        items = list(resp.get("Items") or [])
        while resp.get("LastEvaluatedKey"):
            resp = table.scan(ExclusiveStartKey=resp["LastEvaluatedKey"])
            items.extend(resp.get("Items") or [])
        return items
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("approvals ddb scan failed: %s", exc)
        return None


def _save(row: dict[str, Any]) -> None:
    """Write-through: DDB when reachable, ALWAYS mirrored to the JSON file."""
    wrote_ddb = _ddb_put(row)
    wrote_json = _json_save(row)
    if not wrote_ddb and not wrote_json:
        _LOG.warning("approval %s not persisted anywhere (degraded)", row.get("id"))


def _load_all() -> dict[str, dict[str, Any]]:
    """All rows, DDB view merged over the JSON mirror (DDB wins on conflict)."""
    merged = _json_load_all()
    ddb_rows = _ddb_scan()
    if ddb_rows:
        for item in ddb_rows:
            rid = str(item.get("id") or "")
            if rid:
                merged[rid] = {**merged.get(rid, {}), **_plain(item)}
    return merged


def _plain(item: dict[str, Any]) -> dict[str, Any]:
    """Decimal-free copy of a DDB item (JSON-serializable)."""
    from decimal import Decimal

    def conv(v: Any) -> Any:
        if isinstance(v, Decimal):
            f = float(v)
            return int(f) if f.is_integer() else f
        if isinstance(v, dict):
            return {k: conv(x) for k, x in v.items()}
        if isinstance(v, list):
            return [conv(x) for x in v]
        return v

    return {k: conv(v) for k, v in item.items()}


# ---------------------------------------------------------------------------
# Expiry
# ---------------------------------------------------------------------------

def _expire_if_due(row: dict[str, Any]) -> dict[str, Any]:
    """Flip a stale ``pending`` row to ``expired`` (fail-closed, persisted)."""
    if row.get("status") == STATUS_PENDING:
        expires = str(row.get("ttl_expires_at") or "")
        if expires and time.time() >= _iso_to_epoch(expires):
            row = dict(row)
            row["status"] = STATUS_EXPIRED
            _save(row)
    return row


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def create_approval(
    kind: str,
    payload: dict[str, Any] | None = None,
    *,
    risk_level: str = "normal",
    ev_usd: float | None = None,
    confidence: float | None = None,
    ttl_seconds: float | None = None,
) -> dict[str, Any]:
    """Submit an approval request. Never raises; returns the persisted row.

    Severity + TTL follow ADR-0019: high/critical risk -> ``emergency`` with
    the short deadline, everything else -> ``routine`` with the long one.
    Expiry is fail-closed (row -> ``expired``; the gated action stays blocked).
    """
    try:
        level = (risk_level or "normal").strip().lower()
        severity = severity_for(level)
        ttl = float(ttl_seconds) if ttl_seconds else _default_ttl_sec(severity)
        row: dict[str, Any] = {
            "id": uuid.uuid4().hex,
            "kind": str(kind),
            "payload": dict(payload or {}),
            "risk_level": level,
            "severity": severity,
            "ev_usd": float(ev_usd) if ev_usd is not None else None,
            "confidence": float(confidence) if confidence is not None else None,
            "status": STATUS_PENDING,
            "ttl_expires_at": _iso_at(time.time() + max(1.0, ttl)),
            "decided_by": None,
            "decided_at": None,
            "created_at": iso_now(),
        }
        _save(row)
        _emit_decision_event("approval.requested", row)
        return row
    except Exception as exc:  # noqa: BLE001 — approval creation must not sink callers
        _LOG.warning("create_approval failed (%s); returning degraded row", exc)
        return {"id": "", "kind": str(kind), "status": "error", "error": str(exc)}


def get_approval(approval_id: str) -> dict[str, Any] | None:
    """One row by id (auto-expiring). Never raises."""
    try:
        row = _load_all().get(str(approval_id))
        return _expire_if_due(row) if row else None
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("get_approval failed: %s", exc)
        return None


def list_approvals(
    status: str | None = STATUS_PENDING,
    kind: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Rows filtered by status/kind, oldest first (auto-expiring). Never raises."""
    try:
        rows = [_expire_if_due(r) for r in _load_all().values()]
        if status:
            rows = [r for r in rows if r.get("status") == status]
        if kind:
            rows = [r for r in rows if r.get("kind") == kind]
        rows.sort(key=lambda r: str(r.get("created_at") or ""))
        return rows[: max(0, int(limit))]
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("list_approvals failed: %s", exc)
        return []


def decide_approval(
    approval_id: str,
    decision: str,
    *,
    decided_by: str = "operator",
) -> dict[str, Any] | None:
    """Approve/reject one PENDING, unexpired request.

    ``decision`` in {"approved", "rejected"} (also accepts "approve"/"reject").
    Returns the updated row, or None when the id is unknown / not pending /
    already expired (expiry is fail-closed — a stale approval can't be minted).
    """
    verb = (decision or "").strip().lower()
    normalized = {
        "approve": STATUS_APPROVED, "approved": STATUS_APPROVED,
        "reject": STATUS_REJECTED, "rejected": STATUS_REJECTED,
    }.get(verb)
    if normalized is None:
        return None
    row = get_approval(approval_id)
    if row is None or row.get("status") != STATUS_PENDING:
        return None
    row = dict(row)
    row["status"] = normalized
    row["decided_by"] = str(decided_by)
    row["decided_at"] = iso_now()
    _save(row)
    _emit_decision_event(f"approval.{normalized}", row)
    return row


def batch_approve(
    approval_ids: list[str],
    *,
    decided_by: str = "operator",
) -> dict[str, Any]:
    """One-action batch approval — allowed ONLY for low-risk routine requests.

    High/critical (emergency-severity) requests are always per-item, per the
    plan's HOTL guarantee. Returns ``{"approved": [...], "skipped": [...]}``.
    """
    approved: list[str] = []
    skipped: list[dict[str, Any]] = []
    for approval_id in approval_ids:
        row = get_approval(approval_id)
        if row is None or row.get("status") != STATUS_PENDING:
            skipped.append({"id": approval_id, "why": "not_pending"})
            continue
        if row.get("severity") == SEVERITY_EMERGENCY:
            skipped.append({"id": approval_id, "why": "emergency_requires_per_item"})
            continue
        decided = decide_approval(approval_id, "approved", decided_by=decided_by)
        if decided:
            approved.append(approval_id)
        else:
            skipped.append({"id": approval_id, "why": "decide_failed"})
    return {"approved": approved, "skipped": skipped}


def is_currently_approved(approval_id: str) -> bool:
    """True iff the row is ``approved`` (decisions don't TTL-expire; only
    pending requests do — an operator's explicit yes stands until superseded).
    """
    row = get_approval(approval_id)
    return bool(row and row.get("status") == STATUS_APPROVED)


def _emit_decision_event(event_name: str, row: dict[str, Any]) -> None:
    """Mirror approval lifecycle onto the unified business-event stream."""
    try:
        from backend.common.business_events import DECISION_MADE, emit_business_event

        emit_business_event(
            DECISION_MADE,
            workcell="governance",
            opportunity_id=(row.get("payload") or {}).get("opportunity_id"),
            prospect_id=(row.get("payload") or {}).get("prospect_id"),
            metadata={
                "decision": event_name,
                "approval_id": row.get("id"),
                "kind": row.get("kind"),
                "risk_level": row.get("risk_level"),
                "severity": row.get("severity"),
                "decided_by": row.get("decided_by"),
            },
        )
    except Exception as exc:  # noqa: BLE001 — event mirror is best-effort
        _LOG.debug("approval business-event emit skipped: %s", exc)


__all__ = [
    "SEVERITY_EMERGENCY",
    "SEVERITY_ROUTINE",
    "STATUS_PENDING",
    "STATUS_APPROVED",
    "STATUS_REJECTED",
    "STATUS_EXPIRED",
    "severity_for",
    "create_approval",
    "get_approval",
    "list_approvals",
    "decide_approval",
    "batch_approve",
    "is_currently_approved",
]
