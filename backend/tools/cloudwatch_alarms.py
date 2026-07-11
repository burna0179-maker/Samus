"""Create CloudWatch alarms on key SQS metrics."""

from __future__ import annotations

import json

from ..common.settings import bootstrap_settings


def run() -> dict:
    s = bootstrap_settings()
    import boto3

    cw = boto3.client("cloudwatch", region_name=s.aws_region)
    results = []
    for service, url in s.sqs_queues.items():
        if not url:
            continue
        queue_name = url.rsplit("/", 1)[-1]
        try:
            cw.put_metric_alarm(
                AlarmName=f"samus-{service}-queue-depth",
                MetricName="ApproximateNumberOfMessagesVisible",
                Namespace="AWS/SQS",
                Statistic="Average",
                Period=300,
                EvaluationPeriods=2,
                Threshold=50.0,
                ComparisonOperator="GreaterThanThreshold",
                Dimensions=[{"Name": "QueueName", "Value": queue_name}],
            )
            results.append({"service": service, "ok": True})
        except Exception as exc:
            results.append({"service": service, "ok": False, "err": str(exc)})
    return {"alarms": results}


if __name__ == "__main__":
    print(json.dumps(run(), default=str, indent=2))
