"""Pre-deploy infrastructure check.

Walks every dependency the multi-service stack needs to boot. Returns a
structured report; CLI exits 0 on green, 1 on any failure.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Callable

from ..common.aws import dynamodb_resource, sqs_client
from ..common.settings import bootstrap_settings


def _check(name: str, fn: Callable[[], Any]) -> dict[str, Any]:
    try:
        out = fn()
    except Exception as exc:
        return {"name": name, "ok": False, "detail": f"{exc.__class__.__name__}: {exc}"}
    if isinstance(out, bool):
        return {"name": name, "ok": out, "detail": str(out)}
    if out is None:
        return {"name": name, "ok": False, "detail": "None"}
    return {
        "name": name,
        "ok": True,
        "detail": out if isinstance(out, (str, int, float)) else "ok",
    }


def run() -> dict[str, Any]:
    s = bootstrap_settings()
    checks: list[dict[str, Any]] = []

    checks.append(_check("aws_region_set", lambda: s.aws_region))
    checks.append(_check("shared_hmac_key_set", lambda: bool(s.shared_hmac_key)))

    # DDB tables exist + ACTIVE.
    for logical, name in (
        ("task_state", s.ddb_task_state_table),
        ("idempotency", s.ddb_idempotency_table),
        ("suppression", s.ddb_suppression_table),
        ("feedback", s.ddb_feedback_table),
    ):
        checks.append(
            _check(
                f"ddb_table_{logical}",
                lambda n=name: dynamodb_resource().Table(n).table_status,
            )
        )

    # SQS queues exist (if configured).
    for svc, url in s.sqs_queue_urls.items():
        if not url:
            continue
        checks.append(
            _check(
                f"sqs_queue_{svc}",
                lambda u=url: sqs_client().get_queue_attributes(
                    QueueUrl=u,
                    AttributeNames=["QueueArn"],
                )["Attributes"]["QueueArn"],
            )
        )

    all_ok = all(c["ok"] for c in checks)
    return {"all_ok": all_ok, "checks": checks}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args()
    report = run()
    if args.json:
        print(json.dumps(report, default=str, indent=2))
    else:
        for c in report["checks"]:
            mark = "OK" if c["ok"] else "FAIL"
            print(f"  [{mark}] {c['name']:<32} {c['detail']}")
        print()
        print("PREFLIGHT:", "GREEN" if report["all_ok"] else "RED")
    sys.exit(0 if report["all_ok"] else 1)


if __name__ == "__main__":
    main()
