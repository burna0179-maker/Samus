"""Shared task-state persistence for the ``samus_task_state`` DynamoDB table.

Wraps ``AwsRuntime.task_state_table().put_item`` with a float-to-Decimal
coercion pass — DynamoDB rejects native Python floats, and the autonomous
workcells persist float fields (``efficiency_ema``, ``entropy_score``,
``llm_cost``). Fail-soft: a persistence error is logged and swallowed.
Task-state lineage is observability, never on the request critical path.

``backend/common/dynamodb.py`` is broken legacy (it references a non-existent
``Settings.ddb_tables`` attribute) — this module is the working path.
"""
from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

from .aws_runtime import AwsRuntime, AwsWorkerSettings
from .config import get_settings
from .dates import iso_now

_LOG = logging.getLogger("samus.task_state")


def _coerce(value: Any) -> Any:
    """Recursively convert floats to ``Decimal`` so DynamoDB accepts the item.

    ``bool`` is checked before ``float`` because DynamoDB stores booleans
    natively and ``isinstance(True, int)`` would otherwise be ambiguous.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: _coerce(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_coerce(v) for v in value]
    return value


def write_task_state(
    *,
    task_id: str,
    service: str,
    status: str = "completed",
    **fields: Any,
) -> bool:
    """Persist one row to the ``samus_task_state`` table.

    ``task_id`` is the partition key. ``service`` names the writing workcell
    (also used to build the AWS runtime). Arbitrary ``**fields`` — e.g.
    ``capability``, ``decision_path``, ``efficiency_ema``, ``entropy_score``,
    ``llm_cost``, ``fallback_triggered`` — are written verbatim after the
    float-to-Decimal coercion.

    Returns ``True`` on a successful write, ``False`` when persistence is
    disabled (empty ``DDB_TASK_STATE_TABLE``) or the write fails. Never raises.
    """
    table_name = get_settings().ddb_task_state_table
    if not table_name:
        return False

    item: dict[str, Any] = {
        "task_id": task_id,
        "service": service,
        "status": status,
        "ts": iso_now(),
    }
    item.update(fields)

    try:
        runtime = AwsRuntime(
            AwsWorkerSettings.from_env(service, f"SQS_{service.upper()}_QUEUE_URL")
        )
        runtime.task_state_table().put_item(Item=_coerce(item))
        return True
    except Exception as exc:  # noqa: BLE001 — fail-soft on any persistence error
        _LOG.warning("task_state write failed for %s/%s: %s", service, task_id, exc)
        return False


__all__ = ["write_task_state"]
