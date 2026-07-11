"""structured audit logger — fire-and-forget; never raises into the caller."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from backend.common import audit, audit_ledger


@pytest.fixture(autouse=True)
def _isolate_default_ledger(tmp_path: Path, monkeypatch):
    path = tmp_path / "audit.jsonl"
    monkeypatch.setenv("SAMUS_AUDIT_LEDGER_PATH", str(path))
    audit_ledger.reset_default_ledger()
    yield
    audit_ledger.reset_default_ledger()


def test_record_appends_to_log_and_ledger(caplog) -> None:
    caplog.set_level(logging.INFO, logger="samus.audit")
    audit.record("dispatch.test", service="leadgen", action="qualify", ok=True)

    # Log line: JSON with type + payload + trace_id.
    audit_logs = [r for r in caplog.records if r.name == "samus.audit"]
    assert len(audit_logs) == 1
    data = json.loads(audit_logs[0].message)
    assert data["type"] == "dispatch.test"
    assert data["service"] == "leadgen"
    assert data["action"] == "qualify"
    assert data["ok"] is True
    assert "trace_id" in data

    # Ledger has one canonical entry.
    tail = audit.tail()
    assert len(tail) == 1
    assert tail[0]["type"] == "dispatch.test"
    assert tail[0]["payload"]["service"] == "leadgen"


def test_record_never_raises_on_ledger_failure(monkeypatch, caplog) -> None:
    """A broken ledger sink must NOT propagate into the dispatch hot path."""

    class Boom:
        def record(self, *a, **kw):
            raise RuntimeError("simulated disk error")

    # audit.py imports get_default_ledger by name (``from .audit_ledger
    # import get_default_ledger``) so monkeypatch must target the bound
    # name in audit's namespace, not the source module.
    monkeypatch.setattr(audit, "get_default_ledger", lambda: Boom())
    caplog.set_level(logging.ERROR, logger="samus.audit.sink_error")
    # Must not raise.
    audit.record("dispatch.test", k="v")
    sink_errors = [r for r in caplog.records if r.name == "samus.audit.sink_error"]
    assert sink_errors, "ledger failure should be reported on sink_error logger"


def test_verify_returns_ledger_state() -> None:
    audit.record("e1", v=1)
    audit.record("e2", v=2)
    state = audit.verify()
    assert state is not None
    assert state.ok and state.chain_length == 2


def test_record_serialises_non_json_values() -> None:
    """default=str must catch Paths, datetimes, exceptions."""
    audit.record("exotic", path=Path("/tmp/x"), err=RuntimeError("boom"))
    tail = audit.tail()
    assert tail[0]["type"] == "exotic"
    # Payload values were coerced to str.
    assert "tmp" in str(tail[0]["payload"]["path"]) or "x" in str(tail[0]["payload"]["path"])
