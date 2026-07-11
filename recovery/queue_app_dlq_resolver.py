#!/usr/bin/env python3
"""
DLQ Resolver — RedrivePolicy-derived DLQ resolution (not name-suffix inference)
Source: ChatGPT recovery chat 11 (gateway /queues + /dlq/{service} fix)

Canonical relationship:
- [EXPANDS §6 orchestration + observability] drift-proof DLQ resolution
- [FIX] replaces brittle main_queue_name + "-dlq" inference with AWS-truth lookup
- Reference impl naming drift: actual DLQs are `samus-<service>-dlq` not `samus-<service>-jobs-dlq`
"""

from __future__ import annotations

import json
from typing import Any, Dict, Tuple


def parse_queue_name_from_arn(queue_arn: str) -> str:
    return queue_arn.split(":")[-1]


def queue_url_from_arn(queue_arn: str) -> str:
    parts = queue_arn.split(":")
    region, account_id, queue_name = parts[3], parts[4], parts[5]
    return f"https://sqs.{region}.amazonaws.com/{account_id}/{queue_name}"


def resolve_dlq_url_for_service(sqs_client, main_queue_url: str) -> Tuple[str, int]:
    """Single source of truth — used by /queues, /dlq/{service}, replay tooling."""
    attrs = sqs_client.get_queue_attributes(
        QueueUrl=main_queue_url, AttributeNames=["RedrivePolicy"],
    )["Attributes"]
    if "RedrivePolicy" not in attrs:
        raise RuntimeError(f"Queue missing RedrivePolicy: {main_queue_url}")
    redrive = json.loads(attrs["RedrivePolicy"])
    return queue_url_from_arn(redrive["deadLetterTargetArn"]), int(redrive["maxReceiveCount"])


def get_queue_depths(sqs_client, queue_url: str) -> Dict[str, int]:
    attrs = sqs_client.get_queue_attributes(
        QueueUrl=queue_url,
        AttributeNames=[
            "ApproximateNumberOfMessages",
            "ApproximateNumberOfMessagesNotVisible",
            "ApproximateNumberOfMessagesDelayed",
        ],
    )["Attributes"]
    return {
        "messages_available": int(attrs.get("ApproximateNumberOfMessages", "0")),
        "messages_in_flight": int(attrs.get("ApproximateNumberOfMessagesNotVisible", "0")),
        "messages_delayed": int(attrs.get("ApproximateNumberOfMessagesDelayed", "0")),
    }


def inspect_queue_pair(sqs_client, service: str, main_queue_url: str) -> Dict[str, Any]:
    main_attrs = sqs_client.get_queue_attributes(
        QueueUrl=main_queue_url,
        AttributeNames=[
            "ApproximateNumberOfMessages",
            "ApproximateNumberOfMessagesNotVisible",
            "ApproximateNumberOfMessagesDelayed",
            "RedrivePolicy",
        ],
    )["Attributes"]

    result: Dict[str, Any] = {
        "service": service,
        "main": {
            "status": "ok",
            "queue_url": main_queue_url,
            "messages_available": int(main_attrs.get("ApproximateNumberOfMessages", "0")),
            "messages_in_flight": int(main_attrs.get("ApproximateNumberOfMessagesNotVisible", "0")),
            "messages_delayed": int(main_attrs.get("ApproximateNumberOfMessagesDelayed", "0")),
        },
    }

    redrive_raw = main_attrs.get("RedrivePolicy")
    if not redrive_raw:
        result["dlq"] = {"status": "missing_redrive_policy"}
        return result

    redrive = json.loads(redrive_raw)
    dlq_arn = redrive["deadLetterTargetArn"]
    dlq_url = queue_url_from_arn(dlq_arn)

    try:
        depths = get_queue_depths(sqs_client, dlq_url)
        result["dlq"] = {
            "status": "ok",
            "queue_url": dlq_url,
            "queue_arn": dlq_arn,
            "queue_name": parse_queue_name_from_arn(dlq_arn),
            "max_receive_count": int(redrive.get("maxReceiveCount", 0)),
            **depths,
        }
    except Exception as exc:
        result["dlq"] = {"status": "error", "queue_url": dlq_url, "queue_arn": dlq_arn, "reason": str(exc)}
    return result


def validate_queue_wiring_at_startup(sqs_client, queue_url: str) -> None:
    """Fail-closed startup check: every configured main queue must have a RedrivePolicy."""
    attrs = sqs_client.get_queue_attributes(QueueUrl=queue_url, AttributeNames=["RedrivePolicy"])["Attributes"]
    if "RedrivePolicy" not in attrs:
        raise RuntimeError(f"Queue missing RedrivePolicy: {queue_url}")
