"""Callback lookup — DynamoDB-backed prospect index for inbound callbacks.

When Morgan (outbound SDR) ends a call, this module writes a row to
``samus_callback_lookup`` keyed by the prospect's E.164 phone number.
When a prospect calls back on one of the 6 Vapi marketing numbers, the
Vapi Receptionist assistant calls the AWS API Gateway endpoint which
invokes the Lambda that queries this table and returns prospect context.

The write path (index_outbound_call) runs inside the voice workcell on
every outbound end-of-call webhook. It is best-effort and never raises —
a DynamoDB outage degrades to "no callback context" rather than failing
the webhook.

DynamoDB table: samus_callback_lookup (us-west-1)
  PK: phone (S)  — E.164 e.g. +15005550006
"""
from __future__ import annotations

import logging
import os
from decimal import Decimal
from typing import Any

_LOG = logging.getLogger("samus.voice.callback_lookup")


def _attr(name: str):
    from boto3.dynamodb.conditions import Attr
    return Attr(name)

_TABLE_NAME = "samus_callback_lookup"
_TABLE_REGION = "us-west-1"
_table = None


def _get_table():
    global _table
    if _table is None:
        try:
            import boto3
            _table = boto3.resource("dynamodb", region_name=_TABLE_REGION).Table(_TABLE_NAME)
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("callback_lookup: boto3 init failed: %s", exc)
            return None
    return _table


def _normalize_phone(phone: str) -> str:
    digits = "".join(c for c in (phone or "") if c.isdigit())
    if len(digits) == 10:
        return f"+1{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    return phone.strip() if phone.startswith("+") else f"+{digits}"


def index_outbound_call(
    *,
    prospect_phone: str,
    prospect_id: str,
    company_name: str,
    owner_name: str,
    offer_summary: str,
    last_intent_score: int | None,
    outbound_number_id: str,
    last_call_ts: str,
) -> bool:
    """Upsert one outbound call into the callback lookup table.

    Returns True on success, False on any error (caller logs context).
    Intended to be called from the voice webhook handler after an outbound
    end-of-call-report so that when the prospect calls back, the Receptionist
    can retrieve their context.
    """
    phone = _normalize_phone(prospect_phone)
    if not phone or phone in ("+", "+1"):
        _LOG.warning("callback_lookup: skipping index — invalid phone %r", prospect_phone)
        return False

    table = _get_table()
    if table is None:
        return False

    item: dict[str, Any] = {
        "phone": phone,
        "prospect_id": prospect_id or "",
        "company_name": company_name or "",
        "owner_name": owner_name or "",
        "offer_summary": offer_summary or "",
        "outbound_number_id": outbound_number_id or "",
        "last_call_ts": last_call_ts or "",
        "updated_at": last_call_ts or "",
    }
    if last_intent_score is not None:
        item["last_intent_score"] = Decimal(str(last_intent_score))

    try:
        table.put_item(Item=item)
        _LOG.info("callback_lookup: indexed %s -> prospect=%s company=%s",
                  phone, prospect_id, company_name)
        return True
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("callback_lookup: DynamoDB write failed for %s: %s", phone, exc)
        return False


def enrich_after_call(
    *,
    prospect_phone: str,
    ended_reason: str,
    call_summary: str,
) -> bool:
    """Update an existing callback lookup record with post-call data.

    Does a DynamoDB update_item on the record keyed by the normalised
    E.164 phone, adding/overwriting ended_reason, call_summary (capped at
    500 chars), and updated_at. Best-effort — returns True on success,
    False on any error, never raises.
    """
    from backend.common.dates import iso_now

    phone = _normalize_phone(prospect_phone)
    if not phone or phone in ("+", "+1"):
        _LOG.warning("callback_lookup.enrich_after_call: invalid phone %r", prospect_phone)
        return False

    table = _get_table()
    if table is None:
        return False

    try:
        table.update_item(
            Key={"phone": phone},
            UpdateExpression="SET ended_reason = :er, call_summary = :cs, updated_at = :ua",
            ExpressionAttributeValues={
                ":er": ended_reason or "",
                ":cs": (call_summary or "")[:500],
                ":ua": iso_now(),
            },
            ConditionExpression=_attr("phone").exists(),
        )
        _LOG.info("callback_lookup.enrich_after_call: enriched %s", phone)
        return True
    except Exception as exc:  # noqa: BLE001
        from botocore.exceptions import ClientError
        if isinstance(exc, ClientError) and exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            _LOG.debug(
                "callback_lookup.enrich_after_call: item not found for %s "
                "(race with index write), skipping",
                phone,
            )
            return False
        _LOG.warning("callback_lookup.enrich_after_call: update failed for %s: %s", phone, exc)
        return False
