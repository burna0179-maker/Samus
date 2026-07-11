"""Guarded business-events shim — no-op pre-merge, delegates post-merge.

Historical context: this shim was written before ``backend.common.business_events``
(HOTL Tranche 1) merged. Pre-merge it returned a no-op event-shaped dict with
``emitted: False`` and ``events_available() == False``. Now that Tranche 1 has
merged the shim ALWAYS delegates to the real module; the pre-merge no-op
branch is only reachable when the real module can't be imported at all (which
should never happen in a healthy checkout). The former ``test_premerge_*``
cases were rewritten to assert the post-merge delegation behavior instead of
deleted — behavioral coverage is preserved.
"""
from __future__ import annotations

import sys
import types

from backend.common.business_events_shim import (
    emit_business_event,
    events_available,
    read_events,
)


def test_emit_delegates_and_returns_real_record_shape():
    """Post-merge: emit reaches the real ledger and returns its record shape.

    The real module normalizes None -> "" for id fields, stamps an event_id +
    trace_id, and returns the full record dict (no ``emitted`` sentinel). The
    conftest points the ledger path at a tmp file so this is a clean write.
    """
    ev = emit_business_event(
        "payment.received", workcell="finance", prospect_id="p_1",
        revenue_usd=10.0,
    )
    # Post-merge shape — real module returns the persisted record.
    assert "emitted" not in ev
    assert ev["event_type"] == "payment.received"
    assert ev["workcell"] == "finance"
    assert ev["prospect_id"] == "p_1"
    assert ev["revenue_usd"] == 10.0
    assert ev["metadata"] == {}
    # Real module also stamps identity + trace.
    assert ev.get("event_id")
    assert ev.get("ts")


def test_read_returns_persisted_events_and_available_true():
    """Post-merge: the real stream is importable and returns what was emitted."""
    emit_business_event(
        "email.sent", workcell="outreach", prospect_id="p_read",
    )
    events = read_events(prospect_id="p_read")
    assert events_available() is True
    assert len(events) >= 1
    assert any(e.get("event_type") == "email.sent" for e in events)


def _install_fake(monkeypatch, *, emit=None, read=None):
    mod = types.ModuleType("backend.common.business_events")
    mod.emit_business_event = emit or (lambda et, **kw: {"event_type": et, **kw})
    mod.read_events = read or (lambda **kw: [{"event_type": "email.sent"}])
    monkeypatch.setitem(sys.modules, "backend.common.business_events", mod)
    return mod


def test_delegates_when_real_module_present(monkeypatch):
    _install_fake(monkeypatch)
    assert events_available() is True
    ev = emit_business_event("call.placed", workcell="voice", cost_usd=0.3)
    assert ev == {
        "event_type": "call.placed", "workcell": "voice", "prospect_id": None,
        "opportunity_id": None, "campaign_id": None, "variant_arm_id": None,
        "cost_usd": 0.3, "revenue_usd": None, "metadata": None,
    }
    assert read_events() == [{"event_type": "email.sent"}]


def test_broken_real_module_degrades_per_call(monkeypatch):
    """A broken (or misbehaving) real module must not crash callers.

    The shim catches every emit/read exception and either returns the pre-merge
    no-op event shape (for emit) or an empty list (for read). This is the same
    fail-soft contract ``llm_budget`` / ``bandit_store`` follow.
    """
    def boom(*a, **kw):
        raise RuntimeError("stream down")

    _install_fake(monkeypatch, emit=boom, read=boom)
    ev = emit_business_event("email.sent", workcell="outreach")
    assert ev["emitted"] is False  # fell back to the no-op shape
    assert ev["event_type"] == "email.sent"
    assert read_events() == []
