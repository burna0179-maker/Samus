"""Heat Field — email deliverability/reputation stress modeling + throttle.

A small, deterministic control loop (modeled on ``backend/entropy/``) that turns
SendGrid delivery feedback (bounces / spam complaints / deferrals / blocks) into
a single reputation "heat" score and a cool/warm/hot/critical band, then exposes
a send multiplier + safe-mode signal the outreach cadence reads to auto-throttle
*before* the sending domain's reputation craters.

Layout mirrors ``backend/entropy/``:
  * ``metrics``    — HeatInputs + compute_heat_score (pure, weighted, clamped).
  * ``controller`` — band thresholds + send multiplier + posture (pure).
  * ``store``      — daily heat-state counters (DDB + JSON fallback, bucket
                     reset) — also the canonical "emails sent today" counter
                     that no code path tracked before.
  * ``service``    — ingest SendGrid events, record sends, derive the live band.
  * ``sendgrid_signature`` — ECDSA event-webhook signature verification.

Wire-dormant: the THROTTLE actions (cadence multiplier, live-send safe-mode) are
gated behind ``settings.heat_field_enabled`` (default OFF). Event ingestion +
the per-deal halt fan-out always run (they extend the existing SES halt loop to
the actual SendGrid backend) — that is observability + opt-out hygiene, not an
autonomous behavior change.
"""

from __future__ import annotations

__all__: list[str] = []
