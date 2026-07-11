"""Site telemetry ingest — the input side of Samus's website-funnel awareness.

Called from ``POST /intake/telemetry``. Records the site-owned funnel events
the operator flagged as invisible today: page views, form views, pricing
views, buy-button clicks. Combined with the ``checkout.session.created`` /
``checkout.session.expired`` funnel signals now recorded in
``backend.finance.webhook``, this closes the "we can't tell a dead form from
zero interest" gap: the ledger has BOTH the intents (site telemetry) and the
outcomes (Stripe events), so the ratio is measurable.

Wire-not-arm: SAMUS_TELEMETRY_INGEST_ENABLED (default OFF). When OFF the
endpoint still returns 200 (the site's beacon must never look broken from
the customer perspective) but nothing is persisted — the ledger stays empty.
When ON, events append to a JSONL ledger via the same ``persistence`` seam
the intake audit uses. There is deliberately NO DDB backend here yet: this
is bounded write volume, JSONL is durable, and the operator can iterate on
the analysis without pre-committing to a Dynamo schema.
"""
from __future__ import annotations

import logging
import os

from backend.common import persistence
from backend.common.config import get_settings
from backend.common.dates import iso_now

from .models import (
    StoredTelemetryEvent,
    TelemetryEventRequest,
    TelemetryEventResult,
)


_LOG = logging.getLogger("samus.intake.telemetry")
_TELEMETRY_PATH_DEFAULT = "/opt/samus/data/intake/site_telemetry.jsonl"


def _telemetry_ledger() -> persistence.JsonlLedger:
    return persistence.JsonlLedger(
        os.getenv("SAMUS_INTAKE_TELEMETRY_PATH", _TELEMETRY_PATH_DEFAULT),
    )


def record_event(
    req: TelemetryEventRequest,
    *,
    source_ip: str = "",
    user_agent: str = "",
) -> TelemetryEventResult:
    """Persist one site telemetry event. Never raises.

    Gated on ``settings.intake_telemetry_ingest_enabled`` (default False —
    wire-not-arm). When disabled, the caller still gets a 200 with
    ``status='dropped_disabled'`` so the site's beacon is never a source of
    noisy errors; when enabled, the event lands in the JSONL ledger.
    """
    ts = iso_now()
    settings = get_settings()

    if not getattr(settings, "intake_telemetry_ingest_enabled", False):
        return TelemetryEventResult(status="dropped_disabled", ts=ts)

    stored = StoredTelemetryEvent(
        event=req.event,
        path=req.path,
        referrer=req.referrer,
        session_id=req.session_id,
        client_ts=req.ts,
        received_at=ts,
        source_ip=source_ip,
        # Bound the user agent so a malicious client can't blow the row size.
        user_agent=(user_agent or "")[:512],
        sku_id=req.sku_id,
    )
    try:
        _telemetry_ledger().append(stored.model_dump())
    except OSError as exc:
        # Persist failure is not a customer-visible error — swallow, log, and
        # tell the caller the event was accepted at the boundary. Losing a
        # single page-view event is a lower-cost failure than 500-ing the
        # beacon and blocking the site.
        _LOG.warning("site telemetry append failed: %s", exc)

    return TelemetryEventResult(status="accepted", ts=ts)
