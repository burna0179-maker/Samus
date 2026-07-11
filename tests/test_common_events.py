"""Doc §3.18 — build_audit_event + _deterministic_hash."""
from __future__ import annotations

import re

from backend.common import correlation
from backend.common.events import _deterministic_hash, build_audit_event


_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_HEX_RE = re.compile(r"^[0-9a-f]{64}$")


def test_build_audit_event_basic_shape():
    ev = build_audit_event(
        service="leadgen",
        task_id="t1",
        action="score",
        input_payload={"company": "Acme"},
        output_payload={"score": 80},
        status="completed",
    )
    assert _UUID_RE.match(ev["event_id"])
    assert ev["service"] == "leadgen"
    assert ev["task_id"] == "t1"
    assert ev["action"] == "score"
    assert ev["status"] == "completed"
    assert _HEX_RE.match(ev["input_hash"])
    assert _HEX_RE.match(ev["output_hash"])
    assert ev["metadata"] == {}
    assert "ts" in ev


def test_build_audit_event_metadata_passthrough():
    ev = build_audit_event("x", "t", "a", {}, {}, "ok", metadata={"approvals": ["owner"]})
    assert ev["metadata"] == {"approvals": ["owner"]}


def test_build_audit_event_propagates_trace_id():
    correlation.set_trace_id("trace-xyz")
    try:
        ev = build_audit_event("x", "t", "a", {}, {}, "ok")
        assert ev["trace_id"] == "trace-xyz"
    finally:
        correlation.set_trace_id("")


def test_deterministic_hash_stable_for_equivalent_payloads():
    a = _deterministic_hash({"a": 1, "b": 2})
    b = _deterministic_hash({"b": 2, "a": 1})  # key order shouldn't matter
    assert a == b


def test_deterministic_hash_changes_for_different_payloads():
    a = _deterministic_hash({"a": 1})
    b = _deterministic_hash({"a": 2})
    assert a != b


def test_deterministic_hash_handles_non_json_types():
    from datetime import datetime, timezone
    h = _deterministic_hash({"ts": datetime(2026, 5, 15, tzinfo=timezone.utc)})
    assert _HEX_RE.match(h)


def test_input_and_output_hashes_independent():
    ev = build_audit_event("x", "t", "a", {"in": 1}, {"out": 2}, "ok")
    assert ev["input_hash"] != ev["output_hash"]
