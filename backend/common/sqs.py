"""SQS wrappers used by the gateway dispatcher and worker poll loops.

Per doc §3.21. Queue URL lookup goes through ``settings.sqs_queue_urls``.
Receive defaults: max_messages=5, visibility_timeout=60, wait_time=20.
"""

from __future__ import annotations

import json
import logging
from typing import Any

try:
    from botocore.exceptions import ClientError
except ImportError:

    class ClientError(Exception):  # type: ignore[no-redef]
        """Stub so module imports succeed without boto3."""

        response: dict = {}


from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential_jitter

from .aws import sqs_client
from .config import get_settings

_LOG = logging.getLogger("samus.sqs")

_DEFAULT_MAX_MESSAGES = 5
_DEFAULT_WAIT_SECONDS = 20
_DEFAULT_VISIBILITY = 60


class QueueNotConfigured(RuntimeError):
    pass


def queue_url(service: str) -> str:
    url = get_settings().sqs_queue_urls.get(service)
    if not url:
        raise QueueNotConfigured(f"No SQS queue URL configured for service '{service}'")
    return url


@retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential_jitter(initial=0.5, max=4),
    retry=retry_if_exception_type(ClientError),
)
def send(service: str, body: str, attributes: dict[str, Any] | None = None) -> str:
    """Send a message and return its SQS message id. Retries 3× on transient ClientError."""
    client = sqs_client()
    msg_attrs: dict[str, Any] = {}
    if attributes:
        for k, v in attributes.items():
            if v is None:
                continue
            msg_attrs[k] = {"DataType": "String", "StringValue": str(v)}
    kwargs: dict[str, Any] = {"QueueUrl": queue_url(service), "MessageBody": body}
    if msg_attrs:
        kwargs["MessageAttributes"] = msg_attrs
    resp = client.send_message(**kwargs)
    return resp["MessageId"]


def receive(
    service: str,
    *,
    max_messages: int = _DEFAULT_MAX_MESSAGES,
    wait_seconds: int = _DEFAULT_WAIT_SECONDS,
    visibility: int = _DEFAULT_VISIBILITY,
) -> list[dict[str, Any]]:
    """Long-poll the queue. Returns the raw SQS message list (possibly empty)."""
    client = sqs_client()
    resp = client.receive_message(
        QueueUrl=queue_url(service),
        MaxNumberOfMessages=max(1, min(10, max_messages)),
        WaitTimeSeconds=wait_seconds,
        VisibilityTimeout=visibility,
        MessageAttributeNames=["All"],
        AttributeNames=["All"],
    )
    return resp.get("Messages", [])


def delete(service: str, receipt_handle: str) -> None:
    sqs_client().delete_message(QueueUrl=queue_url(service), ReceiptHandle=receipt_handle)


def change_visibility(service: str, receipt_handle: str, seconds: int) -> None:
    sqs_client().change_message_visibility(
        QueueUrl=queue_url(service),
        ReceiptHandle=receipt_handle,
        VisibilityTimeout=seconds,
    )


def queue_depth(service: str) -> int:
    resp = sqs_client().get_queue_attributes(
        QueueUrl=queue_url(service),
        AttributeNames=["ApproximateNumberOfMessages"],
    )
    return int(resp.get("Attributes", {}).get("ApproximateNumberOfMessages", 0))


def parse_body(sqs_message: dict[str, Any]) -> dict[str, Any]:
    """Extract the JSON payload from an SQS message wrapper."""
    body = sqs_message.get("Body", "")
    if not body:
        return {}
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return {}


# SQS message retention max — 14 days, in seconds.
_MAX_RETENTION_SECONDS = 14 * 24 * 3600


def ensure_queue_with_dlq(
    queue_name: str,
    *,
    region: str | None = None,
    max_receive_count: int = 5,
    visibility_timeout: int = 60,
    message_retention_seconds: int = _MAX_RETENTION_SECONDS,
    client: Any = None,
) -> dict[str, str]:
    """Idempotently create ``queue_name`` + ``{queue_name}-dlq`` and wire the
    main queue's RedrivePolicy to the DLQ.

    Uses ``create_queue`` (idempotent on the name) followed by
    ``set_queue_attributes`` (an idempotent overwrite), so re-running this
    reconciles the attributes rather than erroring on an existing queue — the
    DLQ is created first because the main queue's RedrivePolicy needs its ARN.
    ``client`` is injectable for tests; in production it resolves to the
    region-bound boto3 SQS client.

    Returns the URLs, ARNs, and the applied redrive policy JSON.
    """
    cli = client or sqs_client(region)
    dlq_name = f"{queue_name}-dlq"

    # 1) DLQ first — its ARN feeds the main queue's RedrivePolicy.
    dlq = cli.create_queue(
        QueueName=dlq_name,
        Attributes={"MessageRetentionPeriod": str(message_retention_seconds)},
    )["QueueUrl"]
    dlq_arn = cli.get_queue_attributes(
        QueueUrl=dlq,
        AttributeNames=["QueueArn"],
    )["Attributes"]["QueueArn"]

    # 2) Main queue, then set its redrive policy + dispatch-shaped attributes.
    redrive_policy = json.dumps(
        {
            "deadLetterTargetArn": dlq_arn,
            "maxReceiveCount": int(max_receive_count),
        }
    )
    main = cli.create_queue(QueueName=queue_name)["QueueUrl"]
    cli.set_queue_attributes(
        QueueUrl=main,
        Attributes={
            "RedrivePolicy": redrive_policy,
            "VisibilityTimeout": str(visibility_timeout),
            "MessageRetentionPeriod": str(message_retention_seconds),
        },
    )
    main_arn = cli.get_queue_attributes(
        QueueUrl=main,
        AttributeNames=["QueueArn"],
    )["Attributes"]["QueueArn"]

    _LOG.info(
        "ensure_queue_with_dlq: queue=%s dlq=%s maxReceiveCount=%s",
        queue_name,
        dlq_name,
        max_receive_count,
    )
    return {
        "queue_name": queue_name,
        "queue_url": main,
        "queue_arn": main_arn,
        "dlq_name": dlq_name,
        "dlq_url": dlq,
        "dlq_arn": dlq_arn,
        "redrive_policy": redrive_policy,
        "max_receive_count": str(max_receive_count),
    }


def dlq_url(service: str) -> str | None:
    """Resolve the DLQ url for a service by reading the queue's RedrivePolicy."""
    resp = sqs_client().get_queue_attributes(
        QueueUrl=queue_url(service),
        AttributeNames=["RedrivePolicy"],
    )
    redrive = resp.get("Attributes", {}).get("RedrivePolicy")
    if not redrive:
        return None
    try:
        arn = json.loads(redrive).get("deadLetterTargetArn")
    except json.JSONDecodeError:
        return None
    if not arn:
        return None
    name = arn.split(":")[-1]
    main = queue_url(service)
    return main.rsplit("/", 1)[0] + "/" + name
