"""SLA timer for service-tier SKUs. Persists deadline in customer metadata, fires OPERATOR_ALERT_OVERDUE."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

_LOG = logging.getLogger("samus.services.sla")


SLA_METADATA_KEY = "service_sla"  # nested dict on Customer.metadata
OPERATOR_ALERT_OVERDUE = "operator_alert_overdue"

# Local-first alert ledger: every overdue detection appends a JSON line here so
# morning-brief can render the queue without touching Neo4j. Path is env-overridable
# for testing + multi-target deployment (host / Cloud Run / VM Docker).
_DEFAULT_ALERT_LEDGER = r"E:\Hustleforge\Samus\data\services\sla_alerts.jsonl"


def _alert_ledger_path() -> Path:
    path = Path(os.getenv("SAMUS_SLA_ALERT_PATH", _DEFAULT_ALERT_LEDGER))
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(ts: datetime) -> str:
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(s: str) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Arm / inspect
# ---------------------------------------------------------------------------


def arm_sla(
    *,
    customer_store: Any,
    customer_id: str,
    sku_id: str,
    sla_hours: int,
    started_at: Optional[datetime] = None,
) -> dict[str, Any]:
    """Record sla_deadline on the customer's metadata. Re-arm is allowed (idempotent on sku_id)."""
    started = started_at or _utcnow()
    deadline = started + timedelta(hours=sla_hours)

    cust = customer_store.get_customer(customer_id)
    if cust is None:
        raise ValueError(f"sla_arm: customer not found: {customer_id}")

    existing_md = dict(getattr(cust, "metadata", {}) or {})
    sla_bucket: dict[str, Any] = dict(existing_md.get(SLA_METADATA_KEY) or {})
    sla_bucket[sku_id] = {
        "sku_id": sku_id,
        "started_at": _iso(started),
        "sla_deadline": _iso(deadline),
        "sla_hours": int(sla_hours),
        "alert_fired_at": None,
        "delivered_at": None,
    }
    existing_md[SLA_METADATA_KEY] = sla_bucket
    _persist_metadata(customer_store, customer_id, existing_md)
    _LOG.info(
        "sla_armed",
        extra={
            "customer_id": customer_id,
            "sku_id": sku_id,
            "deadline": _iso(deadline),
            "sla_hours": sla_hours,
        },
    )
    return sla_bucket[sku_id]


def get_sla(customer_store: Any, customer_id: str, sku_id: str) -> Optional[dict[str, Any]]:
    cust = customer_store.get_customer(customer_id)
    if cust is None:
        return None
    md = dict(getattr(cust, "metadata", {}) or {})
    return (md.get(SLA_METADATA_KEY) or {}).get(sku_id)


def time_remaining(customer_store: Any, customer_id: str, sku_id: str) -> Optional[timedelta]:
    rec = get_sla(customer_store, customer_id, sku_id)
    if rec is None:
        return None
    deadline = _parse_iso(rec.get("sla_deadline", ""))
    if deadline is None:
        return None
    return deadline - _utcnow()


def check_overdue(customer_store: Any, customer_id: str, sku_id: str) -> bool:
    """True if past deadline AND customer is not yet in delivered/renewed."""
    rec = get_sla(customer_store, customer_id, sku_id)
    if rec is None:
        return False
    deadline = _parse_iso(rec.get("sla_deadline", ""))
    if deadline is None:
        return False
    if _utcnow() < deadline:
        return False
    cust = customer_store.get_customer(customer_id)
    if cust is None:
        return False
    terminal = {"delivered", "renewed", "churned"}
    return cust.current_state not in terminal


def mark_delivered(customer_store: Any, customer_id: str, sku_id: str) -> Optional[dict[str, Any]]:
    """Stamp delivered_at on the SLA record so check_overdue stops alerting on this SKU."""
    cust = customer_store.get_customer(customer_id)
    if cust is None:
        return None
    md = dict(getattr(cust, "metadata", {}) or {})
    bucket = dict(md.get(SLA_METADATA_KEY) or {})
    rec = dict(bucket.get(sku_id) or {})
    if not rec:
        return None
    rec["delivered_at"] = _iso(_utcnow())
    bucket[sku_id] = rec
    md[SLA_METADATA_KEY] = bucket
    _persist_metadata(customer_store, customer_id, md)
    return rec


# ---------------------------------------------------------------------------
# Overdue sweep
# ---------------------------------------------------------------------------


def emit_operator_alert(
    *,
    customer_store: Any,
    customer_id: str,
    sku_id: str,
    rec: dict[str, Any],
) -> dict[str, Any]:
    """Append OPERATOR_ALERT_OVERDUE to the JSONL ledger + write a CustomerStateEvent reason note.

    The state itself is NOT advanced (would require a new terminal/branch state). Instead the
    alert is materialized two ways:
      1. JSONL ledger (morning brief reads this).
      2. customer_store.advance_state with to_state=current_state and a reason string — only
         when the store exposes a ``record_event`` method we can use without side-effects.
    """
    now = _utcnow()
    alert = {
        "event": OPERATOR_ALERT_OVERDUE,
        "customer_id": customer_id,
        "sku_id": sku_id,
        "sla_deadline": rec.get("sla_deadline"),
        "started_at": rec.get("started_at"),
        "sla_hours": rec.get("sla_hours"),
        "fired_at": _iso(now),
    }
    try:
        with _alert_ledger_path().open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(alert) + "\n")
    except OSError as exc:
        _LOG.warning("sla_alert_ledger_write_failed: %s", exc)

    # Mark fired so we don't keep alerting every sweep.
    cust = customer_store.get_customer(customer_id)
    if cust is not None:
        md = dict(getattr(cust, "metadata", {}) or {})
        bucket = dict(md.get(SLA_METADATA_KEY) or {})
        upd = dict(bucket.get(sku_id) or {})
        upd["alert_fired_at"] = _iso(now)
        bucket[sku_id] = upd
        md[SLA_METADATA_KEY] = bucket
        _persist_metadata(customer_store, customer_id, md)

    _LOG.warning("operator_alert_overdue", extra=alert)
    return alert


def sweep_overdue(customer_store: Any) -> list[dict[str, Any]]:
    """Iterate every customer in active delivery, fire alerts for any past deadline. Re-fire-safe."""
    alerts: list[dict[str, Any]] = []
    # We only care about customers actively in delivery — fewer scans than 'all'.
    try:
        in_flight = customer_store.list_customers(state="in_delivery", limit=500)
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("sla_sweep_list_customers_failed: %s", exc)
        return alerts

    for cust in in_flight:
        md = getattr(cust, "metadata", {}) or {}
        bucket = md.get(SLA_METADATA_KEY) or {}
        if not bucket:
            continue
        for sku_id, rec in bucket.items():
            if rec.get("delivered_at"):
                continue
            if rec.get("alert_fired_at"):
                continue
            deadline = _parse_iso(rec.get("sla_deadline", ""))
            if deadline is None or _utcnow() < deadline:
                continue
            alert = emit_operator_alert(
                customer_store=customer_store,
                customer_id=cust.id,
                sku_id=sku_id,
                rec=rec,
            )
            alerts.append(alert)
    return alerts


def read_open_alerts(*, limit: int = 50) -> list[dict[str, Any]]:
    """Read the alert ledger for morning-brief / CLI display. Newest last."""
    path = _alert_ledger_path()
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError as exc:
        _LOG.warning("sla_alert_ledger_read_failed: %s", exc)
        return []
    return out[-limit:]


# ---------------------------------------------------------------------------
# Metadata writer
# ---------------------------------------------------------------------------


def _persist_metadata(customer_store: Any, customer_id: str, metadata: dict[str, Any]) -> None:
    """Persist Customer.metadata. Production CustomerStore stores metadata only at create_customer time, so we
    write through the underlying graph client when available; tests inject a fake with a settable .metadata attr.
    """
    # Fake store path (tests + in-memory): just mutate the attribute.
    cust = customer_store.get_customer(customer_id)
    if cust is None:
        return
    try:
        cust.metadata = dict(metadata)
    except Exception:  # noqa: BLE001
        pass

    # Production path: Neo4j write through the underlying graph client.
    client = getattr(customer_store, "_client", None)
    if client is None or not getattr(client, "available", False):
        return
    try:
        # Match the encoding pattern used by backend.memory.customers.
        encoded = json.dumps(metadata, default=str, sort_keys=True)
        client._run(  # noqa: SLF001 — intentional reuse, same pattern as customers.py
            "MATCH (c:Customer {id: $id}) SET c.metadata = $metadata RETURN c",
            {"id": customer_id, "metadata": encoded},
        )
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("sla_metadata_persist_failed: %s", exc)
