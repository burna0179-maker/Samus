"""Low-level SQS poll loop. Decoupled from worker_base for testability."""
from __future__ import annotations

import logging
import threading
from typing import Any, Callable

from . import sqs
from .reconnect_policy import Backoff

_LOG = logging.getLogger("samus.sqs_worker")


def poll_loop(
    *,
    service: str,
    handler: Callable[[dict[str, Any]], None],
    stop_event: threading.Event,
    on_idle: Callable[[], None] | None = None,
) -> None:
    """Long-poll SQS, call ``handler`` per message, retry on transient errors."""
    backoff = Backoff(base_s=1.0, max_s=15.0)
    while not stop_event.is_set():
        try:
            messages = sqs.receive(service)
            if not messages:
                if on_idle:
                    try:
                        on_idle()
                    except Exception as exc:
                        _LOG.warning("on_idle_failed", extra={"err": str(exc)})
                backoff.reset()
                continue
            backoff.reset()
            for msg in messages:
                if stop_event.is_set():
                    break
                handler(msg)
        except sqs.QueueNotConfigured:
            _LOG.error("queue_not_configured", extra={"service": service})
            stop_event.wait(5.0)
        except Exception as exc:
            delay = backoff.next_delay()
            jitter = 0.25 * delay
            _LOG.warning(
                "poll_iteration_failed",
                extra={"service": service, "err": str(exc), "delay_s": delay},
            )
            stop_event.wait(delay + jitter)
        # Tight loops are fine â SQS long-poll provides natural pacing.
