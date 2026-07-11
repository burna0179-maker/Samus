"""DynamoDB helpers: task state, suppression, basic CRUD wrappers."""

from __future__ import annotations

import time
from decimal import Decimal
from typing import Any

try:
    from botocore.exceptions import ClientError
except ImportError:

    class ClientError(Exception):  # type: ignore[no-redef]
        """Stub so module imports succeed without boto3."""

        def __init__(self, error_response=None, operation_name=None):
            self.response = error_response or {}
            super().__init__(str(error_response))


from .aws import dynamodb_resource, table
from .settings import get_settings


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _coerce(value: Any) -> Any:
    """Walk a value and convert floats â Decimal so DynamoDB accepts them."""
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: _coerce(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_coerce(v) for v in value]
    if isinstance(value, tuple):
        return [_coerce(v) for v in value]
    return value


# --- Task state ---------------------------------------------------------
def put_task_state(task_id: str, status: str, **fields: Any) -> None:
    tbl = table(get_settings().ddb_tables["task_state"])
    item = {"task_id": task_id, "status": status, "updated_at": _now(), **fields}
    tbl.put_item(Item=_coerce(item))


def get_task_state(task_id: str) -> dict[str, Any] | None:
    tbl = table(get_settings().ddb_tables["task_state"])
    resp = tbl.get_item(Key={"task_id": task_id})
    return resp.get("Item")


def update_task_state(task_id: str, **fields: Any) -> None:
    if not fields:
        return
    tbl = table(get_settings().ddb_tables["task_state"])
    expr_parts: list[str] = []
    values: dict[str, Any] = {}
    names: dict[str, str] = {}
    for i, (k, v) in enumerate(fields.items()):
        nk = f"#k{i}"
        nv = f":v{i}"
        names[nk] = k
        values[nv] = _coerce(v)
        expr_parts.append(f"{nk} = {nv}")
    expr_parts.append("#updated = :updated")
    names["#updated"] = "updated_at"
    values[":updated"] = _now()
    tbl.update_item(
        Key={"task_id": task_id},
        UpdateExpression="SET " + ", ".join(expr_parts),
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
    )


# --- Suppression list ---------------------------------------------------
def is_suppressed(email: str) -> bool:
    tbl = table(get_settings().ddb_tables["suppression"])
    resp = tbl.get_item(Key={"email": email.lower().strip()})
    return "Item" in resp


def suppress(email: str, reason: str = "manual") -> None:
    tbl = table(get_settings().ddb_tables["suppression"])
    tbl.put_item(Item={"email": email.lower().strip(), "reason": reason, "ts": _now()})


# --- Idempotency store -------------------------------------------------
_IDEMPOTENCY_TTL_SECONDS = 86_400  # 24 hours


def idempotency_claim(workcell: str, key: str) -> bool:
    """Attempt to claim an idempotency key atomically.

    Returns True if this process is the first to claim it (caller should
    process the request). Returns False if another request already claimed it
    (caller should fetch the stored response via ``idempotency_get``).

    Uses a DynamoDB conditional write so concurrent duplicate requests cannot
    both claim the key.
    """
    tbl = table(get_settings().ddb_idempotency_table)
    pk = f"{workcell}:{key}"
    expires_at = int(time.time()) + _IDEMPOTENCY_TTL_SECONDS
    try:
        tbl.put_item(
            Item={
                "pk": pk,
                "status": "claimed",
                "expires_at": expires_at,
                "created_at": _now(),
            },
            ConditionExpression="attribute_not_exists(pk)",
        )
        return True
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise


def idempotency_complete(
    workcell: str,
    key: str,
    status_code: int,
    body: str,
    content_type: str,
) -> None:
    """Store the completed response so future duplicates get the cached reply."""
    tbl = table(get_settings().ddb_idempotency_table)
    pk = f"{workcell}:{key}"
    expires_at = int(time.time()) + _IDEMPOTENCY_TTL_SECONDS
    tbl.update_item(
        Key={"pk": pk},
        UpdateExpression=(
            "SET #s = :s, response_status = :rs, response_body = :rb, "
            "content_type = :ct, expires_at = :exp, completed_at = :ca"
        ),
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={
            ":s": "completed",
            ":rs": status_code,
            ":rb": body,
            ":ct": content_type,
            ":exp": expires_at,
            ":ca": _now(),
        },
    )


def idempotency_get(workcell: str, key: str) -> dict[str, Any] | None:
    """Fetch a claimed/completed idempotency record. Returns None if not found."""
    tbl = table(get_settings().ddb_idempotency_table)
    resp = tbl.get_item(Key={"pk": f"{workcell}:{key}"})
    return resp.get("Item")


# --- Generic helpers ---------------------------------------------------
def safe_put(table_key: str, item: dict[str, Any]) -> bool:
    """Put item into the named logical table (key into ``ddb_tables``). Returns True on success."""
    try:
        table(get_settings().ddb_tables[table_key]).put_item(Item=_coerce(item))
        return True
    except ClientError:
        return False


def safe_get(table_key: str, key: dict[str, Any]) -> dict[str, Any] | None:
    try:
        resp = table(get_settings().ddb_tables[table_key]).get_item(Key=key)
        return resp.get("Item")
    except ClientError:
        return None


# --- Provisioning -------------------------------------------------------
def ensure_table(
    name: str,
    *,
    partition_key: str = "email",
    region: str | None = None,
    billing_mode: str = "PAY_PER_REQUEST",
    resource: Any = None,
) -> dict[str, Any]:
    """Idempotently create a single-key on-demand DynamoDB table.

    Returns ``{"table", "created": bool, "status"}``. A pre-existing table
    (ResourceInUseException) is reported as ``created=False`` rather than
    raising, so re-running is safe. ``resource`` is injectable for tests.
    """
    res = resource if resource is not None else dynamodb_resource(region)
    try:
        tbl = res.create_table(
            TableName=name,
            KeySchema=[{"AttributeName": partition_key, "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": partition_key, "AttributeType": "S"}],
            BillingMode=billing_mode,
        )
        # Block until the table is ACTIVE so a caller can use it immediately.
        waiter = getattr(tbl, "wait_until_exists", None)
        if callable(waiter):
            waiter()
        return {"table": name, "created": True, "status": "created", "partition_key": partition_key}
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ResourceInUseException":
            return {
                "table": name,
                "created": False,
                "status": "exists",
                "partition_key": partition_key,
            }
        raise
