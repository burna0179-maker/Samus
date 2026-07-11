"""Growth B-F action scheduler — deferred and recurring job execution.

Queues growth actions for deferred or recurring execution (e.g. "post
social calendar every Monday"). Backed by an in-memory store; no DB
dependency is required at this stage. APScheduler (or any cron runner)
can drive ``_tick()`` externally.

Flag architecture
-----------------
``SAMUS_GROWTH_SCHEDULER_ENABLED`` (env var, default **False**).

When the flag is OFF:
  - :meth:`GrowthScheduler.schedule` raises :exc:`GrowthSchedulerDisabledError`.
  - All other methods work as expected (cancel/list on an empty store).

When the flag is ON each schedule() call also validates:
  1. The action is known in the :class:`~backend.growth.schema_registry.GrowthActionSchema`
     registry.
  2. The action's group flag is enabled via
     :func:`~backend.growth.dispatch_policy.is_enabled`.
  3. The payload satisfies the schema's required-inputs list.

Raises
------
GrowthSchedulerDisabledError
    Scheduler flag is OFF.
GrowthSchedulerError
    Action is unknown, action's group flag is disabled, or payload
    fails schema validation.
"""
from __future__ import annotations

import logging
import os
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from backend.growth.dispatch_policy import GrowthDispatchEntry  # pragma: no cover
    from backend.growth.schema_registry import GrowthActionSchema    # pragma: no cover

_LOG = logging.getLogger("samus.growth.scheduler")

# ---------------------------------------------------------------------------
# Flag resolution
# ---------------------------------------------------------------------------

_FLAG = "SAMUS_GROWTH_SCHEDULER_ENABLED"


def _scheduler_enabled() -> bool:
    """Return True when SAMUS_GROWTH_SCHEDULER_ENABLED is truthy in the env."""
    raw = os.environ.get(_FLAG, "").strip().lower()
    return raw in ("1", "true", "yes", "on", "y")


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class GrowthSchedulerError(RuntimeError):
    """Raised when schedule() cannot enqueue a job (unknown/disabled action,
    schema validation failure).
    """


class GrowthSchedulerDisabledError(GrowthSchedulerError):
    """Raised when the scheduler flag is OFF and schedule() is called."""


# ---------------------------------------------------------------------------
# Job spec
# ---------------------------------------------------------------------------


@dataclass
class GrowthJobSpec:
    """Specification for a scheduled growth action.

    Attributes:
        job_id:     Unique identifier. Auto-generated if not supplied.
        action:     Growth action verb (must be in the dispatch table).
        payload:    Input payload forwarded to the action handler.
        run_at:     One-time execution time (UTC). ``None`` = run immediately
                    on the next ``_tick()``.
        recurrence: Cron expression for recurring jobs (e.g. ``"0 9 * * 1"``
                    for every Monday at 09:00). ``None`` = one-shot.
        enabled:    When False the job is stored but never ticked.
    """

    job_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    action: str = ""
    payload: dict = field(default_factory=dict)
    run_at: datetime | None = None
    recurrence: str | None = None
    enabled: bool = False


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------


class GrowthScheduler:
    """In-memory scheduler for growth actions.

    Instantiate with the dispatch policy module and schema registry module
    (or any objects that expose the required surface — dependency injection
    makes unit testing straightforward without mocking global state).

    Parameters
    ----------
    dispatch_policy:
        An object / module that exposes ``is_enabled(action: str) -> bool``.
        Typically :mod:`backend.growth.dispatch_policy`.
    schema_registry:
        An object / module that exposes
        ``get_schema(action: str) -> GrowthActionSchema | None``.
        Typically :mod:`backend.growth.schema_registry`.
    """

    def __init__(self, dispatch_policy, schema_registry) -> None:  # noqa: ANN001
        self._policy = dispatch_policy
        self._registry = schema_registry
        self._jobs: dict[str, GrowthJobSpec] = {}
        self._completed: set[str] = set()
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def schedule(self, spec: GrowthJobSpec) -> str:
        """Validate and enqueue a growth job.

        Parameters
        ----------
        spec:
            The job specification. ``spec.job_id`` is used as-is; callers
            should leave it at its default (auto-generated UUID) unless they
            need a deterministic id.

        Returns
        -------
        str
            The ``job_id`` of the enqueued job.

        Raises
        ------
        GrowthSchedulerDisabledError
            When ``SAMUS_GROWTH_SCHEDULER_ENABLED`` is not truthy.
        GrowthSchedulerError
            When the action is unknown, its group flag is disabled, or the
            payload is missing required fields.
        """
        if not _scheduler_enabled():
            raise GrowthSchedulerDisabledError(
                f"Cannot schedule action={spec.action!r}: "
                f"{_FLAG} is not enabled."
            )

        # --- action known? -------------------------------------------------
        schema = self._registry.get_schema(spec.action)
        if schema is None:
            raise GrowthSchedulerError(
                f"Unknown growth action: {spec.action!r}. "
                "Not present in GROWTH_SCHEMA_REGISTRY."
            )

        # --- group flag enabled? ------------------------------------------
        if not self._policy.is_enabled(spec.action):
            entry = None
            try:
                entry = self._policy.get_entry(spec.action)
            except Exception:  # noqa: BLE001
                pass
            flag = entry.flag if entry else "unknown"
            raise GrowthSchedulerError(
                f"Growth action {spec.action!r} is disabled "
                f"(group flag {flag!r} is OFF)."
            )

        # --- payload validation -------------------------------------------
        missing = schema.validate(spec.payload)
        if missing:
            raise GrowthSchedulerError(
                f"Payload for action {spec.action!r} is missing required "
                f"fields: {missing}."
            )

        with self._lock:
            self._jobs[spec.job_id] = spec

        _LOG.debug(
            "growth_scheduler: enqueued job_id=%s action=%s run_at=%s recurrence=%s",
            spec.job_id, spec.action, spec.run_at, spec.recurrence,
        )
        return spec.job_id

    def list_pending(self) -> list[GrowthJobSpec]:
        """Return all scheduled jobs that have not yet completed.

        This includes:
        - One-shot jobs whose ``run_at`` is in the future (or None / immediate).
        - Recurring jobs (recurrence is never "completed").
        - Disabled jobs (``enabled=False``) that are stored but not ticked.

        Returns
        -------
        list[GrowthJobSpec]
            Snapshot of the pending job list at this instant.
        """
        with self._lock:
            return [
                j for job_id, j in self._jobs.items()
                if job_id not in self._completed
            ]

    def cancel(self, job_id: str) -> bool:
        """Remove a job from the queue.

        Parameters
        ----------
        job_id:
            ID of the job to cancel.

        Returns
        -------
        bool
            ``True`` if the job was found and removed, ``False`` if not found.
        """
        with self._lock:
            if job_id in self._jobs:
                del self._jobs[job_id]
                self._completed.discard(job_id)
                _LOG.debug("growth_scheduler: cancelled job_id=%s", job_id)
                return True
        return False

    def _tick(self) -> list[str]:
        """Process all due jobs.

        Called by an external scheduler (e.g. APScheduler) or from tests.
        For each pending, enabled job whose ``run_at`` is in the past (or
        None), the action is dispatched via the policy's
        ``route_growth_action`` function if available, or a stub log if not.

        One-shot jobs are moved to the completed set after a successful tick.
        Recurring jobs remain in the queue (they are not moved to completed).

        Returns
        -------
        list[str]
            List of job_ids that were processed in this tick.
        """
        now = datetime.now(tz=timezone.utc)
        processed: list[str] = []

        with self._lock:
            pending_snapshot = list(self._jobs.values())

        for spec in pending_snapshot:
            if spec.job_id in self._completed:
                continue
            if not spec.enabled:
                continue
            if spec.run_at is not None and spec.run_at > now:
                continue  # not yet due

            _LOG.info(
                "growth_scheduler._tick: dispatching job_id=%s action=%s",
                spec.job_id, spec.action,
            )

            try:
                route_fn = getattr(self._policy, "route_growth_action", None)
                if callable(route_fn):
                    route_fn(spec.action, spec.payload)
            except Exception as exc:  # noqa: BLE001
                _LOG.error(
                    "growth_scheduler._tick: job_id=%s action=%s raised: %s",
                    spec.job_id, spec.action, exc,
                )

            processed.append(spec.job_id)

            # One-shot: mark completed.
            if spec.recurrence is None:
                with self._lock:
                    self._completed.add(spec.job_id)

        return processed


__all__ = [
    "GrowthJobSpec",
    "GrowthScheduler",
    "GrowthSchedulerError",
    "GrowthSchedulerDisabledError",
]
