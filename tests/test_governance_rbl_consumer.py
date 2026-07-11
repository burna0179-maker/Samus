"""Coverage for the RBL band consume loop: ingest (producer) + consumer + handler.

The commercial wrap's ``RblBandConsumer`` reads a local cache file as its
fallback when no live HTTP status endpoint is configured (the default local
deployment). Before this, NOTHING wrote that cache, so the consumer always
defaulted to ``healthy`` — the RBL signal was never actually consumed. These
tests pin the now-complete loop:

  * ``ingest_rbl_band`` writes the cache and is fail-closed on unknown bands,
  * ``RblBandConsumer.current_band`` reads the ingested band back,
  * the ingested band actually gates a commercial action (critical/freeze),
  * the hub-event handler bridges a Major RBL broadcast into the cache, and
    refuses non-Major / non-RBL / bandless / forged-band events.
"""

from __future__ import annotations

import json

import pytest

from backend.governance.commercial_wrap import wrap
from backend.governance.commercial_wrap.wrap import (
    CommercialActionRefusal,
    RblBandConsumer,
    commit_commercial_action,
    ingest_rbl_band,
)
from backend.standard.inter_agent import rbl_band_handler


@pytest.fixture(autouse=True)
def _rbl_cache_to_tmp(tmp_path, monkeypatch):
    """Redirect the RBL cache file off the real state/ tree."""
    monkeypatch.setattr(wrap, "_RBL_CACHE", tmp_path / "rbl" / "current_band.json")


class _PassEFH:
    def evaluate(self, proposed: dict) -> None:
        return None


_LLM_PAYLOAD = {"estimated_cost_usd": 0.02, "purpose": "unit-test"}


# --- ingest (producer) ------------------------------------------------------


def test_ingest_writes_cache_and_consumer_reads_it():
    assert ingest_rbl_band("critical", source="test") is True
    assert wrap._RBL_CACHE.is_file()
    doc = json.loads(wrap._RBL_CACHE.read_text(encoding="utf-8"))
    assert doc["current_band"] == "critical"
    # No HTTP url -> consumer falls back to the cache we just wrote.
    assert RblBandConsumer().current_band() == "critical"


def test_ingest_rejects_unknown_band_fail_closed():
    # Seed a real freeze first.
    assert ingest_rbl_band("freeze", source="test") is True
    # A forged/garbled band must NOT overwrite (can't clear a freeze).
    assert ingest_rbl_band("totally_fake_band", source="attacker") is False
    assert RblBandConsumer().current_band() == "freeze"


def test_ingest_atomic_write_leaves_no_tmp():
    ingest_rbl_band("warning", source="test")
    tmp = wrap._RBL_CACHE.with_suffix(wrap._RBL_CACHE.suffix + ".tmp")
    assert not tmp.exists()


# --- consumer gates a real action -------------------------------------------


def test_freeze_band_blocks_llm_call():
    ingest_rbl_band("freeze", source="test")
    consumer = RblBandConsumer()
    with pytest.raises(CommercialActionRefusal) as ei:
        commit_commercial_action(
            action_class="llm_call_above_threshold",
            action_payload=dict(_LLM_PAYLOAD),
            commercial_destination="unit-test",
            isv_consumer=None,
            template_registry=None,
            efh_evaluator=_PassEFH(),
            dual_channel=None,
            rbl_consumer=consumer,
        )
    assert "rbl_band_freeze_blocks_llm_call_above_threshold" in str(ei.value)


def test_healthy_band_allows_llm_call(tmp_path, monkeypatch):
    ingest_rbl_band("healthy", source="test")
    # VER sink off the real tree too.
    monkeypatch.setattr(wrap, "_VER_DIR", tmp_path / "ver")
    rec = commit_commercial_action(
        action_class="llm_call_above_threshold",
        action_payload=dict(_LLM_PAYLOAD),
        commercial_destination="unit-test",
        isv_consumer=None,
        template_registry=None,
        efh_evaluator=_PassEFH(),
        dual_channel=None,
        rbl_consumer=RblBandConsumer(),
    )
    assert rec["status"] == "committed"
    assert rec["rbl_band_at_commit"] == "healthy"


# --- hub-event handler (inbound bridge) -------------------------------------


def test_handler_ingests_major_rbl_event():
    event = {
        "id": "evt-1",
        "caller": "major",
        "action": "rbl_band_change",
        "current_band": "critical",
        "ts": 123.0,
    }
    rbl_band_handler.rbl_band_event_handler(event)
    assert RblBandConsumer().current_band() == "critical"


def test_handler_reads_band_from_nested_payload():
    event = {
        "id": "evt-2",
        "caller": "major",
        "action": "rbl_status",
        "payload": {"band": "warning"},
    }
    rbl_band_handler.rbl_band_event_handler(event)
    assert RblBandConsumer().current_band() == "warning"


def test_handler_ignores_non_major_caller():
    event = {"id": "x", "caller": "anita", "action": "rbl_band_change", "current_band": "freeze"}
    rbl_band_handler.rbl_band_event_handler(event)
    assert not wrap._RBL_CACHE.is_file()


def test_handler_ignores_non_rbl_action():
    event = {"id": "x", "caller": "major", "action": "quorum_vote", "current_band": "freeze"}
    rbl_band_handler.rbl_band_event_handler(event)
    assert not wrap._RBL_CACHE.is_file()


def test_handler_ignores_bandless_event():
    event = {"id": "x", "caller": "major", "action": "rbl_band_change"}
    rbl_band_handler.rbl_band_event_handler(event)
    assert not wrap._RBL_CACHE.is_file()


def test_handler_ignores_forged_band(monkeypatch):
    # Real freeze cached, then a major event with a garbage band must not clear it.
    ingest_rbl_band("freeze", source="test")
    event = {
        "id": "x",
        "caller": "major",
        "action": "rbl_band_change",
        "current_band": "not_a_band",
    }
    rbl_band_handler.rbl_band_event_handler(event)
    assert RblBandConsumer().current_band() == "freeze"


def test_register_is_idempotent():
    from backend.standard.inter_agent import event_handler

    event_handler._reset_for_tests()
    rbl_band_handler.register_rbl_band_handler()
    rbl_band_handler.register_rbl_band_handler()
    assert event_handler._handlers.count(rbl_band_handler.rbl_band_event_handler) == 1
    event_handler._reset_for_tests()
