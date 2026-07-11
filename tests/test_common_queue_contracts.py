from backend.common.queue_contracts import QueueDispatchError, QueueEnvelope


def test_queue_envelope_round_trip():
    env = QueueEnvelope(
        task_id="t1",
        service="prospecting",
        action="discover",
        payload={"k": "v"},
        trace_id="trace-1",
        idempotency_key="idem-1",
    )
    raw = env.model_dump_json()
    back = QueueEnvelope.model_validate_json(raw)
    assert back.task_id == "t1"
    assert back.service == "prospecting"
    assert back.action == "discover"
    assert back.payload == {"k": "v"}
    assert back.trace_id == "trace-1"
    assert back.idempotency_key == "idem-1"


def test_queue_envelope_metadata_default_empty():
    env = QueueEnvelope(task_id="t1", service="s", action="a")
    assert env.metadata == {}
    assert env.payload == {}


def test_dispatch_error_carries_code_and_reason():
    exc = QueueDispatchError(409, "duplicate_task_id")
    assert exc.code == 409
    assert exc.reason == "duplicate_task_id"
    assert "duplicate_task_id" in str(exc)
