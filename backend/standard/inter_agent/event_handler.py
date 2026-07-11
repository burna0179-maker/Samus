"""Handler registry for inbound Quorum Hub events.

Modules anywhere in Samus can register a callable that fires on every
inbound hub event. The dispatcher invokes handlers in registration
order; an exception raised by one handler is logged and never blocks
the others or the SSE consumer loop.

Event shape (matches ``_shared/quorum_hub/hub.py:QuorumEvent.to_dict``)::

    {
      "id": "<uuid4>",
      "ts": <float epoch>,
      "caller": "anita" | "darwin" | "major" | "optimus" | "samus" | ...,
      "action": "<verb>",
      "risk_score": <float 0..1>,
      "approved": <bool>,
      "approval_score": <float>,
      "threshold": <float>,
      "votes": [<per-voter dicts>],
      "reason": "<str>",
    }
"""
from __future__ import annotations

import logging
from typing import Any, Callable, List

_LOG = logging.getLogger("samus.inter_agent.event_handler")

HubEventHandler = Callable[[dict[str, Any]], None]

_handlers: List[HubEventHandler] = []


def register_handler(handler: HubEventHandler) -> None:
    """Register a handler invoked on every hub event.

    Idempotent by identity — registering the same callable twice is a
    no-op. There's no ordering guarantee between handlers; assume any
    order.
    """
    if handler not in _handlers:
        _handlers.append(handler)


def unregister_handler(handler: HubEventHandler) -> None:
    """Remove a previously-registered handler. No-op if not present."""
    try:
        _handlers.remove(handler)
    except ValueError:
        pass


def dispatch(event: dict[str, Any]) -> None:
    """Invoke every registered handler with the event. Best-effort.

    Iterates a snapshot of the handler list so a handler that
    (un)registers another during dispatch doesn't corrupt the loop.
    Exceptions are logged and swallowed — one bad handler MUST NOT take
    down the SSE consumer or the rest of the handlers.
    """
    for h in list(_handlers):
        try:
            h(event)
        except Exception:  # noqa: BLE001 — handlers must not crash the subscriber
            _LOG.exception(
                "hub event handler %r raised on event id=%s",
                getattr(h, "__qualname__", repr(h)),
                event.get("id"),
            )


def _default_log_handler(event: dict[str, Any]) -> None:
    """Default handler — logs each event at INFO. Always registered."""
    _LOG.info(
        "hub_event id=%s caller=%s action=%s approved=%s score=%.2f/%.2f",
        event.get("id"),
        event.get("caller"),
        event.get("action"),
        event.get("approved"),
        float(event.get("approval_score") or 0.0),
        float(event.get("threshold") or 0.0),
    )


def _reset_for_tests() -> None:
    """Test-only handler list reset."""
    _handlers.clear()


__all__ = [
    "HubEventHandler",
    "dispatch",
    "register_handler",
    "unregister_handler",
]
