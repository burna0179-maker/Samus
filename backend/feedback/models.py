"""Pydantic models for SES SNS feedback payloads (doc §9).

The outer SNS envelope (:class:`SnsNotification`) carries either a
``SubscriptionConfirmation`` handshake or a ``Notification`` whose ``Message``
field is a JSON-encoded SES event. SES events are typed via
:class:`SesNotification` with one of three payload variants attached.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class SnsNotification(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    Type: str
    MessageId: str | None = None
    TopicArn: str | None = None
    Message: str = ""
    Timestamp: str | None = None
    Token: str | None = None
    SubscribeURL: str | None = None


class SesBounceRecipient(BaseModel):
    model_config = ConfigDict(extra="allow")

    emailAddress: str
    action: str | None = None
    status: str | None = None
    diagnosticCode: str | None = None


class SesBouncePayload(BaseModel):
    model_config = ConfigDict(extra="allow")

    bounceType: str
    bounceSubType: str | None = None
    bouncedRecipients: list[SesBounceRecipient] = Field(default_factory=list)
    timestamp: str | None = None


class SesComplaintRecipient(BaseModel):
    model_config = ConfigDict(extra="allow")

    emailAddress: str


class SesComplaintPayload(BaseModel):
    model_config = ConfigDict(extra="allow")

    complaintFeedbackType: str | None = None
    complainedRecipients: list[SesComplaintRecipient] = Field(default_factory=list)
    timestamp: str | None = None


class SesDeliveryPayload(BaseModel):
    model_config = ConfigDict(extra="allow")

    timestamp: str | None = None
    recipients: list[str] = Field(default_factory=list)
    processingTimeMillis: int | None = None


class SesNotification(BaseModel):
    model_config = ConfigDict(extra="allow")

    notificationType: Literal["Bounce", "Complaint", "Delivery"]
    mail: dict[str, Any] = Field(default_factory=dict)
    bounce: SesBouncePayload | None = None
    complaint: SesComplaintPayload | None = None
    delivery: SesDeliveryPayload | None = None


class FeedbackResult(BaseModel):
    notification_type: str
    recipient_count: int
    suppressed: list[str] = Field(default_factory=list)
    event_id: str
