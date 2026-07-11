"""SQS wiring — producer (enqueue -> real queue) + consumer (poll -> process_job)."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from backend.cash_engine import queue as cash_queue
from backend.cash_engine import worker as cash_worker
from backend.common.queue_contracts import QueueEnvelope


# --------------------------------------------------------------------------
# Roster + producer
# --------------------------------------------------------------------------

def test_roster_reads_cash_engine_queue_env(monkeypatch):
    monkeypatch.setenv("SQS_CASH_ENGINE_QUEUE_URL", "https://sqs.example/cash")
    from backend.common.settings import bootstrap_settings
    s = bootstrap_settings()
    assert s.sqs_queue_urls.get("cash_engine") == "https://sqs.example/cash"


def test_enqueue_uses_sqs_when_queue_configured(monkeypatch):
    from backend.gateway import sqs_dispatch

    # setitem auto-reverts at teardown -> no QUEUE_URLS leak into other tests.
    monkeypatch.setitem(sqs_dispatch.QUEUE_URLS, "cash_engine", "https://sqs.example/cash")
    fake = MagicMock()
    fake.send_message = MagicMock(return_value={"MessageId": "m-1"})
    monkeypatch.setattr(sqs_dispatch, "sqs_client", lambda: fake)

    res = cash_queue.enqueue_cash_job(
        task_id="ce-1",
        payload={"opportunity_id": "op-1", "prospect_id": "pr-1"},
        metadata={"action": "cash_engine_step"},
        idempotency_key="cash:pr-1:signal_decay",
    )
    assert res["queued"] is True
    assert res["queue"] == "sqs:cash_engine"
    assert res["message_id"] == "m-1"
    assert res["service"] == "cash_engine"
    fake.send_message.assert_called_once()
    # The body sent is a QueueEnvelope carrying the cash-engine payload.
    sent_body = fake.send_message.call_args.kwargs["MessageBody"]
    env = QueueEnvelope.model_validate_json(sent_body)
    assert env.service == "cash_engine"
    assert env.action == "cash_engine_step"
    assert env.payload["opportunity_id"] == "op-1"


def test_enqueue_falls_back_to_mock_without_queue(tmp_path, monkeypatch):
    monkeypatch.setenv("SAMUS_STATE_ROOT", str(tmp_path))
    from backend.gateway import sqs_dispatch
    monkeypatch.delitem(sqs_dispatch.QUEUE_URLS, "cash_engine", raising=False)
    res = cash_queue.enqueue_cash_job(task_id="ce-2", payload={"opportunity_id": "op-2"})
    assert res["queue"] == "mock:jsonl"


# --------------------------------------------------------------------------
# Consumer
# --------------------------------------------------------------------------

@pytest.fixture
def sqs_worker(tmp_path, monkeypatch):
    monkeypatch.setenv("SAMUS_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("SAMUS_STATE_ROOT", str(tmp_path))
    return cash_worker.CashEngineSqsWorker(SimpleNamespace())


def _envelope():
    return QueueEnvelope(
        task_id="ce-1", service="cash_engine", action="cash_engine_step",
        payload={"opportunity_id": "op-1", "prospect_id": "pr-1"},
    )


def test_handle_routes_envelope_to_process_job(sqs_worker, monkeypatch):
    captured = {}

    def fake_process_job(job, **kw):
        captured["job"] = job
        return SimpleNamespace(model_dump=lambda: {"status": "dormant", "opportunity_id": "op-1"})

    monkeypatch.setattr(cash_worker, "process_job", fake_process_job)
    out = sqs_worker.handle(_envelope())
    assert out == {"status": "dormant", "opportunity_id": "op-1"}
    assert captured["job"]["payload"]["opportunity_id"] == "op-1"
    assert captured["job"]["task_id"] == "ce-1"


def test_handle_returns_structured_noop_when_dropped(sqs_worker, monkeypatch):
    monkeypatch.setattr(cash_worker, "process_job", lambda job, **kw: None)
    out = sqs_worker.handle(_envelope())
    assert out["ok"] is False
    assert out["reason"] == "no_state_produced"


def test_serve_entrypoint_is_wired():
    # The module exposes the standard worker entrypoint + real (non-placeholder)
    # class when worker_base is importable.
    assert cash_worker._SQS_IMPORT_ERROR is None
    assert callable(cash_worker.main)
    assert cash_worker.CashEngineSqsWorker.service == "cash_engine"
