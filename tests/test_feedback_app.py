"""TestClient smoke tests for backend.feedback.app."""

from __future__ import annotations

import json
from typing import Any

import pytest


class _FakeTable:
    """In-memory stand-in for a DynamoDB Table; just records calls."""

    def __init__(self) -> None:
        self.puts: list[dict[str, Any]] = []

    def put_item(self, *, Item: dict[str, Any]) -> None:
        self.puts.append(Item)


def _reset_idempotency(monkeypatch):
    from backend.common.idempotency import IdempotencyStore
    import backend.common.idempotency as idem_mod

    fresh = IdempotencyStore()
    monkeypatch.setattr(idem_mod, "GLOBAL_IDEMPOTENCY_STORE", fresh)
    import backend.feedback.service as svc_mod

    monkeypatch.setattr(svc_mod, "GLOBAL_IDEMPOTENCY_STORE", fresh)
    return fresh


def _install_fake_tables(monkeypatch) -> tuple[_FakeTable, _FakeTable]:
    suppression = _FakeTable()
    feedback_events = _FakeTable()
    import backend.feedback.handlers as handlers_mod

    monkeypatch.setattr(handlers_mod, "_suppression_table", lambda: suppression)
    monkeypatch.setattr(handlers_mod, "_feedback_events_table", lambda: feedback_events)
    return suppression, feedback_events


def _bounce_message(addresses: list[str]) -> str:
    return json.dumps(
        {
            "notificationType": "Bounce",
            "mail": {"messageId": "abc", "source": "ops@hustleforge.com"},
            "bounce": {
                "bounceType": "Permanent",
                "bounceSubType": "General",
                "bouncedRecipients": [{"emailAddress": a} for a in addresses],
                "timestamp": "2026-05-14T00:00:00.000Z",
            },
        }
    )


def _complaint_message(addresses: list[str]) -> str:
    return json.dumps(
        {
            "notificationType": "Complaint",
            "mail": {"messageId": "abc", "source": "ops@hustleforge.com"},
            "complaint": {
                "complaintFeedbackType": "abuse",
                "complainedRecipients": [{"emailAddress": a} for a in addresses],
                "timestamp": "2026-05-14T00:00:00.000Z",
            },
        }
    )


def _delivery_message(addresses: list[str]) -> str:
    return json.dumps(
        {
            "notificationType": "Delivery",
            "mail": {"messageId": "abc", "source": "ops@hustleforge.com"},
            "delivery": {
                "timestamp": "2026-05-14T00:00:00.000Z",
                "recipients": addresses,
                "processingTimeMillis": 42,
            },
        }
    )


def _sns_envelope(message: str) -> dict[str, Any]:
    return {
        "Type": "Notification",
        "MessageId": "11111111-2222-3333-4444-555555555555",
        "TopicArn": "arn:aws:sns:us-west-1:000000000000:samus-ses-feedback",
        "Message": message,
        "Timestamp": "2026-05-14T00:00:01.000Z",
    }


def test_health_endpoint_ok(tmp_path, monkeypatch):
    monkeypatch.setenv("SAMUS_FEEDBACK_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    from fastapi.testclient import TestClient
    from backend.feedback.app import app

    client = TestClient(app)
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["service"] == "feedback"
    assert body["status"] == "ok"


def test_bounce_writes_suppression_for_each_recipient(tmp_path, monkeypatch):
    _reset_idempotency(monkeypatch)
    suppression, feedback_events = _install_fake_tables(monkeypatch)
    monkeypatch.setenv("SAMUS_FEEDBACK_AUDIT_PATH", str(tmp_path / "audit.jsonl"))

    from fastapi.testclient import TestClient
    from backend.feedback.app import app

    client = TestClient(app)
    body = _sns_envelope(_bounce_message(["alice@example.com", "bob@example.com"]))
    r = client.post("/api/ses/feedback", json=body)
    assert r.status_code == 200, r.text
    result = r.json()
    assert result["notification_type"] == "Bounce"
    assert result["recipient_count"] == 2
    assert sorted(result["suppressed"]) == ["alice@example.com", "bob@example.com"]

    assert len(suppression.puts) == 2
    emails = sorted(item["email"] for item in suppression.puts)
    assert emails == ["alice@example.com", "bob@example.com"]
    for item in suppression.puts:
        assert item["reason"] == "bounce"
        assert item["subtype"] == "General"
        assert "ts" in item
        assert "task_id" in item
    assert len(feedback_events.puts) == 1
    assert feedback_events.puts[0]["notification_type"] == "Bounce"


def test_complaint_writes_suppression(tmp_path, monkeypatch):
    _reset_idempotency(monkeypatch)
    suppression, feedback_events = _install_fake_tables(monkeypatch)
    monkeypatch.setenv("SAMUS_FEEDBACK_AUDIT_PATH", str(tmp_path / "audit.jsonl"))

    from fastapi.testclient import TestClient
    from backend.feedback.app import app

    client = TestClient(app)
    body = _sns_envelope(_complaint_message(["mallory@example.com"]))
    r = client.post("/api/ses/feedback", json=body)
    assert r.status_code == 200, r.text
    result = r.json()
    assert result["notification_type"] == "Complaint"
    assert result["recipient_count"] == 1
    assert result["suppressed"] == ["mallory@example.com"]

    assert len(suppression.puts) == 1
    assert suppression.puts[0]["email"] == "mallory@example.com"
    assert suppression.puts[0]["reason"] == "complaint"
    assert len(feedback_events.puts) == 1
    assert feedback_events.puts[0]["notification_type"] == "Complaint"


def test_delivery_no_suppression_write(tmp_path, monkeypatch):
    _reset_idempotency(monkeypatch)
    suppression, feedback_events = _install_fake_tables(monkeypatch)
    monkeypatch.setenv("SAMUS_FEEDBACK_AUDIT_PATH", str(tmp_path / "audit.jsonl"))

    from fastapi.testclient import TestClient
    from backend.feedback.app import app

    client = TestClient(app)
    body = _sns_envelope(_delivery_message(["customer@example.com"]))
    r = client.post("/api/ses/feedback", json=body)
    assert r.status_code == 200, r.text
    result = r.json()
    assert result["notification_type"] == "Delivery"
    assert result["recipient_count"] == 1
    assert result["suppressed"] == []

    assert suppression.puts == []
    assert len(feedback_events.puts) == 1
    assert feedback_events.puts[0]["notification_type"] == "Delivery"


def test_subscription_confirmation_returns_without_aws_calls(tmp_path, monkeypatch):
    _reset_idempotency(monkeypatch)
    suppression, feedback_events = _install_fake_tables(monkeypatch)
    monkeypatch.setenv("SAMUS_FEEDBACK_AUDIT_PATH", str(tmp_path / "audit.jsonl"))

    from fastapi.testclient import TestClient
    from backend.feedback.app import app

    client = TestClient(app)
    body = {
        "Type": "SubscriptionConfirmation",
        "MessageId": "abc",
        "TopicArn": "arn:aws:sns:us-west-1:000000000000:samus-ses-feedback",
        "Token": "tok",
        "Message": "Please confirm",
        "SubscribeURL": "https://sns.example.com/confirm?token=tok",
    }
    r = client.post("/api/ses/feedback", json=body)
    assert r.status_code == 200, r.text
    result = r.json()
    assert result["notification_type"] == "SubscriptionConfirmation"
    assert result["recipient_count"] == 0
    assert result["suppressed"] == []

    assert suppression.puts == []
    assert feedback_events.puts == []


def test_idempotent_repeat_returns_cached(tmp_path, monkeypatch):
    _reset_idempotency(monkeypatch)
    suppression, feedback_events = _install_fake_tables(monkeypatch)
    monkeypatch.setenv("SAMUS_FEEDBACK_AUDIT_PATH", str(tmp_path / "audit.jsonl"))

    from fastapi.testclient import TestClient
    from backend.feedback.app import app

    client = TestClient(app)
    inner = _sns_envelope(_bounce_message(["dup@example.com"]))
    body = {"task_id": "task-feedback-dup-1", "payload": inner, "metadata": {}}

    r1 = client.post("/api/ses/feedback", json=body)
    assert r1.status_code == 200, r1.text
    r2 = client.post("/api/ses/feedback", json=body)
    assert r2.status_code == 200, r2.text

    # Same event_id on both calls — proves second came from cache.
    assert r1.json() == r2.json()
    # Only the first call drove DDB puts.
    assert len(suppression.puts) == 1
    assert len(feedback_events.puts) == 1


def test_taskenvelope_wrapper_accepted(tmp_path, monkeypatch):
    _reset_idempotency(monkeypatch)
    suppression, feedback_events = _install_fake_tables(monkeypatch)
    monkeypatch.setenv("SAMUS_FEEDBACK_AUDIT_PATH", str(tmp_path / "audit.jsonl"))

    from fastapi.testclient import TestClient
    from backend.feedback.app import app

    client = TestClient(app)
    inner = _sns_envelope(_bounce_message(["wrap@example.com"]))
    body = {"task_id": "task-wrap-1", "payload": inner, "metadata": {"src": "gateway"}}
    r = client.post("/api/ses/feedback", json=body)
    assert r.status_code == 200, r.text
    assert r.json()["suppressed"] == ["wrap@example.com"]


# ---------------------------------------------------------------------------
# M4 — SubscribeURL SSRF allowlist (CWE-918)
# ---------------------------------------------------------------------------


def test_subscribe_url_aws_sns_allowlist():
    """_subscribe_url_is_aws_sns admits only HTTPS AWS SNS hosts."""
    from backend.feedback.app import _subscribe_url_is_aws_sns

    # Genuine AWS SNS endpoints (any region) pass.
    assert _subscribe_url_is_aws_sns(
        "https://sns.us-west-1.amazonaws.com/?Action=ConfirmSubscription"
    )
    assert _subscribe_url_is_aws_sns(
        "https://sns.eu-central-1.amazonaws.com/?Action=ConfirmSubscription"
    )
    # Wrong scheme, internal targets, and look-alike hosts are all rejected.
    assert not _subscribe_url_is_aws_sns(
        "http://sns.us-west-1.amazonaws.com/?Action=ConfirmSubscription"
    )
    assert not _subscribe_url_is_aws_sns("https://evil.example.com/?Action=Confirm")
    assert not _subscribe_url_is_aws_sns("https://169.254.169.254/latest/meta-data/")
    assert not _subscribe_url_is_aws_sns("https://sns.us-west-1.amazonaws.com.evil.example/")
    assert not _subscribe_url_is_aws_sns("https://snsxus-west-1.amazonaws.com/")
    assert not _subscribe_url_is_aws_sns("")


def test_confirm_subscription_rejects_non_aws_url_without_fetching(monkeypatch):
    """A non-AWS SubscribeURL is dropped before any httpx.get is issued."""
    import backend.feedback.app as app_mod

    called = {"hit": False}

    def _explode(*a, **kw):  # pragma: no cover - must not be reached
        called["hit"] = True
        raise AssertionError("httpx.get must not be called for a non-AWS URL")

    monkeypatch.setattr(app_mod.httpx, "get", _explode)
    app_mod._confirm_subscription("https://evil.example.com/?Action=Confirm")
    assert called["hit"] is False


def test_confirm_subscription_fetches_aws_url(monkeypatch):
    """A valid AWS SNS SubscribeURL is fetched via httpx.get."""
    import backend.feedback.app as app_mod

    seen: dict[str, Any] = {}

    class _Resp:
        status_code = 200

    def _fake_get(url, **kwargs):
        seen["url"] = url
        return _Resp()

    monkeypatch.setattr(app_mod.httpx, "get", _fake_get)
    app_mod._confirm_subscription(
        "https://sns.us-west-1.amazonaws.com/?Action=ConfirmSubscription&Token=t"
    )
    assert seen["url"].startswith("https://sns.us-west-1.amazonaws.com/")


# ---------------------------------------------------------------------------
# L2 — production guard on SAMUS_FEEDBACK_VERIFY_SNS (CWE-294)
# ---------------------------------------------------------------------------


def test_verify_flag_cannot_be_disabled_in_production(monkeypatch):
    """In a production env, disabling SNS verification fails closed."""
    from backend.common.settings import reload_settings
    from backend.feedback.app import _signature_verification_enabled

    monkeypatch.setenv("SAMUS_FEEDBACK_VERIFY_SNS", "0")
    monkeypatch.setenv("SAMUS_ENV", "production")
    reload_settings()

    with pytest.raises(RuntimeError, match="cannot be disabled in a production"):
        _signature_verification_enabled()


def test_verify_flag_disable_allowed_outside_production(monkeypatch):
    """Outside production the flag may still be disabled (test convenience)."""
    from backend.common.settings import reload_settings
    from backend.feedback.app import _signature_verification_enabled

    monkeypatch.setenv("SAMUS_FEEDBACK_VERIFY_SNS", "0")
    monkeypatch.setenv("SAMUS_ENV", "development")
    reload_settings()

    assert _signature_verification_enabled() is False


# ---------------------------------------------------------------------------
# FIN-05 — SNS replay gate is persistent (survives container restart)
# ---------------------------------------------------------------------------


def test_replay_claim_survives_inprocess_store_reset(tmp_path, monkeypatch):
    """A persistent claim blocks a replay even after the in-process store is
    emptied (the restart-bypass FIN-05 closed).

    The first claim wins; resetting the in-process OrderedDict simulates a
    container restart; the second claim of the SAME MessageId must still be
    rejected because the persistent ledger ``claim()`` already holds it.
    """
    monkeypatch.setenv("SAMUS_FEEDBACK_REPLAY_PATH", str(tmp_path / "sns_replay.jsonl"))
    monkeypatch.setenv("SAMUS_LEDGER_BACKEND", "jsonl")

    import backend.feedback.app as app_mod
    from backend.common.idempotency import IdempotencyStore

    msg_id = "msg-fin05-replay-1"

    # First sighting wins.
    monkeypatch.setattr(app_mod, "GLOBAL_IDEMPOTENCY_STORE", IdempotencyStore())
    assert app_mod._claim_replay(msg_id) is True

    # Simulate a container restart: a brand-new empty in-process store.
    monkeypatch.setattr(app_mod, "GLOBAL_IDEMPOTENCY_STORE", IdempotencyStore())
    # The persistent ledger still holds the claim -> replay rejected.
    assert app_mod._claim_replay(msg_id) is False


def test_replay_claim_distinct_ids_each_win(tmp_path, monkeypatch):
    """Distinct MessageIds each win their own persistent claim."""
    monkeypatch.setenv("SAMUS_FEEDBACK_REPLAY_PATH", str(tmp_path / "sns_replay.jsonl"))
    monkeypatch.setenv("SAMUS_LEDGER_BACKEND", "jsonl")

    import backend.feedback.app as app_mod
    from backend.common.idempotency import IdempotencyStore

    monkeypatch.setattr(app_mod, "GLOBAL_IDEMPOTENCY_STORE", IdempotencyStore())
    assert app_mod._claim_replay("msg-a") is True
    assert app_mod._claim_replay("msg-b") is True
    # Repeat of msg-a within the same process is a duplicate.
    assert app_mod._claim_replay("msg-a") is False
