"""Direct tests for backend.feedback.handlers (no FastAPI)."""

from __future__ import annotations

import json
from typing import Any


class _FakeTable:
    def __init__(self) -> None:
        self.puts: list[dict[str, Any]] = []

    def put_item(self, *, Item: dict[str, Any]) -> None:
        self.puts.append(Item)


def _install_fake_tables(monkeypatch) -> tuple[_FakeTable, _FakeTable]:
    suppression = _FakeTable()
    feedback_events = _FakeTable()
    import backend.feedback.handlers as handlers_mod

    monkeypatch.setattr(handlers_mod, "_suppression_table", lambda: suppression)
    monkeypatch.setattr(handlers_mod, "_feedback_events_table", lambda: feedback_events)
    return suppression, feedback_events


def test_parse_sns_message_bounce():
    from backend.feedback.handlers import parse_sns_message

    raw = json.dumps(
        {
            "notificationType": "Bounce",
            "mail": {"messageId": "id"},
            "bounce": {
                "bounceType": "Permanent",
                "bounceSubType": "General",
                "bouncedRecipients": [{"emailAddress": "x@example.com"}],
            },
        }
    )
    notif = parse_sns_message(raw)
    assert notif.notificationType == "Bounce"
    assert notif.bounce is not None
    assert notif.bounce.bouncedRecipients[0].emailAddress == "x@example.com"


def test_record_bounce_writes_one_row_per_recipient(monkeypatch):
    suppression, feedback_events = _install_fake_tables(monkeypatch)
    from backend.feedback.handlers import record_bounce
    from backend.feedback.models import SesBouncePayload, SesBounceRecipient

    payload = SesBouncePayload(
        bounceType="Permanent",
        bounceSubType="General",
        bouncedRecipients=[
            SesBounceRecipient(emailAddress="a@example.com"),
            SesBounceRecipient(emailAddress="b@example.com"),
        ],
    )
    result = record_bounce(payload, task_id="t-1")
    assert result.notification_type == "Bounce"
    assert result.recipient_count == 2
    assert sorted(result.suppressed) == ["a@example.com", "b@example.com"]
    assert len(suppression.puts) == 2
    for item in suppression.puts:
        assert item["reason"] == "bounce"
        assert item["subtype"] == "General"
        assert item["task_id"] == "t-1"
    assert len(feedback_events.puts) == 1


def test_record_complaint_writes_suppression(monkeypatch):
    suppression, feedback_events = _install_fake_tables(monkeypatch)
    from backend.feedback.handlers import record_complaint
    from backend.feedback.models import SesComplaintPayload, SesComplaintRecipient

    payload = SesComplaintPayload(
        complaintFeedbackType="abuse",
        complainedRecipients=[SesComplaintRecipient(emailAddress="c@example.com")],
    )
    result = record_complaint(payload, task_id="t-2")
    assert result.notification_type == "Complaint"
    assert result.recipient_count == 1
    assert result.suppressed == ["c@example.com"]
    assert len(suppression.puts) == 1
    assert suppression.puts[0]["email"] == "c@example.com"
    assert suppression.puts[0]["reason"] == "complaint"
    assert suppression.puts[0]["subtype"] == "abuse"
    assert len(feedback_events.puts) == 1


def test_record_delivery_no_suppression_write(monkeypatch):
    suppression, feedback_events = _install_fake_tables(monkeypatch)
    from backend.feedback.handlers import record_delivery
    from backend.feedback.models import SesDeliveryPayload

    payload = SesDeliveryPayload(
        recipients=["d@example.com", "e@example.com"],
        processingTimeMillis=17,
    )
    result = record_delivery(payload, task_id="t-3")
    assert result.notification_type == "Delivery"
    assert result.recipient_count == 2
    assert result.suppressed == []
    assert suppression.puts == []
    assert len(feedback_events.puts) == 1
    assert feedback_events.puts[0]["recipients"] == ["d@example.com", "e@example.com"]
