"""AWS SES email adapter for the outreach workcell."""
from __future__ import annotations

import logging
from typing import Any

# boto3 / botocore are optional at IMPORT time (a Cloud Run image that
# only uses GCP services doesn't ship them); kept as module-level
# attributes so tests can monkeypatch ``ses_adapter.boto3.client``
# against a stub without re-importing.
try:
    import boto3  # type: ignore[import-untyped]
except ImportError:
    class _Boto3Stub:
        """Stand-in for the boto3 module when the package isn't installed.

        Exists so ``ses_adapter.boto3.client`` is a stable attribute path
        — tests monkeypatch ``client`` against a fake, production raises
        cleanly via :class:`SesAdapterError` when ``client`` is still
        ``None`` at call time.
        """
        client = None  # type: ignore[assignment]

    boto3 = _Boto3Stub()  # type: ignore[assignment]

try:
    from botocore.exceptions import ClientError
except ImportError:
    class ClientError(Exception):  # type: ignore[no-redef]
        response: dict = {}

from backend.common.config import get_settings
from backend.common.dates import iso_now

_LOG = logging.getLogger("samus.outreach.ses_adapter")


class SesAdapterError(Exception):
    """Raised when the SES API call fails — wraps botocore ClientError."""


def send_email_via_ses(
    to: str,
    subject: str,
    body: str,
    *,
    from_addr: str | None = None,
    aws_region: str | None = None,
) -> dict[str, str]:
    """Send a plain-text email via AWS SES. Returns delivery receipt dict."""
    settings = get_settings()
    source = from_addr if from_addr is not None else settings.ses_from_email
    if not (source and source.strip()):
        raise ValueError(
            "send_email_via_ses requires from_addr (arg or settings.ses_from_email)"
        )
    region = aws_region or settings.aws_region
    if boto3.client is None:
        raise SesAdapterError(
            "boto3 is not installed; install boto3 or stub send_email_via_ses for this deploy"
        )
    client: Any = boto3.client("ses", region_name=region)
    try:
        resp = client.send_email(
            Source=source,
            Destination={"ToAddresses": [to]},
            Message={
                "Subject": {"Data": subject},
                "Body": {"Text": {"Data": body}},
            },
        )
    except ClientError as exc:
        _LOG.warning("SES send_email failed: %s", exc)
        raise SesAdapterError(str(exc)) from exc
    message_id = str(resp.get("MessageId", ""))
    return {
        "message_id": message_id,
        "channel": "email",
        "to": to,
        "ts": iso_now(),
    }
