"""AwsRuntime + AwsWorkerSettings — bundles SQS+DynamoDB+SNS for workers.

Per doc §3.24. Used by BaseSqsWorker to keep boto3 client management out of
each workcell. ``AwsWorkerSettings.from_env`` produces the typed config from
env vars; ``AwsRuntime`` owns lazy-init AWS clients + helper methods.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

from . import aws
from .settings import get_settings


@dataclass(frozen=True, slots=True)
class AwsWorkerSettings:
    service_name: str
    queue_url: str
    task_state_table: str
    idempotency_table: str
    event_topic_arn: str
    region: str

    @classmethod
    def from_env(cls, service_name: str, queue_env_var: str) -> "AwsWorkerSettings":
        settings = get_settings()
        return cls(
            service_name=service_name,
            queue_url=os.getenv(queue_env_var, "") or settings.sqs_queue_urls.get(service_name, ""),
            task_state_table=settings.ddb_task_state_table,
            idempotency_table=settings.ddb_idempotency_table,
            event_topic_arn=settings.sns_event_topic_arn,
            region=settings.aws_region,
        )


class AwsRuntime:
    def __init__(self, aws_settings: AwsWorkerSettings) -> None:
        self.settings = aws_settings

    # --- client accessors (lazy via underlying aws module's lru_cache) ----

    @property
    def sqs(self) -> Any:
        return aws.sqs_client(self.settings.region)

    @property
    def sns(self) -> Any:
        return aws.sns_client(self.settings.region)

    @property
    def dynamodb(self) -> Any:
        return aws.dynamodb_resource(self.settings.region)

    def task_state_table(self) -> Any:
        return aws.table(self.settings.task_state_table, self.settings.region)

    def idempotency_table(self) -> Any:
        return aws.table(self.settings.idempotency_table, self.settings.region)

    # --- SQS helpers ------------------------------------------------------

    def receive_messages(
        self,
        max_count: int = 5,
        visibility_timeout: int = 60,
        wait_time: int = 20,
    ) -> list[dict[str, Any]]:
        if not self.settings.queue_url:
            return []
        resp = self.sqs.receive_message(
            QueueUrl=self.settings.queue_url,
            MaxNumberOfMessages=max(1, min(10, max_count)),
            VisibilityTimeout=visibility_timeout,
            WaitTimeSeconds=wait_time,
            MessageAttributeNames=["All"],
            # System attribute — the SQS-tracked redelivery count. BaseSqsWorker
            # reads this to enforce an explicit max-retry ceiling (HOTL T5)
            # rather than relying solely on the queue's redrive policy.
            AttributeNames=["ApproximateReceiveCount"],
        )
        return resp.get("Messages", []) or []

    def delete_message(self, receipt_handle: str) -> None:
        if not self.settings.queue_url:
            return
        self.sqs.delete_message(
            QueueUrl=self.settings.queue_url,
            ReceiptHandle=receipt_handle,
        )

    def change_visibility(self, receipt_handle: str, seconds: int) -> None:
        if not self.settings.queue_url:
            return
        self.sqs.change_message_visibility(
            QueueUrl=self.settings.queue_url,
            ReceiptHandle=receipt_handle,
            VisibilityTimeout=seconds,
        )

    # --- SNS event publish ------------------------------------------------

    def publish_event(self, event_type: str, payload: dict[str, Any]) -> None:
        if not self.settings.event_topic_arn:
            return
        body = json.dumps({"type": event_type, "payload": payload}, default=str)
        self.sns.publish(
            TopicArn=self.settings.event_topic_arn,
            Message=body,
            MessageAttributes={
                "event_type": {"DataType": "String", "StringValue": event_type},
                "service": {"DataType": "String", "StringValue": self.settings.service_name},
            },
        )
