"""GAP-9 — fast-ACK for the Vapi ``end-of-call-report`` webhook.

ROOT CAUSE these tests guard against: Vapi only waits ``server.timeoutSeconds``
(=20s live) for ``/vapi/webhook`` to ACK an ``end-of-call-report`` and does NOT
retry a timed-out report. The end-of-call branch makes several sequential
awaited HTTP round-trips (memory + 2x CRM + WordPress) before returning, so a
slow peer pushes the ACK past 20s and the terminal report is lost.

Fix: when ``SAMUS_VOICE_WEBHOOK_FAST_ACK`` is on, the handler returns 200
immediately (``memory_dispatch_error == "accepted_async"``) and runs the heavy
work in a detached background task. These tests assert the ACK is prompt even
when downstream dispatch is slow, that the background work still runs, and that
a re-delivered report is deduped.
"""

from __future__ import annotations

import asyncio
import time

import pytest


def _override_settings(
    monkeypatch, *, memory_url="http://samus-memory:8080", crm_url="http://samus-crm:8080"
):
    class _S:
        pass

    s = _S()
    s.vapi_api_key = "vapi_x"
    s.shared_hmac_key = "test-hmac-32"
    s.vapi_inbound_assistant_id = ""
    s.vapi_inbound_phone_number_id = ""
    gw: dict[str, str] = {}
    if memory_url:
        gw["memory"] = memory_url
    if crm_url:
        gw["crm"] = crm_url
    s.gateway_urls = gw
    import backend.voice.service as svc_mod

    monkeypatch.setattr(svc_mod, "get_settings", lambda: s)


def _eoc_event(call_id="call_fastack_1"):
    from backend.voice.models import VapiWebhookEvent

    return VapiWebhookEvent.model_validate(
        {
            "message": {
                "type": "end-of-call-report",
                "call": {"id": call_id, "status": "ended", "metadata": {"prospect_id": "p1"}},
                "endedReason": "customer-ended-call",
                "summary": "they liked the pitch",
                "recordingUrl": "https://vapi.example/r1.mp3",
                "structuredData": {
                    "lead_summary": {
                        "company": "Acme",
                        "intent_score": 82,
                        "tier": "high",
                        "recommended_action": "book_call",
                    }
                },
            }
        }
    )


def _reset_dedupe():
    import backend.voice.service as svc_mod

    svc_mod._PROCESSED_CALL_IDS = None


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setenv("SAMUS_VOICE_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("SAMUS_VOICE_EVENTS_PATH", str(tmp_path / "events.jsonl"))
    _reset_dedupe()
    yield
    _reset_dedupe()


def test_fast_ack_returns_promptly_even_with_slow_downstream(monkeypatch):
    """The webhook ACKs in well under Vapi's 20s timeout even when every
    downstream dispatch sleeps for ~2s. Without fast-ACK the four sequential
    dispatches would serialize into ~8s+ of latency before the 200."""
    monkeypatch.setenv("SAMUS_VOICE_WEBHOOK_FAST_ACK", "1")
    _override_settings(monkeypatch)

    ran: dict[str, int] = {"calls": 0}

    class _Resp:
        status_code = 200
        text = '{"status":"ok"}'

    async def _slow_post(base_url, path, payload, **kwargs):
        ran["calls"] += 1
        await asyncio.sleep(2.0)  # simulate a slow peer workcell
        return _Resp()

    import backend.voice.service as svc_mod

    monkeypatch.setattr(svc_mod, "signed_post_json", _slow_post)
    # Neutralise the other best-effort side-channels so the test is hermetic.
    monkeypatch.setattr(svc_mod, "index_outbound_call", lambda **k: None)
    monkeypatch.setattr(
        svc_mod, "submit_product_page", lambda **k: {"status": "skipped_existing_sku"}
    )

    from backend.voice.service import handle_webhook_event

    async def _drive():
        t0 = time.monotonic()
        result = await handle_webhook_event(_eoc_event())
        ack_latency = time.monotonic() - t0
        # Let the detached background task run to completion.
        await asyncio.sleep(2.5)
        return result, ack_latency

    result, ack_latency = asyncio.run(_drive())

    # ACK was prompt — orders of magnitude under the 20s Vapi timeout, and
    # under even a single 2s downstream call.
    assert ack_latency < 1.0, f"ACK too slow: {ack_latency:.2f}s"
    assert result.received is True
    assert result.message_type == "end-of-call-report"
    assert result.memory_dispatch_error == "accepted_async"
    # Background work actually fired (memory + 2 CRM dispatches).
    assert ran["calls"] >= 1


def test_fast_ack_dedupes_redelivered_report(monkeypatch):
    """A second delivery of the same call_id is ACKed but not re-processed."""
    monkeypatch.setenv("SAMUS_VOICE_WEBHOOK_FAST_ACK", "1")
    _override_settings(monkeypatch)

    class _Resp:
        status_code = 200
        text = "{}"

    async def _post(*a, **k):
        return _Resp()

    import backend.voice.service as svc_mod

    monkeypatch.setattr(svc_mod, "signed_post_json", _post)
    monkeypatch.setattr(svc_mod, "index_outbound_call", lambda **k: None)
    monkeypatch.setattr(
        svc_mod, "submit_product_page", lambda **k: {"status": "skipped_existing_sku"}
    )

    from backend.voice.service import handle_webhook_event

    async def _drive():
        first = await handle_webhook_event(_eoc_event("dup_call"))
        second = await handle_webhook_event(_eoc_event("dup_call"))
        await asyncio.sleep(0.1)
        return first, second

    first, second = asyncio.run(_drive())
    assert first.memory_dispatch_error == "accepted_async"
    assert second.memory_dispatch_error == "duplicate_already_processed"


def test_sync_path_preserved_when_fast_ack_disabled(monkeypatch):
    """With fast-ACK explicitly off, the handler runs synchronously and returns
    the real dispatch result — the pre-GAP-9 contract the existing suite relies
    on."""
    monkeypatch.setenv("SAMUS_VOICE_WEBHOOK_FAST_ACK", "0")
    _override_settings(monkeypatch)

    class _Resp:
        status_code = 200
        text = '{"status":"ok"}'

    async def _post(*a, **k):
        return _Resp()

    import backend.voice.service as svc_mod

    monkeypatch.setattr(svc_mod, "signed_post_json", _post)
    monkeypatch.setattr(svc_mod, "index_outbound_call", lambda **k: None)
    monkeypatch.setattr(
        svc_mod, "submit_product_page", lambda **k: {"status": "skipped_existing_sku"}
    )

    from backend.voice.service import handle_webhook_event

    result = asyncio.run(handle_webhook_event(_eoc_event("sync_call")))
    # Real result, not the async sentinel.
    assert result.memory_dispatch_error != "accepted_async"
    assert result.memory_dispatch_ok is True
    assert result.crm_dispatch_ok is True
    assert result.lead_summary is not None


def test_non_terminal_event_unaffected_by_fast_ack(monkeypatch):
    """status-update / transcript events never went through the slow branch and
    still ACK with the trivial log-and-drop result."""
    monkeypatch.setenv("SAMUS_VOICE_WEBHOOK_FAST_ACK", "1")
    _override_settings(monkeypatch)
    from backend.voice.service import handle_webhook_event
    from backend.voice.models import VapiWebhookEvent

    event = VapiWebhookEvent.model_validate(
        {"message": {"type": "status-update", "call": {"id": "c9"}}}
    )
    result = asyncio.run(handle_webhook_event(event))
    assert result.received is True
    assert result.message_type == "status-update"
    assert result.memory_dispatch_error is None
