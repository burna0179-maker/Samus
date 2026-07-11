"""Email -> prospect/opportunity index for bounce/complaint -> deal halt.

The cash engine parks a deal "dormant" after sending outreach and waits for a
signal. A POSITIVE signal (reply/open) arrives id-keyed via the engagement
webhook, but a NEGATIVE deliverability event from SES (bounce/complaint) only
carries the recipient's *email address* — there is no prospect id on it. This
index closes that gap: when the cash engine composes outreach to a prospect it
records ``email -> {prospect_id, opportunity_id}``; when a bounce/complaint
lands, the feedback workcell resolves the address back to the deal and halts
it (never re-engages a dead/hostile address).

Backed by a DynamoDB table (PK=``email``) so the writer (cash-engine-worker)
and the reader (feedback workcell) — separate containers — share one store.
Both operations are fail-soft and the whole thing is a no-op when the table
is unconfigured (empty name), so dev/test without DDB is unaffected. Pass
``tbl`` to inject a fake table in tests.
"""
from __future__ import annotations

import logging
from typing import Any

from .aws import table
from .dates import iso_now
from .settings import get_settings

_LOG = logging.getLogger("samus.recipient_index")


def _norm(email: str) -> str:
    return (email or "").strip().lower()


def _index_table() -> Any | None:
    settings = get_settings()
    name = getattr(settings, "ddb_recipient_index_table", "") or ""
    if not name:
        return None
    return table(name, settings.aws_region)


def record_recipient(
    *,
    email: str,
    prospect_id: str,
    opportunity_id: str = "",
    source: str = "cash_engine",
    tbl: Any = None,
) -> bool:
    """Upsert ``email -> {prospect_id, opportunity_id}``. Best-effort.

    Returns True on a successful write, False on a no-op (missing inputs /
    table unconfigured) or a swallowed write error.
    """
    em = _norm(email)
    pid = (prospect_id or "").strip()
    if not em or not pid:
        return False
    target = tbl if tbl is not None else _index_table()
    if target is None:
        return False
    try:
        target.put_item(Item={
            "email": em,
            "prospect_id": pid,
            "opportunity_id": (opportunity_id or "").strip(),
            "source": source,
            "ts": iso_now(),
        })
        return True
    except Exception as exc:  # noqa: BLE001 — index write never fatal
        _LOG.warning("recipient_index record failed for %s: %s", em, exc)
        return False


def lookup_recipient(email: str, *, tbl: Any = None) -> dict[str, str] | None:
    """Resolve an email to ``{prospect_id, opportunity_id}``, or None.

    None when the address is unknown, the table is unconfigured, or the read
    fails — every one of which means "we can't attribute this bounce", which
    the caller treats as a clean no-op.
    """
    em = _norm(email)
    if not em:
        return None
    target = tbl if tbl is not None else _index_table()
    if target is None:
        return None
    try:
        resp = target.get_item(Key={"email": em})
    except Exception as exc:  # noqa: BLE001 — read never fatal
        _LOG.warning("recipient_index lookup failed for %s: %s", em, exc)
        return None
    item = resp.get("Item")
    if not item:
        return None
    return {
        "prospect_id": str(item.get("prospect_id", "") or ""),
        "opportunity_id": str(item.get("opportunity_id", "") or ""),
    }


__all__ = ["record_recipient", "lookup_recipient"]
