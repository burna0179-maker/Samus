"""Retry + circuit breaker primitives per doc §3.14.

Replaces the prior ``circuit_breaker.py`` (CLOSED/OPEN/HALF_OPEN state machine).
The new shape is a per-target ``CircuitState`` dataclass with a module-level
``CIRCUITS`` dict keyed by target URL. ``retry_request`` wraps a callable in
exponential backoff + circuit-aware refusal.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import httpx


@dataclass(slots=True)
class CircuitState:
    failures: int = 0
    threshold: int = 5
    recovery_seconds: float = 30.0
    open_until: float = 0.0  # monotonic timestamp

    def can_call(self) -> bool:
        return self.open_until == 0.0 or time.monotonic() >= self.open_until

    def record_success(self) -> bool:
        """Reset the circuit. Return True iff this was an OPEN->CLOSED recovery."""
        recovered = self.open_until != 0.0
        self.failures = 0
        self.open_until = 0.0
        return recovered

    def record_failure(self) -> bool:
        """Count a failure. Return True iff this failure TRIPPED the circuit
        (CLOSED->OPEN transition — i.e. it was closed and this pushed it open)."""
        was_open = self.open_until != 0.0
        self.failures += 1
        if self.failures >= self.threshold:
            self.open_until = time.monotonic() + self.recovery_seconds
        return (not was_open) and self.open_until != 0.0


CIRCUITS: dict[str, CircuitState] = {}


def _on_health_transition(target: str, *, healthy: bool) -> None:
    """Record a gateway health-state transition to the ledger AND push it.

    A circuit tripping (CLOSED->OPEN) means an upstream target just went
    unhealthy; recovering (OPEN->CLOSED) means it came back. Previously the
    circuit state changed silently in-memory — no ledger trail and no
    real-time push. Both sinks are fail-soft with lazy imports (this runs on
    the request hot path and must never break a call). Severity: unhealthy is
    critical, recovery is info. dedup_key is per target+direction so a flapping
    target pages once per window, not per retry.
    """
    state = "healthy" if healthy else "unhealthy"
    try:
        from . import audit
        audit.record("gateway.health.transition", target=target, state=state)
    except Exception:  # noqa: BLE001 - transition side effects must never break the call
        pass
    try:
        from .notify import notify_operator
        if healthy:
            notify_operator(
                "Gateway target recovered",
                f"circuit CLOSED — {target} is healthy again",
                severity="info",
                dedup_key=f"health_recover:{target}",
            )
        else:
            notify_operator(
                "Gateway target unhealthy",
                f"circuit OPEN — {target} failed repeatedly and is now shed",
                severity="critical",
                dedup_key=f"health_unhealthy:{target}",
            )
    except Exception:  # noqa: BLE001 - notify must never break the call
        pass


def _get_circuit(target: str) -> CircuitState:
    circuit = CIRCUITS.get(target)
    if circuit is None:
        circuit = CircuitState()
        CIRCUITS[target] = circuit
    return circuit


async def retry_request(
    client_call: Callable[[], Awaitable[Any]],
    target: str,
    *,
    max_retries: int = 3,
    base_delay: float = 0.5,
    idempotent: bool = True,
) -> Any:
    """Call ``client_call`` with exponential backoff + circuit-aware refusal.

    Raises ``httpx.HTTPStatusError`` (with a 503 response) if the circuit is open.
    """
    circuit = _get_circuit(target)
    if not circuit.can_call():
        raise httpx.HTTPStatusError(
            f"circuit open for target {target}",
            request=httpx.Request("POST", target),
            response=httpx.Response(503, request=httpx.Request("POST", target)),
        )

    last_exc: BaseException | None = None
    for attempt in range(max_retries + 1):
        try:
            result = await client_call()
            if circuit.record_success():
                _on_health_transition(target, healthy=True)
            return result
        except (httpx.TimeoutException, httpx.ConnectError) as exc:
            last_exc = exc
            if circuit.record_failure():
                _on_health_transition(target, healthy=False)
            if not idempotent or attempt >= max_retries:
                break
            delay = base_delay * (2 ** attempt)
            await asyncio.sleep(delay)
        except Exception as exc:
            if circuit.record_failure():
                _on_health_transition(target, healthy=False)
            raise exc

    assert last_exc is not None
    raise last_exc


def reset_circuits() -> None:
    """Test helper — clear all circuits."""
    CIRCUITS.clear()
