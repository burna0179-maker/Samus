"""Tests for backend.standard.inter_agent.

Async tests follow the Samus convention (see ``test_common_retry.py``):
synchronous test functions that wrap their body in ``asyncio.run(...)`` —
no pytest-asyncio dependency.

The SSE side is exercised against a real ASGI FastAPI test server reached
via httpx.AsyncClient(transport=httpx.ASGITransport(...)). No network.
"""

from __future__ import annotations

import asyncio
import json
from typing import AsyncIterator, List

import httpx
from fastapi import FastAPI
from fastapi.responses import StreamingResponse

from backend.standard.inter_agent import (
    HubSubscriber,
    event_handler,
    hub_subscriber as hub_subscriber_mod,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sse_event(payload: dict) -> str:
    return f"data: {json.dumps(payload, separators=(',', ':'))}\n\n"


def _build_hub_app(events: List[dict], *, disconnect_after_all: bool = True) -> FastAPI:
    """Tiny FastAPI app whose /events endpoint emits ``events`` then closes."""
    app = FastAPI()

    @app.get("/events")
    async def stream() -> StreamingResponse:
        async def _gen() -> AsyncIterator[str]:
            for ev in events:
                yield _sse_event(ev)
                await asyncio.sleep(0)
            if disconnect_after_all:
                return
            while True:
                await asyncio.sleep(60)

        return StreamingResponse(_gen(), media_type="text/event-stream")

    return app


def _client_factory_for(app: FastAPI):
    def _factory() -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://hub.test",
            timeout=httpx.Timeout(None, connect=5.0),
        )

    return _factory


async def _drain_until(predicate, *, timeout: float = 3.0, step: float = 0.05) -> None:
    """Spin the event loop until predicate() is true or timeout."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(step)


# ---------------------------------------------------------------------------
# event_handler unit tests (sync)
# ---------------------------------------------------------------------------


def test_dispatch_invokes_registered_handlers():
    event_handler._reset_for_tests()
    received: List[dict] = []
    event_handler.register_handler(received.append)
    event_handler.dispatch({"id": "a", "caller": "anita"})
    assert received == [{"id": "a", "caller": "anita"}]


def test_register_handler_is_idempotent_by_identity():
    event_handler._reset_for_tests()
    received: List[dict] = []

    def h(e: dict) -> None:
        received.append(e)

    event_handler.register_handler(h)
    event_handler.register_handler(h)
    event_handler.dispatch({"id": "x"})
    assert len(received) == 1


def test_dispatch_isolates_handler_exceptions():
    event_handler._reset_for_tests()
    received: List[dict] = []

    def bad(_: dict) -> None:
        raise RuntimeError("boom")

    def good(e: dict) -> None:
        received.append(e)

    event_handler.register_handler(bad)
    event_handler.register_handler(good)
    event_handler.dispatch({"id": "x"})  # must not raise
    assert received == [{"id": "x"}]


def test_unregister_handler_no_op_when_absent():
    event_handler._reset_for_tests()
    event_handler.unregister_handler(lambda e: None)  # must not raise


# ---------------------------------------------------------------------------
# hub_subscriber tests
# ---------------------------------------------------------------------------


def test_subscriber_dispatches_events_from_sse_stream():
    async def _run() -> List[dict]:
        event_handler._reset_for_tests()
        received: List[dict] = []
        event_handler.register_handler(received.append)

        events = [
            {"id": "e1", "caller": "anita", "action": "vote", "approved": True},
            {"id": "e2", "caller": "major", "action": "poll", "approved": False},
        ]
        app = _build_hub_app(events)
        sub = HubSubscriber(
            url="http://hub.test/events",
            client_factory=_client_factory_for(app),
        )
        await sub.start()
        await _drain_until(lambda: len(received) >= 2)
        await sub.stop()
        return received

    received = asyncio.run(_run())
    assert [e["id"] for e in received] == ["e1", "e2"]


def test_subscriber_reconnects_after_disconnect():
    async def _run() -> tuple[List[dict], int]:
        event_handler._reset_for_tests()
        received: List[dict] = []
        event_handler.register_handler(received.append)

        state = {"call_count": 0}
        events_first = [{"id": "round1"}]
        events_second = [{"id": "round2"}]

        app = FastAPI()

        @app.get("/events")
        async def stream() -> StreamingResponse:
            state["call_count"] += 1
            events = events_first if state["call_count"] == 1 else events_second

            async def _gen() -> AsyncIterator[str]:
                for ev in events:
                    yield _sse_event(ev)
                    await asyncio.sleep(0)

            return StreamingResponse(_gen(), media_type="text/event-stream")

        # Shrink backoff so the test isn't slow.
        monkey_initial = hub_subscriber_mod._INITIAL_BACKOFF_S
        monkey_max = hub_subscriber_mod._MAX_BACKOFF_S
        hub_subscriber_mod._INITIAL_BACKOFF_S = 0.01
        hub_subscriber_mod._MAX_BACKOFF_S = 0.01
        try:
            sub = HubSubscriber(
                url="http://hub.test/events",
                client_factory=_client_factory_for(app),
            )
            await sub.start()
            await _drain_until(lambda: len(received) >= 2, timeout=5.0)
            await sub.stop()
        finally:
            hub_subscriber_mod._INITIAL_BACKOFF_S = monkey_initial
            hub_subscriber_mod._MAX_BACKOFF_S = monkey_max

        return received, state["call_count"]

    received, call_count = asyncio.run(_run())
    ids = [e["id"] for e in received]
    assert "round1" in ids
    assert "round2" in ids
    assert call_count >= 2


def test_subscriber_stop_is_idempotent():
    async def _run() -> bool:
        sub = HubSubscriber(
            url="http://hub.test/events",
            client_factory=_client_factory_for(_build_hub_app([])),
        )
        await sub.stop()  # stop() before start() is a no-op
        await sub.start()
        await sub.stop()
        await sub.stop()  # double-stop must not raise
        return sub.running

    assert asyncio.run(_run()) is False


def test_subscriber_start_is_idempotent():
    async def _run() -> bool:
        sub = HubSubscriber(
            url="http://hub.test/events",
            client_factory=_client_factory_for(_build_hub_app([], disconnect_after_all=False)),
        )
        await sub.start()
        task_first = sub._task  # noqa: SLF001
        await sub.start()
        task_second = sub._task  # noqa: SLF001
        same = task_first is task_second
        await sub.stop()
        return same

    assert asyncio.run(_run()) is True


def test_subscriber_swallows_malformed_payloads():
    async def _run() -> List[dict]:
        event_handler._reset_for_tests()
        received: List[dict] = []
        event_handler.register_handler(received.append)

        app = FastAPI()

        @app.get("/events")
        async def stream() -> StreamingResponse:
            async def _gen() -> AsyncIterator[str]:
                yield "data: not-json\n\n"
                yield _sse_event({"id": "valid"})

            return StreamingResponse(_gen(), media_type="text/event-stream")

        sub = HubSubscriber(
            url="http://hub.test/events",
            client_factory=_client_factory_for(app),
        )
        await sub.start()
        await _drain_until(lambda: bool(received))
        await sub.stop()
        return received

    received = asyncio.run(_run())
    assert [e["id"] for e in received] == ["valid"]


def test_subscriber_ignores_sse_comments_and_non_data_lines():
    async def _run() -> List[dict]:
        event_handler._reset_for_tests()
        received: List[dict] = []
        event_handler.register_handler(received.append)

        app = FastAPI()

        @app.get("/events")
        async def stream() -> StreamingResponse:
            async def _gen() -> AsyncIterator[str]:
                yield ": keep-alive\n\n"
                yield "event: ping\n\n"
                yield "id: 42\n\n"
                yield _sse_event({"id": "real"})

            return StreamingResponse(_gen(), media_type="text/event-stream")

        sub = HubSubscriber(
            url="http://hub.test/events",
            client_factory=_client_factory_for(app),
        )
        await sub.start()
        await _drain_until(lambda: bool(received))
        await sub.stop()
        return received

    received = asyncio.run(_run())
    assert received == [{"id": "real"}]


# ---------------------------------------------------------------------------
# Env-var gates
# ---------------------------------------------------------------------------


def test_is_subscribe_disabled_honours_env(monkeypatch):
    monkeypatch.setenv("QUORUM_HUB_SUBSCRIBE_DISABLED", "1")
    assert hub_subscriber_mod.is_subscribe_disabled() is True
    monkeypatch.setenv("QUORUM_HUB_SUBSCRIBE_DISABLED", "false")
    assert hub_subscriber_mod.is_subscribe_disabled() is False
    monkeypatch.delenv("QUORUM_HUB_SUBSCRIBE_DISABLED", raising=False)
    assert hub_subscriber_mod.is_subscribe_disabled() is False


def test_resolve_url_defaults_and_env_override(monkeypatch):
    monkeypatch.delenv("QUORUM_HUB_SSE_URL", raising=False)
    assert hub_subscriber_mod._resolve_url() == hub_subscriber_mod.DEFAULT_HUB_SSE_URL
    monkeypatch.setenv("QUORUM_HUB_SSE_URL", "http://custom:9000/events")
    assert hub_subscriber_mod._resolve_url() == "http://custom:9000/events"
