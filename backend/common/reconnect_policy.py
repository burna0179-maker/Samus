"""Exponential backoff for transient AWS / SQS errors.

Referenced by sqs_worker.poll_loop. Separated so it can be tested
independently and reused by any reconnecting client.
"""

from __future__ import annotations

import random


class Backoff:
    """Exponential backoff with optional jitter.

    Usage::

        b = Backoff(base_s=1.0, max_s=15.0)
        while True:
            try:
                do_something()
                b.reset()
            except TransientError:
                delay = b.next_delay()
                time.sleep(delay + random.uniform(0, 0.25 * delay))
    """

    def __init__(self, base_s: float = 1.0, max_s: float = 15.0) -> None:
        self._base = base_s
        self._max = max_s
        self._attempt = 0

    def next_delay(self) -> float:
        """Return the next backoff delay in seconds and advance the attempt counter."""
        delay = min(self._base * (2.0**self._attempt), self._max)
        self._attempt += 1
        return delay

    def jitter(self, delay: float, pct: float = 0.25) -> float:
        """Return ``delay`` ± up to ``pct`` random spread."""
        return delay + random.uniform(0, pct * delay)

    def reset(self) -> None:
        """Call after a successful operation to restart from base delay."""
        self._attempt = 0

    @property
    def attempt(self) -> int:
        return self._attempt
