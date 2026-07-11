"""Strict-signature verification tests for the feedback workcell.

These tests generate a real RSA keypair + self-signed X.509 cert in-process,
sign a canonical SNS string per the AWS spec, and feed the result through both:

  - :func:`backend.feedback.sns_signature.verify_sns_message` directly, and
  - the FastAPI route at ``POST /api/ses/feedback`` with verification turned ON.

Coverage targets the items called out in the audit checklist:

  - valid Notification passes,
  - tampered Message fails,
  - wrong SigningCertURL host fails,
  - missing required fields fail,
  - SignatureVersion outside {"1","2"} fails,
  - TopicArn outside the allowlist fails (when allowlist is configured),
  - SubscriptionConfirmation passes verification.

Replay protection: a second POST carrying a previously-seen MessageId is
short-circuited at the route with ``notification_type="Replay"``; see
``test_route_rejects_replayed_message_id`` at the bottom of the file.
"""
from __future__ import annotations

import base64
import datetime as dt
import json
from typing import Any

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import NameOID


# ---------------------------------------------------------------------------
# Helpers: real RSA keypair + self-signed cert + canonical signing helper.
# ---------------------------------------------------------------------------


def _make_cert_and_key() -> tuple[rsa.RSAPrivateKey, bytes]:
    """Generate a fresh RSA-2048 key and a 1-day self-signed X.509 PEM cert."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "sns-test")])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    pem = cert.public_bytes(serialization.Encoding.PEM)
    return key, pem


def _canonical_for(message: dict[str, Any]) -> bytes:
    """Re-import the production canonicalizer so test signatures match exactly."""
    from backend.feedback.sns_signature import build_string_to_sign

    return build_string_to_sign(message).encode("utf-8")


def _sign(key: rsa.RSAPrivateKey, message: dict[str, Any], *, version: str = "1") -> str:
    """Sign a canonical SNS string with the given key and SignatureVersion."""
    algo = hashes.SHA1() if version == "1" else hashes.SHA256()  # noqa: S303 — AWS spec
    sig = key.sign(_canonical_for(message), padding.PKCS1v15(), algo)
    return base64.b64encode(sig).decode("ascii")


def _stub_http_get(pem: bytes):
    """Return a fake ``httpx.get``-shaped callable that serves the given PEM."""

    class _Resp:
        def __init__(self, body: bytes, status: int = 200) -> None:
            self.content = body
            self.status_code = status

    def _get(url: str) -> _Resp:  # noqa: ARG001 — URL unused in stub
        return _Resp(pem)

    return _get


def _notification(message_text: str = "hi", *, topic_arn: str = "arn:aws:sns:us-west-1:000000000000:samus-ses-feedback") -> dict[str, Any]:
    """Minimal SNS Notification skeleton with the fields AWS canonicalizes."""
    return {
        "Type": "Notification",
        "MessageId": "11111111-2222-3333-4444-555555555555",
        "TopicArn": topic_arn,
        "Message": message_text,
        "Timestamp": "2026-05-14T00:00:01.000Z",
        "SignatureVersion": "1",
        "SigningCertURL": "https://sns.us-west-1.amazonaws.com/SimpleNotificationService-test.pem",
    }


@pytest.fixture
def _clear_cert_cache():
    """Ensure each test gets a clean cert cache."""
    from backend.feedback.sns_signature import _CERT_CACHE

    _CERT_CACHE.clear()
    yield
    _CERT_CACHE.clear()


# ---------------------------------------------------------------------------
# Direct verifier tests.
# ---------------------------------------------------------------------------


def test_valid_signature_passes(_clear_cert_cache):
    from backend.feedback.sns_signature import verify_sns_message

    key, pem = _make_cert_and_key()
    msg = _notification("hello world")
    msg["Signature"] = _sign(key, msg, version="1")

    verify_sns_message(msg, http_get=_stub_http_get(pem))  # must not raise


def test_signature_version_2_passes(_clear_cert_cache):
    from backend.feedback.sns_signature import verify_sns_message

    key, pem = _make_cert_and_key()
    msg = _notification("hello world")
    msg["SignatureVersion"] = "2"
    msg["Signature"] = _sign(key, msg, version="2")

    verify_sns_message(msg, http_get=_stub_http_get(pem))


def test_tampered_body_fails(_clear_cert_cache):
    from backend.feedback.sns_signature import SnsSignatureError, verify_sns_message

    key, pem = _make_cert_and_key()
    msg = _notification("hello world")
    msg["Signature"] = _sign(key, msg, version="1")
    # Mutate the body AFTER signing — attacker-style payload swap.
    msg["Message"] = "ATTACKER-INJECTED"

    with pytest.raises(SnsSignatureError, match="signature did not verify"):
        verify_sns_message(msg, http_get=_stub_http_get(pem))


def test_wrong_signing_cert_host_fails(_clear_cert_cache):
    from backend.feedback.sns_signature import SnsSignatureError, verify_sns_message

    key, pem = _make_cert_and_key()
    msg = _notification("hello world")
    msg["SigningCertURL"] = "https://evil.example.com/SimpleNotificationService.pem"
    msg["Signature"] = _sign(key, msg, version="1")

    with pytest.raises(SnsSignatureError, match="not in .* allowlist"):
        verify_sns_message(msg, http_get=_stub_http_get(pem))


def test_non_https_signing_cert_url_fails(_clear_cert_cache):
    from backend.feedback.sns_signature import SnsSignatureError, verify_sns_message

    key, pem = _make_cert_and_key()
    msg = _notification("hello world")
    msg["SigningCertURL"] = "http://sns.us-west-1.amazonaws.com/cert.pem"
    msg["Signature"] = _sign(key, msg, version="1")

    with pytest.raises(SnsSignatureError, match="must be HTTPS"):
        verify_sns_message(msg, http_get=_stub_http_get(pem))


def test_missing_signature_fails(_clear_cert_cache):
    from backend.feedback.sns_signature import SnsSignatureError, verify_sns_message

    msg = _notification("hello world")
    # No Signature field at all.

    with pytest.raises(SnsSignatureError, match="Signature is required"):
        verify_sns_message(msg)


def test_missing_signing_cert_url_fails(_clear_cert_cache):
    from backend.feedback.sns_signature import SnsSignatureError, verify_sns_message

    msg = _notification("hello world")
    msg.pop("SigningCertURL")
    msg["Signature"] = "aGVsbG8="  # base64("hello")

    with pytest.raises(SnsSignatureError, match="SigningCertURL is required"):
        verify_sns_message(msg)


def test_missing_required_canonical_field_fails(_clear_cert_cache):
    from backend.feedback.sns_signature import SnsSignatureError, verify_sns_message

    key, pem = _make_cert_and_key()
    msg = _notification("hello world")
    msg["Signature"] = _sign(key, msg, version="1")
    # Strip a required field AFTER signing — must fail at canonicalization,
    # not at "signature didn't verify".
    msg.pop("Timestamp")

    with pytest.raises(SnsSignatureError, match="missing required field"):
        verify_sns_message(msg, http_get=_stub_http_get(pem))


def test_bad_signature_version_fails(_clear_cert_cache):
    from backend.feedback.sns_signature import SnsSignatureError, verify_sns_message

    key, pem = _make_cert_and_key()
    msg = _notification("hello world")
    msg["SignatureVersion"] = "3"
    msg["Signature"] = _sign(key, msg, version="1")

    with pytest.raises(SnsSignatureError, match="SignatureVersion must be one of"):
        verify_sns_message(msg, http_get=_stub_http_get(pem))


def test_topic_arn_allowlist_rejects_other_arns(_clear_cert_cache):
    from backend.feedback.sns_signature import SnsSignatureError, verify_sns_message

    key, pem = _make_cert_and_key()
    msg = _notification("hello world", topic_arn="arn:aws:sns:us-west-1:999:other-topic")
    msg["Signature"] = _sign(key, msg, version="1")

    with pytest.raises(SnsSignatureError, match="not in .* allowlist"):
        verify_sns_message(
            msg,
            http_get=_stub_http_get(pem),
            allowed_topic_arns=["arn:aws:sns:us-west-1:000000000000:samus-ses-feedback"],
        )


def test_topic_arn_allowlist_accepts_match(_clear_cert_cache):
    from backend.feedback.sns_signature import verify_sns_message

    key, pem = _make_cert_and_key()
    msg = _notification("hello world")
    msg["Signature"] = _sign(key, msg, version="1")

    verify_sns_message(
        msg,
        http_get=_stub_http_get(pem),
        allowed_topic_arns=[msg["TopicArn"]],
    )


def test_subscription_confirmation_canonicalizes_subscribe_url(_clear_cert_cache):
    from backend.feedback.sns_signature import verify_sns_message

    key, pem = _make_cert_and_key()
    msg = {
        "Type": "SubscriptionConfirmation",
        "MessageId": "msg-1",
        "Token": "tok-1",
        "TopicArn": "arn:aws:sns:us-west-1:000000000000:samus-ses-feedback",
        "Message": "please confirm",
        "SubscribeURL": "https://sns.us-west-1.amazonaws.com/?Action=ConfirmSubscription&Token=tok-1",
        "Timestamp": "2026-05-14T00:00:01.000Z",
        "SignatureVersion": "1",
        "SigningCertURL": "https://sns.us-west-1.amazonaws.com/cert.pem",
    }
    msg["Signature"] = _sign(key, msg, version="1")

    verify_sns_message(msg, http_get=_stub_http_get(pem))


def test_subscription_confirmation_subscribe_url_tamper_fails(_clear_cert_cache):
    from backend.feedback.sns_signature import SnsSignatureError, verify_sns_message

    key, pem = _make_cert_and_key()
    msg = {
        "Type": "SubscriptionConfirmation",
        "MessageId": "msg-1",
        "Token": "tok-1",
        "TopicArn": "arn:aws:sns:us-west-1:000000000000:samus-ses-feedback",
        "Message": "please confirm",
        "SubscribeURL": "https://sns.us-west-1.amazonaws.com/?Action=ConfirmSubscription&Token=tok-1",
        "Timestamp": "2026-05-14T00:00:01.000Z",
        "SignatureVersion": "1",
        "SigningCertURL": "https://sns.us-west-1.amazonaws.com/cert.pem",
    }
    msg["Signature"] = _sign(key, msg, version="1")
    # Attacker swaps the SubscribeURL to a phishing endpoint — must fail.
    msg["SubscribeURL"] = "https://evil.example.com/?Action=ConfirmSubscription&Token=tok-1"

    with pytest.raises(SnsSignatureError):
        verify_sns_message(msg, http_get=_stub_http_get(pem))


# ---------------------------------------------------------------------------
# End-to-end: real signature through the FastAPI route, verification ON.
# ---------------------------------------------------------------------------


class _FakeTable:
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
    # app.py imports the singleton at module load for the MessageId replay gate.
    import backend.feedback.app as app_mod
    monkeypatch.setattr(app_mod, "GLOBAL_IDEMPOTENCY_STORE", fresh)
    return fresh


def _install_fake_tables(monkeypatch) -> tuple[_FakeTable, _FakeTable]:
    suppression = _FakeTable()
    feedback_events = _FakeTable()
    import backend.feedback.handlers as handlers_mod

    monkeypatch.setattr(handlers_mod, "_suppression_table", lambda: suppression)
    monkeypatch.setattr(handlers_mod, "_feedback_events_table", lambda: feedback_events)
    return suppression, feedback_events


def _enable_verification_with_stub_cert(monkeypatch, pem: bytes):
    """Turn signature verification ON for the route and stub cert fetching."""
    monkeypatch.setenv("SAMUS_FEEDBACK_VERIFY_SNS", "1")
    # Monkeypatch the real cert-fetcher so the route uses our in-process PEM.
    import backend.feedback.sns_signature as sig_mod

    sig_mod._CERT_CACHE.clear()

    class _Resp:
        def __init__(self) -> None:
            self.content = pem
            self.status_code = 200

    def _fake_httpx_get(url, **kwargs):  # noqa: ARG001
        return _Resp()

    monkeypatch.setattr(sig_mod.httpx, "get", _fake_httpx_get)


def test_route_accepts_signed_bounce(tmp_path, monkeypatch):
    _reset_idempotency(monkeypatch)
    suppression, feedback_events = _install_fake_tables(monkeypatch)
    monkeypatch.setenv("SAMUS_FEEDBACK_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    # Isolate the persistent replay-claim ledger per-test too: the default path
    # (/opt/samus/data/feedback/sns_replay_claims) persists across runs on a
    # dev box, so without this the fixed test MessageId is claimed once and
    # every later run/test returns "Replay". Mirrors the audit-path isolation.
    monkeypatch.setenv("SAMUS_FEEDBACK_REPLAY_PATH", str(tmp_path / "sns_replay.jsonl"))

    key, pem = _make_cert_and_key()
    _enable_verification_with_stub_cert(monkeypatch, pem)

    ses_message = json.dumps(
        {
            "notificationType": "Bounce",
            "mail": {"messageId": "abc", "source": "ops@hustleforge.com"},
            "bounce": {
                "bounceType": "Permanent",
                "bounceSubType": "General",
                "bouncedRecipients": [{"emailAddress": "alice@example.com"}],
                "timestamp": "2026-05-14T00:00:00.000Z",
            },
        }
    )
    body = _notification(message_text=ses_message)
    body["Signature"] = _sign(key, body, version="1")

    from fastapi.testclient import TestClient
    from backend.feedback.app import app

    client = TestClient(app)
    r = client.post("/api/ses/feedback", json=body)
    assert r.status_code == 200, r.text
    result = r.json()
    assert result["notification_type"] == "Bounce"
    assert result["suppressed"] == ["alice@example.com"]
    assert len(suppression.puts) == 1
    assert len(feedback_events.puts) == 1


def test_route_rejects_tampered_body_with_403(tmp_path, monkeypatch):
    _reset_idempotency(monkeypatch)
    suppression, feedback_events = _install_fake_tables(monkeypatch)
    monkeypatch.setenv("SAMUS_FEEDBACK_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    # Isolate the persistent replay-claim ledger per-test too: the default path
    # (/opt/samus/data/feedback/sns_replay_claims) persists across runs on a
    # dev box, so without this the fixed test MessageId is claimed once and
    # every later run/test returns "Replay". Mirrors the audit-path isolation.
    monkeypatch.setenv("SAMUS_FEEDBACK_REPLAY_PATH", str(tmp_path / "sns_replay.jsonl"))

    key, pem = _make_cert_and_key()
    _enable_verification_with_stub_cert(monkeypatch, pem)

    ses_message = json.dumps(
        {
            "notificationType": "Bounce",
            "mail": {"messageId": "abc"},
            "bounce": {
                "bounceType": "Permanent",
                "bounceSubType": "General",
                "bouncedRecipients": [{"emailAddress": "victim@example.com"}],
            },
        }
    )
    body = _notification(message_text=ses_message)
    body["Signature"] = _sign(key, body, version="1")
    # Attacker swaps in their own SES payload while keeping the legit signature.
    body["Message"] = json.dumps(
        {
            "notificationType": "Bounce",
            "mail": {"messageId": "abc"},
            "bounce": {
                "bounceType": "Permanent",
                "bouncedRecipients": [{"emailAddress": "attacker-target@example.com"}],
            },
        }
    )

    from fastapi.testclient import TestClient
    from backend.feedback.app import app

    client = TestClient(app)
    r = client.post("/api/ses/feedback", json=body)
    assert r.status_code == 403, r.text
    # Confirm the forged payload never reached the suppression handler.
    assert suppression.puts == []
    assert feedback_events.puts == []


def test_route_rejects_missing_signature_with_403(tmp_path, monkeypatch):
    _reset_idempotency(monkeypatch)
    suppression, feedback_events = _install_fake_tables(monkeypatch)
    monkeypatch.setenv("SAMUS_FEEDBACK_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    # Isolate the persistent replay-claim ledger per-test too: the default path
    # (/opt/samus/data/feedback/sns_replay_claims) persists across runs on a
    # dev box, so without this the fixed test MessageId is claimed once and
    # every later run/test returns "Replay". Mirrors the audit-path isolation.
    monkeypatch.setenv("SAMUS_FEEDBACK_REPLAY_PATH", str(tmp_path / "sns_replay.jsonl"))

    _key, pem = _make_cert_and_key()
    _enable_verification_with_stub_cert(monkeypatch, pem)

    body = _notification(message_text="hi")
    # No Signature field at all — pure forgery attempt.

    from fastapi.testclient import TestClient
    from backend.feedback.app import app

    client = TestClient(app)
    r = client.post("/api/ses/feedback", json=body)
    assert r.status_code == 403, r.text
    assert suppression.puts == []
    assert feedback_events.puts == []


# Replay protection: a second POST carrying a previously-seen MessageId must
# be short-circuited with 200 + notification_type="Replay" and must NOT run
# the suppression handlers a second time. 200 is intentional — non-2xx makes
# SNS retry, which a benign duplicate doesn't warrant. The gate keys on the
# AWS-controlled MessageId in the LRU idempotency store.
def test_route_rejects_replayed_message_id(tmp_path, monkeypatch):
    _reset_idempotency(monkeypatch)
    suppression, feedback_events = _install_fake_tables(monkeypatch)
    monkeypatch.setenv("SAMUS_FEEDBACK_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    # Isolate the persistent replay-claim ledger per-test too: the default path
    # (/opt/samus/data/feedback/sns_replay_claims) persists across runs on a
    # dev box, so without this the fixed test MessageId is claimed once and
    # every later run/test returns "Replay". Mirrors the audit-path isolation.
    monkeypatch.setenv("SAMUS_FEEDBACK_REPLAY_PATH", str(tmp_path / "sns_replay.jsonl"))

    key, pem = _make_cert_and_key()
    _enable_verification_with_stub_cert(monkeypatch, pem)

    ses_message = json.dumps(
        {
            "notificationType": "Bounce",
            "mail": {"messageId": "abc", "source": "ops@hustleforge.com"},
            "bounce": {
                "bounceType": "Permanent",
                "bounceSubType": "General",
                "bouncedRecipients": [{"emailAddress": "alice@example.com"}],
                "timestamp": "2026-05-14T00:00:00.000Z",
            },
        }
    )
    body = _notification(message_text=ses_message)
    body["Signature"] = _sign(key, body, version="1")

    from fastapi.testclient import TestClient
    from backend.feedback.app import app

    client = TestClient(app)

    first = client.post("/api/ses/feedback", json=body)
    assert first.status_code == 200, first.text
    assert first.json()["notification_type"] == "Bounce"
    assert len(suppression.puts) == 1
    assert len(feedback_events.puts) == 1

    # Identical body, same MessageId — replay. Must short-circuit at the gate.
    second = client.post("/api/ses/feedback", json=body)
    assert second.status_code == 200, second.text
    payload = second.json()
    assert payload["notification_type"] == "Replay"
    assert payload["event_id"] == body["MessageId"]
    # Critical: the suppression / events tables must NOT have grown.
    assert len(suppression.puts) == 1
    assert len(feedback_events.puts) == 1


