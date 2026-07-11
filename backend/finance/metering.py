"""Metered usage reporting — push call-minute usage to a Stripe Meter.

The AI Digital Receptionist is billed flat base + per-call-minute overage.
The metered half is a Stripe ``usage_type=metered`` price bound to a Meter;
after each inbound call the voice workcell reports the call's duration here
and Stripe rolls the usage onto the metered subscription item's invoice at
period close — no further call needed.

``report_call_minutes`` NEVER raises: a metering failure must not fail the
inbound webhook (Vapi would retry and double-handle the call). Every outcome,
success or failure, is appended to ``meter_events.jsonl`` so the operator can
reconcile against Stripe or manually replay a missed event.

Minutes are rounded UP to whole minutes (telecom convention — a 61-second
call bills 2 minutes). ``call_id`` is passed to Stripe as the meter event
``identifier`` so a webhook redelivery is idempotent and cannot double-bill.
"""

from __future__ import annotations

import json
import logging
import math
import os
from pathlib import Path

from backend.common.config import get_settings
from backend.common.dates import iso_now
from backend.retainer.registry import get_retainer_sku

from .models import MeterReportResult
from .stripe_client import StripeClient, StripeError


_LOG = logging.getLogger("samus.finance.metering")

_METER_LOG_DEFAULT = "/opt/samus/data/finance/meter_events.jsonl"
_DEFAULT_SKU_ID = "retainer_ai_receptionist"


def _meter_log_path() -> Path:
    return Path(os.getenv("SAMUS_METER_EVENT_LOG", _METER_LOG_DEFAULT))


def _append_meter_log(record: dict) -> None:
    """Append one audit row to meter_events.jsonl. Never raises."""
    path = _meter_log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, separators=(",", ":")) + "\n")
    except OSError as exc:
        _LOG.warning("meter event log append failed: %s", exc)


def seconds_to_billable_minutes(duration_seconds: float | None) -> int:
    """Whole billable minutes for a call, rounded UP (telecom convention)."""
    if not duration_seconds or duration_seconds <= 0:
        return 0
    return int(math.ceil(duration_seconds / 60.0))


def report_call_minutes(
    *,
    stripe_customer_id: str,
    call_id: str,
    duration_seconds: float,
    sku_id: str = _DEFAULT_SKU_ID,
    client: StripeClient | None = None,
) -> MeterReportResult:
    """Report one inbound call's billable minutes to the SKU's Stripe Meter.

    Never raises. Returns a :class:`MeterReportResult`; every call (including
    failures and zero-minute no-ops) appends an audit row to
    ``meter_events.jsonl``.

    PILOT NOTE — included minutes: the pilot SKU runs
    ``included_units_per_cycle == 0`` (bill from minute 1), so there is no
    month-to-date netting and no counter state. If a SKU later sets a
    non-zero included allowance, netting must be added here (a Stripe
    graduated price or an MTD counter) — see plan Open Decision #1.
    """
    ts = iso_now()

    def _fail(error: str, minutes: int = 0) -> MeterReportResult:
        result = MeterReportResult(
            ok=False,
            call_id=call_id,
            stripe_customer_id=stripe_customer_id,
            minutes_reported=minutes,
            error=error,
            ts=ts,
        )
        _append_meter_log({**result.model_dump(), "sku_id": sku_id})
        return result

    if not stripe_customer_id:
        return _fail("stripe_customer_id_unset")
    if not call_id:
        return _fail("call_id_unset")

    sku = get_retainer_sku(sku_id)
    if sku is None:
        return _fail(f"unknown_sku:{sku_id}")
    event_name = sku.stripe_meter_event_name
    if not event_name:
        return _fail(f"sku_has_no_meter:{sku_id}")

    billable = seconds_to_billable_minutes(duration_seconds)
    if billable <= 0:
        # A 0-second call (failed connect, immediate hangup) is billable-clean
        # — log the no-op so the ledger still has a row for the call.
        result = MeterReportResult(
            ok=True,
            call_id=call_id,
            stripe_customer_id=stripe_customer_id,
            minutes_reported=0,
            meter_event_id="",
            error="",
            ts=ts,
        )
        _append_meter_log(
            {**result.model_dump(), "sku_id": sku_id, "note": "zero_billable_minutes"}
        )
        return result

    if client is None:
        api_key = (get_settings().stripe_api_key or "").strip()
        if not api_key:
            return _fail("stripe_api_key_unset", minutes=billable)
        client = StripeClient(api_key=api_key)

    try:
        raw = client.create_meter_event(
            event_name=event_name,
            stripe_customer_id=stripe_customer_id,
            value=billable,
            identifier=call_id,  # idempotency — redelivery can't double-bill
        )
    except (StripeError, ValueError) as exc:
        return _fail(f"stripe_meter_event_failed: {exc}", minutes=billable)

    result = MeterReportResult(
        ok=True,
        call_id=call_id,
        stripe_customer_id=stripe_customer_id,
        minutes_reported=billable,
        meter_event_id=str(raw.get("identifier") or raw.get("id") or ""),
        error="",
        ts=ts,
    )
    _append_meter_log({**result.model_dump(), "sku_id": sku_id})
    return result
