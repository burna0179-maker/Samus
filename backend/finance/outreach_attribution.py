"""Outreach conversion attribution — tie a Stripe purchase back to the exact
prospect who clicked a flyer buy-now link.

The Stripe webhook already stores ``client_reference_id`` on every event, but
only ``op_`` (Opportunity) and ``upsell_`` refs were *consumed*. Cold-outreach
flyer links carry an ``out_<prospect_id>`` ref (see
:func:`backend.outreach.flyer.buy_url`). This module consumes that: on a paid
checkout it appends a durable conversion record (prospect -> purchase: which
prospect, which offer, how much) so campaign ROI is measurable and the specific
prospect is credited — regardless of which email the customer paid with.

Idempotent by Stripe ``event_id`` (a webhook retry never double-credits). The
webhook's own event ledger remains the primary durable record; this is the
queryable outreach-attribution view built from the ``out_`` refs.
"""
from __future__ import annotations

import logging
import os
from dataclasses import asdict, dataclass
from pathlib import Path

from backend.common import persistence
from backend.common.dates import iso_now

_LOG = logging.getLogger("samus.finance.outreach_attribution")

# The namespace the flyer stamps on client_reference_id for cold-outreach
# prospects, kept distinct from op_ (opportunity) and upsell_ refs.
OUT_PREFIX = "out_"

_DEFAULT_LOG = "/opt/samus/data/finance/outreach_conversions.jsonl"


def _ledger_path() -> Path:
    return Path(os.getenv("SAMUS_OUTREACH_CONVERSIONS_LOG", _DEFAULT_LOG))


def _ledger() -> persistence.Ledger:
    return persistence.open_ledger(
        jsonl_path=str(_ledger_path()), collection="outreach_conversions",
    )


@dataclass
class ConversionRecord:
    prospect_id: str
    email: str
    amount_usd: float
    currency: str
    offer_code: str
    event_id: str
    attributed_at: str
    # First-class campaign attribution (HOTL Tranche 1). Optional + defaulted
    # so rows persisted before this field existed keep loading unchanged.
    campaign_id: str | None = None


def prospect_id_from_ref(ref: str) -> str:
    """Return the prospect id from an ``out_<prospect_id>`` ref, else ''."""
    r = (ref or "").strip()
    return r[len(OUT_PREFIX):] if r.startswith(OUT_PREFIX) and len(r) > len(OUT_PREFIX) else ""


def record_conversion(
    *,
    ref: str,
    email: str = "",
    amount_usd: float = 0.0,
    currency: str = "usd",
    offer_code: str = "",
    event_id: str = "",
    received_at: str | None = None,
    campaign_id: str | None = None,
) -> ConversionRecord | None:
    """Attribute a paid checkout to the outreach prospect in ``ref``.

    Returns the ConversionRecord on a fresh attribution, or None when ``ref`` is
    not an outreach ref or the event was already attributed (idempotent). Never
    raises — attribution must never fail the webhook's 200 to Stripe.
    """
    prospect_id = prospect_id_from_ref(ref)
    if not prospect_id:
        return None
    ledger = _ledger()
    if event_id:
        try:
            for rec in ledger.scan():
                if rec.get("event_id") == event_id:
                    return None  # already attributed (webhook retry)
        except Exception as exc:  # noqa: BLE001 — degrade to "record it"
            _LOG.warning("conversion idempotency scan failed: %s", exc)
    record = ConversionRecord(
        prospect_id=prospect_id,
        email=email or "",
        amount_usd=round(float(amount_usd or 0.0), 2),
        currency=(currency or "usd").lower(),
        offer_code=offer_code or "",
        event_id=event_id or "",
        attributed_at=received_at or iso_now(),
        campaign_id=campaign_id or None,
    )
    try:
        ledger.append(asdict(record))
        _LOG.info(
            "outreach conversion attributed: prospect=%s amount=$%.2f offer=%s "
            "event=%s", prospect_id, record.amount_usd, offer_code, event_id,
        )
    except Exception as exc:  # noqa: BLE001 — best-effort
        _LOG.warning("conversion append failed: %s", exc)
        return None
    return record


def load_conversions() -> list[ConversionRecord]:
    """Read all attributed conversions (for campaign ROI reporting)."""
    out: list[ConversionRecord] = []
    try:
        for rec in _ledger().scan():
            try:
                out.append(ConversionRecord(**{
                    k: rec.get(k) for k in ConversionRecord.__dataclass_fields__
                }))
            except (TypeError, ValueError):
                continue
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("load_conversions failed: %s", exc)
    return out
