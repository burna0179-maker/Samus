"""process_sns_notification — orchestrates SES feedback ingestion (doc §9).

Idempotency-cached, audit-stamped pipeline:

  SnsNotification -> (Subscription handshake | parse_sns_message)
                  -> route to record_bounce/record_complaint/record_delivery
                  -> build_audit_event + ledger append -> FeedbackResult.
"""

from __future__ import annotations

import logging
import uuid

from backend.common import events
from backend.common.idempotency import GLOBAL_IDEMPOTENCY_STORE

from . import handlers
from .models import FeedbackResult, SnsNotification

_LOG = logging.getLogger("samus.feedback.service")


def _cache_key(task_id: str) -> str:
    return f"feedback:{task_id}"


def process_sns_notification(
    sns: SnsNotification,
    *,
    task_id: str | None = None,
) -> FeedbackResult:
    """End-to-end SES-via-SNS feedback ingestion."""
    task_id = task_id or f"feedback-{uuid.uuid4().hex[:16]}"
    key = _cache_key(task_id)

    cached = GLOBAL_IDEMPOTENCY_STORE.get(key)
    if cached is not None and isinstance(cached, dict):
        _LOG.info("process_sns_notification cache hit task_id=%s", task_id)
        return FeedbackResult.model_validate(cached)

    # SubscriptionConfirmation never carries SES payloads; surface as no-op.
    if sns.Type == "SubscriptionConfirmation":
        result = FeedbackResult(
            notification_type="SubscriptionConfirmation",
            recipient_count=0,
            suppressed=[],
            event_id=task_id,
        )
        GLOBAL_IDEMPOTENCY_STORE.set(key, result.model_dump())
        audit_event = events.build_audit_event(
            service="feedback",
            task_id=task_id,
            action="SubscriptionConfirmation",
            input_payload=sns.model_dump(),
            output_payload=result.model_dump(),
            status="completed",
            metadata={"subscribe_url_present": bool(sns.SubscribeURL)},
        )
        try:
            handlers._audit_ledger().append(audit_event)
        except OSError as exc:
            _LOG.warning("feedback audit ledger append failed: %s", exc)
        return result

    try:
        ses_notif = handlers.parse_sns_message(sns.Message)
    except Exception as exc:
        _LOG.warning("failed to parse SES message: %s", exc)
        result = FeedbackResult(
            notification_type="unknown",
            recipient_count=0,
            suppressed=[],
            event_id=task_id,
        )
        GLOBAL_IDEMPOTENCY_STORE.set(key, result.model_dump())
        return result

    if ses_notif.notificationType == "Bounce" and ses_notif.bounce is not None:
        result = handlers.record_bounce(ses_notif.bounce, task_id=task_id)
    elif ses_notif.notificationType == "Complaint" and ses_notif.complaint is not None:
        result = handlers.record_complaint(ses_notif.complaint, task_id=task_id)
    elif ses_notif.notificationType == "Delivery" and ses_notif.delivery is not None:
        result = handlers.record_delivery(ses_notif.delivery, task_id=task_id)
    else:
        result = FeedbackResult(
            notification_type="unknown",
            recipient_count=0,
            suppressed=[],
            event_id=task_id,
        )

    audit_event = events.build_audit_event(
        service="feedback",
        task_id=task_id,
        action=ses_notif.notificationType,
        input_payload=sns.model_dump(),
        output_payload=result.model_dump(),
        status="completed",
    )
    try:
        handlers._audit_ledger().append(audit_event)
    except OSError as exc:
        _LOG.warning("feedback audit ledger append failed: %s", exc)

    GLOBAL_IDEMPOTENCY_STORE.set(key, result.model_dump())
    return result
