"""boto3 client / resource factories.

The factories cache by region so a long-running worker reuses a single client.
Each call asserts AWS_REGION is set; failures surface at boot rather than
when the first message arrives.

boto3 / botocore are imported lazily so that modules which transitively
depend on this file (suppression -> compliance_guard -> email_backend -> …)
can be imported in environments where boto3 is not installed (host dev venv,
Cloud Run containers that use GCP services instead of AWS).
"""
from __future__ import annotations

from functools import lru_cache
from typing import Any

from .settings import get_settings


@lru_cache(maxsize=1)
def _retry_config() -> Any:
    from botocore.config import Config
    return Config(retries={"max_attempts": 5, "mode": "standard"})


@lru_cache(maxsize=8)
def sqs_client(region: str | None = None) -> Any:
    import boto3
    region = region or get_settings().aws_region
    return boto3.client("sqs", region_name=region, config=_retry_config())


@lru_cache(maxsize=8)
def sns_client(region: str | None = None) -> Any:
    import boto3
    region = region or get_settings().aws_region
    return boto3.client("sns", region_name=region, config=_retry_config())


@lru_cache(maxsize=8)
def ses_client(region: str | None = None) -> Any:
    import boto3
    region = region or get_settings().aws_region
    return boto3.client("ses", region_name=region, config=_retry_config())


@lru_cache(maxsize=8)
def dynamodb_resource(region: str | None = None) -> Any:
    import boto3
    region = region or get_settings().aws_region
    return boto3.resource("dynamodb", region_name=region, config=_retry_config())


def table(name: str, region: str | None = None) -> Any:
    return dynamodb_resource(region).Table(name)
