"""Shared suppression-list read helper (email channel).

The suppression list (DynamoDB ``samus_suppression``, PK=``email``) is written
by the feedback workcell on every bounce / complaint / unsubscribe
(``backend.feedback.handlers``) and by the campaign opt-out path. This module
is the canonical *read* side, used anywhere an email is about to be sent or an
inbound reply is being classified, so prior opt-outs are honored fleet-wide.

Fail-OPEN by design: a DynamoDB hiccup returns ``False`` (not suppressed) and
logs a warning rather than blocking a send — availability over a transient
read failure. The authoritative opt-out enforcement remains the
bounce/complaint *halt* loop (``feedback.handlers``); this read is a fast
pre-send courtesy check that catches the common case.

Distinct from the legacy ``backend.common.dynamodb.is_suppressed`` (which
references a stale ``ddb_tables`` settings shape); this mirrors the live
feedback-handler access path: ``aws.table(ddb_suppression_table, aws_region)``.
"""

from __future__ import annotations

import logging

from backend.common import aws
from backend.common.config import get_settings

_LOG = logging.getLogger("samus.common.suppression")


def is_email_suppressed(email: str) -> bool:
    """Return True iff ``email`` is on the suppression list.

    Fail-open: any read / boto bootstrap error returns ``False`` (and logs a
    warning) so a DynamoDB blip never blocks outbound email. Addresses are
    lower-cased + stripped to match the write side.
    """
    addr = (email or "").strip().lower()
    if not addr:
        return False
    try:
        settings = get_settings()
        tbl = aws.table(settings.ddb_suppression_table, settings.aws_region)
        resp = tbl.get_item(Key={"email": addr})
        return "Item" in resp
    except Exception as exc:  # noqa: BLE001 — fail-open read, never block a send
        _LOG.warning("suppression read failed for %r: %s", addr, exc)
        return False


__all__ = ["is_email_suppressed"]
