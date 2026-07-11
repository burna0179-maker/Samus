"""Thin DDB persistence layer for the CRM workcell.

Pattern mirrors :mod:`backend.intake.service`: one ``*_table()`` accessor
per entity (lazy table lookup), wrapped ``put_item`` that never raises (so
boto / IAM hiccups don't lose a write — caller still gets a degraded
result), and ``get_item`` / ``scan`` helpers that return typed Pydantic
rows where useful.

Read endpoints use full-table ``scan`` with attribute filters in Phase 1.
At current volume that's fine; GSIs (by prospect_id, by stage, by status)
are deferred — see Phase 2+ notes in :mod:`backend.crm.__init__`.
"""
from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

from backend.common import aws
from backend.common.config import get_settings


_LOG = logging.getLogger("samus.crm.persistence")


# ---------------------------------------------------------------------------
# Table accessors
# ---------------------------------------------------------------------------

def _prospects_table() -> Any:
    s = get_settings()
    return aws.table(s.ddb_prospects_table, s.aws_region)


def _contacts_table() -> Any:
    s = get_settings()
    return aws.table(s.ddb_contacts_table, s.aws_region)


def _conversations_table() -> Any:
    s = get_settings()
    return aws.table(s.ddb_conversations_table, s.aws_region)


def _call_state_table() -> Any:
    s = get_settings()
    return aws.table(s.ddb_call_state_table, s.aws_region)


def _opportunities_table() -> Any:
    s = get_settings()
    return aws.table(s.ddb_opportunities_table, s.aws_region)


def _operator_tasks_table() -> Any:
    s = get_settings()
    return aws.table(s.ddb_operator_tasks_table, s.aws_region)


def _artifacts_table() -> Any:
    s = get_settings()
    return aws.table(s.ddb_artifacts_table, s.aws_region)


def _onboarding_leads_table() -> Any:
    """Read-only access to intake's table for the lead-conversion flow."""
    s = get_settings()
    return aws.table(s.ddb_onboarding_leads_table, s.aws_region)


# ---------------------------------------------------------------------------
# Generic helpers (kept tiny — every workcell has its own variant of these)
# ---------------------------------------------------------------------------

def _coerce_floats(value: Any) -> Any:
    """Recursively convert floats to Decimal for a DynamoDB put.

    boto3's DynamoDB resource rejects native Python floats outright — even
    ``0.0``. Opportunity rows carry float fields (``deal_size_usd``,
    ``close_probability``, ``won_amount_usd``), so every ``safe_put`` item
    must pass through this first. Mirrors the helper in
    :mod:`backend.common.dynamodb`; kept local per this module's
    "every workcell has its own variant" convention.
    """
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: _coerce_floats(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_coerce_floats(v) for v in value]
    return value


def safe_put(table: Any, item: dict[str, Any]) -> tuple[bool, str | None]:
    """Wrap put_item so AWS misconfig doesn't drop the row on the floor.

    The item is run through :func:`_coerce_floats` first — DynamoDB rejects
    native Python floats, and Opportunity rows carry several.

    Returns (persisted, error_string). Caller decides what to do with the
    error; the audit ledger upstream should always still capture the intent.
    """
    try:
        table.put_item(Item=_coerce_floats(item))
        return True, None
    except Exception as exc:  # noqa: BLE001 - degraded path
        _LOG.warning("crm put_item failed: %s", exc)
        return False, f"ddb_put_failed: {exc}"


def safe_get(table: Any, key: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    """Wrap get_item so a missing row returns (None, None) and AWS errors
    return (None, error_string). Caller distinguishes 404 from 500 via the
    error field.
    """
    try:
        resp = table.get_item(Key=key)
    except Exception as exc:  # noqa: BLE001 - degraded path
        _LOG.warning("crm get_item failed: %s", exc)
        return None, f"ddb_get_failed: {exc}"
    return resp.get("Item"), None


# A filtered Scan applies its FilterExpression *after* DynamoDB has read a
# page of items, and `Limit` caps that read window — not the number of
# matches. So `Limit=N` + a filter can return zero rows even when a matching
# row sits just past the Nth item scanned. To honour `limit` as a *result*
# count, the filtered path in safe_scan pages through the table with
# ExclusiveStartKey until it has enough matches or the table is exhausted.
# _SCAN_PAGE_CAP bounds that loop so a sparse filter over a large table can't
# scan unboundedly — Phase-1 table volumes stay well inside it.
_SCAN_PAGE_CAP = 20


def safe_scan(
    table: Any, *, limit: int = 50,
    filter_expression: Any = None,
    expression_attribute_values: dict[str, Any] | None = None,
    expression_attribute_names: dict[str, str] | None = None,
) -> tuple[list[dict[str, Any]], bool, str | None]:
    """Wrap scan so a misconfig returns ([], False, error).

    ``limit`` is the number of *result* rows the caller wants. With no filter
    that maps straight onto DynamoDB's ``Limit``. With a filter it cannot
    (see the note above ``_SCAN_PAGE_CAP``), so the filtered path pages
    through the table until it has ``limit`` matches or runs out. The 2nd
    tuple element is True when matching rows may remain beyond what was
    returned (caller can surface that as ``scan_truncated``).
    """
    want = max(1, min(500, int(limit)))

    if filter_expression is None:
        # No filter: DynamoDB's Limit is exactly the result count.
        try:
            resp = table.scan(Limit=want)
        except Exception as exc:  # noqa: BLE001 - degraded path
            _LOG.warning("crm scan failed: %s", exc)
            return [], False, f"ddb_scan_failed: {exc}"
        return resp.get("Items", []) or [], "LastEvaluatedKey" in resp, None

    base_kwargs: dict[str, Any] = {"FilterExpression": filter_expression}
    if expression_attribute_values:
        base_kwargs["ExpressionAttributeValues"] = expression_attribute_values
    if expression_attribute_names:
        base_kwargs["ExpressionAttributeNames"] = expression_attribute_names

    collected: list[dict[str, Any]] = []
    start_key: dict[str, Any] | None = None
    for _ in range(_SCAN_PAGE_CAP):
        kwargs = dict(base_kwargs)
        if start_key is not None:
            kwargs["ExclusiveStartKey"] = start_key
        try:
            resp = table.scan(**kwargs)
        except Exception as exc:  # noqa: BLE001 - degraded path
            _LOG.warning("crm scan failed: %s", exc)
            return [], False, f"ddb_scan_failed: {exc}"
        collected.extend(resp.get("Items", []) or [])
        start_key = resp.get("LastEvaluatedKey")
        if len(collected) >= want or start_key is None:
            break

    # Truncated when matching rows may remain: either the table still has
    # unscanned pages, or this page-set produced more matches than wanted.
    truncated = start_key is not None or len(collected) > want
    return collected[:want], truncated, None


def upsert_prospect_record(record: Any) -> bool:
    """Write a ProspectRecord to samus_prospects.  Returns True if persisted.

    Called by the in-container prospecting cadence (persist_prospects=True)
    so newly discovered prospects flow into the auto-stake sweep pool.  Accepts
    Any to avoid a circular import with backend.prospecting.models.
    Best-effort — never raises.
    """
    try:
        pid = getattr(record, "prospect_id", None) or ""
        if not pid:
            return False
        # Pydantic v2 model_dump; fall back to __dict__ for plain dicts.
        item: dict[str, Any] = (
            record.model_dump(exclude_none=True)
            if hasattr(record, "model_dump")
            else {k: v for k, v in vars(record).items() if v is not None}
        )
        # DDB rejects None and empty strings as attribute values.
        item = {k: v for k, v in item.items() if v not in (None, "")}
        ok, _err = safe_put(_prospects_table(), item)
        return ok
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("upsert_prospect_record failed: %s", exc)
        return False
