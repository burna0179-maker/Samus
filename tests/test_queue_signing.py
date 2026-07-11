"""FIN-03 — per-message HMAC authenticity for SQS QueueEnvelopes.

Covers the signing module (sign/verify round trip, tamper detection) and the
audit/enforce policy wired into ``BaseSqsWorker._process_message``:
  * signed message verifies and is processed;
  * tampered / unsigned message is LOGGED but processed in audit mode (default);
  * tampered / unsigned message is REJECTED (dropped, handler never runs) in
    enforce mode (SAMUS_SQS_REQUIRE_HMAC on).
"""
from __future__ import annotations

from typing import Any

import pytest

from backend.common import queue_signing
from backend.common.queue_contracts import QueueEnvelope

_KEY = "fin03-test-shared-hmac-key"


@pytest.fixture(autouse=True)
def _with_hmac_key(monkeypatch):
    """Configure a shared HMAC key + reload settings so signing has material."""
    monkeypatch.setenv("SAMUS_SHARED_HMAC_KEY", _KEY)
    # Ensure no per-service override shadows the shared key for the gateway.
    monkeypatch.delenv("SAMUS_HMAC_KEY_GATEWAY", raising=False)
    from backend.common.settings import reload_settings
    reload_settings()
    yield
    reload_settings()


def _envelope(**overrides: Any) -> QueueEnvelope:
    base = dict(
        task_id="t-1", service="crm", action="close_payment_to_opportunity",
        payload={"email": "buyer@x.com", "amount_usd": 1500.0},
        metadata={"src": "gateway"},
    )
    base.update(overrides)
    return QueueEnvelope(**base)


# ---------------------------------------------------------------------------
# Signing module — round trip + tamper detection
# ---------------------------------------------------------------------------

def test_sign_then_verify_round_trip():
    env = queue_signing.sign_envelope(_envelope())
    assert env.hmac  # populated
    assert queue_signing.verify_envelope(env) is True


def test_tampered_payload_fails_verify():
    env = queue_signing.sign_envelope(_envelope())
    # Forge the amount after signing — the recomputed HMAC no longer matches.
    env.payload["amount_usd"] = 999999.0
    assert queue_signing.verify_envelope(env) is False


def test_unsigned_envelope_fails_verify():
    env = _envelope()  # never signed
    assert env.hmac is None
    assert queue_signing.verify_envelope(env) is False


def test_serialized_signature_survives_round_trip():
    env = queue_signing.sign_envelope(_envelope())
    raw = env.model_dump_json()
    back = QueueEnvelope.model_validate_json(raw)
    assert back.hmac == env.hmac
    assert queue_signing.verify_envelope(back) is True


def test_no_key_signs_nothing(monkeypatch):
    monkeypatch.setenv("SAMUS_SHARED_HMAC_KEY", "")
    from backend.common.settings import reload_settings
    reload_settings()
    env = queue_signing.sign_envelope(_envelope())
    assert env.hmac is None


# ---------------------------------------------------------------------------
# Audit / enforce policy
# ---------------------------------------------------------------------------

def test_audit_mode_processes_bad_message(monkeypatch, caplog):
    monkeypatch.delenv("SAMUS_SQS_REQUIRE_HMAC", raising=False)  # default OFF
    assert queue_signing.enforce_hmac() is False
    env = _envelope()  # unsigned
    with caplog.at_level("WARNING"):
        assert queue_signing.check_message_authenticity(env, service="crm") is True
    assert any("audit" in r.message or "audit" in str(r.__dict__) for r in caplog.records)


def test_enforce_mode_rejects_unsigned(monkeypatch):
    monkeypatch.setenv("SAMUS_SQS_REQUIRE_HMAC", "1")
    assert queue_signing.enforce_hmac() is True
    env = _envelope()  # unsigned
    assert queue_signing.check_message_authenticity(env, service="crm") is False


def test_enforce_mode_rejects_tampered(monkeypatch):
    monkeypatch.setenv("SAMUS_SQS_REQUIRE_HMAC", "1")
    env = queue_signing.sign_envelope(_envelope())
    env.payload["amount_usd"] = 1.0
    assert queue_signing.check_message_authenticity(env, service="crm") is False


def test_enforce_mode_admits_valid(monkeypatch):
    monkeypatch.setenv("SAMUS_SQS_REQUIRE_HMAC", "1")
    env = queue_signing.sign_envelope(_envelope())
    assert queue_signing.check_message_authenticity(env, service="crm") is True


# ---------------------------------------------------------------------------
# Wiring through BaseSqsWorker._process_message
# ---------------------------------------------------------------------------

class _FakeTable:
    def put_item(self, **kwargs):  # noqa: D401 - stub
        pass


class _FakeRuntime:
    def __init__(self):
        self.deleted: list[str] = []

    def task_state_table(self):
        return _FakeTable()

    def publish_event(self, *a, **kw):
        pass

    def delete_message(self, receipt: str) -> None:
        self.deleted.append(receipt)


def _make_worker(tmp_path, monkeypatch):
    monkeypatch.setenv("SAMUS_DATA_ROOT", str(tmp_path))
    from backend.common.idempotency import IdempotencyStore
    from backend.common.worker_base import BaseSqsWorker

    handled: list[QueueEnvelope] = []

    class _Worker(BaseSqsWorker):
        service = "crm"

        def handle(self, envelope, *, stop_event=None):
            handled.append(envelope)
            return {"ok": True}

    # Fresh per-worker idempotency store so a shared task_id across cases in
    # this file does not dedup the second worker's message.
    return _Worker(_FakeRuntime(), idempotency_store=IdempotencyStore()), handled


def _msg(env: QueueEnvelope) -> dict[str, Any]:
    return {"ReceiptHandle": "rh-1", "Body": env.model_dump_json()}


def test_process_message_signed_is_handled(tmp_path, monkeypatch):
    monkeypatch.setenv("SAMUS_SQS_REQUIRE_HMAC", "1")
    worker, handled = _make_worker(tmp_path, monkeypatch)
    env = queue_signing.sign_envelope(_envelope())
    worker._process_message(_msg(env))
    assert len(handled) == 1
    assert worker.runtime.deleted == ["rh-1"]  # completed -> deleted


def test_process_message_enforce_drops_unsigned(tmp_path, monkeypatch):
    monkeypatch.setenv("SAMUS_SQS_REQUIRE_HMAC", "1")
    worker, handled = _make_worker(tmp_path, monkeypatch)
    env = _envelope()  # unsigned
    worker._process_message(_msg(env))
    assert handled == []  # handler never ran — fail closed
    assert worker.runtime.deleted == ["rh-1"]  # dropped, not redriven


def test_process_message_audit_processes_unsigned(tmp_path, monkeypatch):
    monkeypatch.delenv("SAMUS_SQS_REQUIRE_HMAC", raising=False)  # audit default
    worker, handled = _make_worker(tmp_path, monkeypatch)
    env = _envelope()  # unsigned
    worker._process_message(_msg(env))
    assert len(handled) == 1  # processed despite missing HMAC (audit mode)
