"""ensure_queue_with_dlq — queue + DLQ + redrive policy provisioning."""
from __future__ import annotations

import json

import pytest

from backend.common import sqs


@pytest.fixture
def fake_sqs():
    """A boto3-SQS-shaped fake recording create/get/set calls."""
    class _Fake:
        def __init__(self):
            self.created: list[str] = []
            self.set_calls: list[dict] = []

        def create_queue(self, *, QueueName, Attributes=None):  # noqa: N803
            self.created.append(QueueName)
            return {"QueueUrl": f"https://sqs.us-west-1.amazonaws.com/123456789012/{QueueName}"}

        def get_queue_attributes(self, *, QueueUrl, AttributeNames):  # noqa: N803
            name = QueueUrl.rsplit("/", 1)[-1]
            return {"Attributes": {"QueueArn": f"arn:aws:sqs:us-west-1:123456789012:{name}"}}

        def set_queue_attributes(self, *, QueueUrl, Attributes):  # noqa: N803
            self.set_calls.append({"url": QueueUrl, "attrs": Attributes})

    return _Fake()


def test_creates_dlq_then_main_and_wires_redrive(fake_sqs):
    out = sqs.ensure_queue_with_dlq(
        "samus-cash-engine-jobs", max_receive_count=5, client=fake_sqs,
    )
    # DLQ created BEFORE the main queue (its ARN feeds the redrive policy).
    assert fake_sqs.created == ["samus-cash-engine-jobs-dlq", "samus-cash-engine-jobs"]

    rp = json.loads(out["redrive_policy"])
    assert rp["maxReceiveCount"] == 5
    assert rp["deadLetterTargetArn"].endswith(":samus-cash-engine-jobs-dlq")

    assert out["queue_url"].endswith("/samus-cash-engine-jobs")
    assert out["dlq_url"].endswith("/samus-cash-engine-jobs-dlq")
    assert out["queue_arn"].endswith(":samus-cash-engine-jobs")
    assert out["dlq_arn"].endswith(":samus-cash-engine-jobs-dlq")


def test_main_queue_gets_redrive_and_visibility(fake_sqs):
    sqs.ensure_queue_with_dlq(
        "samus-cash-engine-jobs", visibility_timeout=90, client=fake_sqs,
    )
    # The set_queue_attributes targeting the MAIN queue carries the redrive
    # policy + visibility timeout.
    main_set = [c for c in fake_sqs.set_calls if c["url"].endswith("/samus-cash-engine-jobs")]
    assert len(main_set) == 1
    attrs = main_set[0]["attrs"]
    assert "RedrivePolicy" in attrs
    assert attrs["VisibilityTimeout"] == "90"
    assert attrs["MessageRetentionPeriod"] == str(14 * 24 * 3600)


def test_custom_max_receive_count_is_honored(fake_sqs):
    out = sqs.ensure_queue_with_dlq(
        "samus-cash-engine-jobs", max_receive_count=3, client=fake_sqs,
    )
    assert json.loads(out["redrive_policy"])["maxReceiveCount"] == 3
    assert out["max_receive_count"] == "3"
