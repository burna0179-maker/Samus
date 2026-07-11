"""TaskEnvelope shape."""
from __future__ import annotations

import pytest
from pydantic import ValidationError


def test_task_envelope_defaults():
    from backend.common.models import TaskEnvelope
    env = TaskEnvelope(task_id="t1")
    assert env.task_id == "t1"
    assert env.payload == {}
    assert env.metadata == {}


def test_task_envelope_rejects_empty_task_id():
    from backend.common.models import TaskEnvelope
    with pytest.raises(ValidationError):
        TaskEnvelope(task_id="")


def test_task_envelope_round_trip():
    from backend.common.models import TaskEnvelope
    raw = TaskEnvelope(
        task_id="t1",
        payload={"k": "v"},
        metadata={"approvals": ["owner"]},
    ).model_dump_json()
    back = TaskEnvelope.model_validate_json(raw)
    assert back.payload == {"k": "v"}
    assert back.metadata["approvals"] == ["owner"]
