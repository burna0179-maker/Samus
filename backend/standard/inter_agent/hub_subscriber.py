"""Long-lived SSE consumer for the cross-agent Quorum Hub.

Opens ``GET /events`` on the hub (default
``http://host.docker.internal:8090/events`` from inside the container),
parses each ``data: <json>`` line, and dispatches the parsed dict to
:func:`event_handler.dispatch`. Auto-reconnects on disconnect with
capped exponential backoff.

The hub's ``/events`` endpoint is NOT HMAC-gated in the current server
implementation — the middleware only guards ``POST /mcp``. So this
subscriber needs no signing material today. If the hub adds auth later,
add header injection here.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Callable, Optional

import httpx

from .event_handler import dispatch

_LOG = logging.getLogger("samus.inter_agent.hub_subscriber")

DEFAULT_HUB_SSE_URL = "http://host.docker.internal:8090/events"
ENV_HUB_SSE_URL = "QUORUM_HUB_SSE_URL"
ENV_HUB_DISABLED = "QUORUM_HUB_SUBSCRIBE_DISABLED"

_INITIAL_BACKOFF_S = 1.0
_MAX_BACKOFF_S = 60.0
_BACKOFF_MULTIPLIER = 2.0

# Type alias: () -> httpx.AsyncClient. Tests inject a stub client.
ClientFactory = Callable[[], httpx.AsyncClient]


def _resolve_url() -> str:
    return os.environ.get(ENV_HUB_SSE_URL, "").strip() or DEFAULT_HUB_SSE_URL


def is_subscribe_disabled() -> bool:
    """Honoured by the lifespan wiring to skip starting the subscriber."""
    return os.environ.get(ENV_HUB_DISABLED, "").strip().lower() in (
        "1",
        "true",
        "yes",
    )


class HubSubscriber:
    """Long-lived SSE consumer + reconnect loop.

    Lifecycle::

        sub = HubSubscriber()
        await sub.start()         # spawns the consume task
        ...
        await sub.stop()          # signals shutdown + awaits cleanup

    Both methods are idempotent: start() on a running subscriber is a
    no-op; stop() on a stopped subscriber is a no-op. The consume loop
    catches every transport exception and retries with backoff, so a
    never-reachable hub manifests as periodic 'retrying' logs, never a
    crash.
    """

    def __init__(
        self,
        *,
        url: Optional[str] = None,
        client_factory: Optional[ClientFactory] = None,
        dispatcher: Optional[Callable[[dict], None]] = None,
    ) -> None:
        self._url = url or _resolve_url()
        # No request-level timeout — SSE is a long-lived stream. Connect
        # timeout is kept short so a dead hub fails fast at connect, not
        # mid-read.
        self._client_factory: ClientFactory = client_factory or (
            lambda: httpx.AsyncClient(timeout=httpx.Timeout(None, connect=5.0))
        )
        self._dispatch = dispatcher or dispatch
        self._task: Optional[asyncio.Task[None]] = None
        self._stop_event = asyncio.Event()

    @property
    def url(self) -> str:
        return self._url

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        """Spawn the consume task. No-op if already running."""
        if self.running:
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run(), name="samus-hub-subscriber")

    async def stop(self, timeout: float = 5.0) -> None:
        """Signal shutdown, cancel the consume task, await cleanup."""
        if self._task is None:
            return
        self._stop_event.set()
        task = self._task
        self._task = None
        task.cancel()
        try:
            await asyncio.wait_for(task, timeout=timeout)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass

    async def _run(self) -> None:
        """Reconnect loop. Runs until stop() is called."""
        backoff = _INITIAL_BACKOFF_S
        while not self._stop_event.is_set():
            try:
                await self._consume_once()
                # Clean server-side disconnect — reset backoff for prompt
                # reconnect.
                backoff = _INITIAL_BACKOFF_S
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — log + retry every transport failure
                _LOG.warning(
                    "hub SSE connection error: %s; retrying in %.1fs",
                    exc,
                    backoff,
                )
            if self._stop_event.is_set():
                return
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * _BACKOFF_MULTIPLIER, _MAX_BACKOFF_S)

    async def _consume_once(self) -> None:
        """Open one SSE connection and consume until server disconnects."""
        async with self._client_factory() as client:
            async with client.stream("GET", self._url) as response:
                response.raise_for_status()
                _LOG.info("hub SSE connected at %s", self._url)
                async for line in response.aiter_lines():
                    if self._stop_event.is_set():
                        return
                    if not line or line.startswith(":"):
                        # SSE comments (": keep-alive") and empty lines.
                        continue
                    if not line.startswith("data:"):
                        # Other SSE fields (event:, id:, retry:) — we
                        # don't use them today; skip without warning.
                        continue
                    payload = line[len("data:") :].strip()
                    if not payload:
                        continue
                    try:
                        event = json.loads(payload)
                    except json.JSONDecodeError:
                        _LOG.warning(
                            "malformed SSE payload: %s%s",
                            payload[:80],
                            "…" if len(payload) > 80 else "",
                        )
                        continue
                    if isinstance(event, dict):
                        self._dispatch(event)


# ---------------------------------------------------------------------------
# Module-level singleton — one subscriber per process.
# ---------------------------------------------------------------------------

_subscriber: Optional[HubSubscriber] = None


def get_subscriber() -> HubSubscriber:
    """Return the process-wide HubSubscriber singleton."""
    global _subscriber  # noqa: PLW0603
    if _subscriber is None:
        _subscriber = HubSubscriber()
    return _subscriber


def _reset_for_tests() -> None:
    """Test-only singleton reset."""
    global _subscriber  # noqa: PLW0603
    _subscriber = None


__all__ = [
    "ClientFactory",
    "DEFAULT_HUB_SSE_URL",
    "ENV_HUB_DISABLED",
    "ENV_HUB_SSE_URL",
    "HubSubscriber",
    "get_subscriber",
    "is_subscribe_disabled",
]
