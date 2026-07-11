"""Worker action-token budget system.

Two cooperating components:

ActionContext
    Per-job token budget.  The registry maps every (service, action) pair
    to a default token count.  BaseSqsWorker creates one ActionContext per
    envelope before calling handle().  Handlers that opt-in declare an
    ``action_context`` kwarg and call ``ctx.spend()`` before each expensive
    sub-step (LLM call, external API call, DAG stage, etc.).  When tokens
    run to zero ``BudgetExhausted`` is raised; the worker catches it, marks
    the job "budget_exhausted" and deletes the message — no DLQ trip.

PollThrottle
    Idle backoff for the SQS poll loop.  After ``idle_threshold`` consecutive
    empty polls the worker sleeps an extra ``base_extra_s`` between polls;
    each subsequent empty round doubles the extra sleep up to ``max_extra_s``.
    A work arrival resets to normal pacing immediately.

    Workers already pay a 20-second SQS long-poll on every empty receive.
    PollThrottle adds sleep *on top of* that, converging idle workers toward
    a cycle time of up to ``20 + max_extra_s`` seconds — far cheaper than
    spinning every 20s when the queue has been dry for minutes.

Budget registry
    ACTION_BUDGET_REGISTRY is a flat dict keyed by "service.action".
    Handlers are grouped into three tiers:

      · deterministic/CRUD   (budget  3–5)  — no LLM, pure state mutation
      · ML / moderate        (budget  5–15) — scoring, pipeline steps
      · LLM-heavy / pipeline (budget 10–40) — multi-turn LLM, DAG execution

    Override at dispatch time by setting ``envelope.metadata["action_budget"]``
    (int) on the outgoing message; the consumer gives that value priority over
    the registry entry.
"""
from __future__ import annotations

import logging

_LOG = logging.getLogger("samus.worker_budget")


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class BudgetExhausted(RuntimeError):
    """Raised inside a handler when its action-token allotment hits zero."""

    def __init__(self, service: str, action: str, budget: int, used: int) -> None:
        super().__init__(
            f"{service}.{action}: action budget exhausted "
            f"(allotted={budget}, used={used})"
        )
        self.service = service
        self.action = action
        self.budget = budget
        self.used = used


# ---------------------------------------------------------------------------
# Per-job context
# ---------------------------------------------------------------------------


class ActionContext:
    """Per-job action-token tracker.

    BaseSqsWorker creates one before calling ``handle()`` and passes it as
    the ``action_context`` kwarg to handlers that accept it.

    Handlers call ``ctx.spend()`` before each expensive sub-step::

        def handle(self, envelope, *, action_context=None, **kw):
            if action_context:
                action_context.spend()        # costs 1 token
            result = self._llm_call(...)
            if action_context:
                action_context.spend()        # costs another
            ...

    Handlers that do not declare ``action_context`` receive nothing and are
    unaffected — the budget is silently unenforced for them (backwards-compat).
    """

    def __init__(self, service: str, action: str, budget: int) -> None:
        self.service = service
        self.action = action
        self._total = max(1, budget)
        self._used = 0

    # --- public API -------------------------------------------------------

    def spend(self, n: int = 1) -> None:
        """Consume *n* tokens.  Raises :exc:`BudgetExhausted` when zero remain."""
        if n < 1:
            return
        if self._used + n > self._total:
            raise BudgetExhausted(self.service, self.action, self._total, self._used)
        self._used += n

    def can_spend(self, n: int = 1) -> bool:
        """Return True if *n* tokens are still available (no side-effects)."""
        return self._used + n <= self._total

    # --- read-only properties ---------------------------------------------

    @property
    def remaining(self) -> int:
        return max(0, self._total - self._used)

    @property
    def total(self) -> int:
        return self._total

    @property
    def used(self) -> int:
        return self._used

    def __repr__(self) -> str:
        return (
            f"ActionContext({self.service!r}, {self.action!r}, "
            f"used={self._used}/{self._total})"
        )


# ---------------------------------------------------------------------------
# Budget registry
# ---------------------------------------------------------------------------

# Default when a (service, action) key is absent from the registry.
_FALLBACK_BUDGET = 10

ACTION_BUDGET_REGISTRY: dict[str, int] = {
    # ---- deterministic / CRUD (3–5 tokens) --------------------------------
    "feedback.ingest": 3,
    "outreach.log_outcome": 3,
    "outreach.advance_call": 5,
    "outreach.send_message": 5,
    "crm.upsert_prospect": 3,
    "crm.upsert_conversation": 3,
    "crm.upsert_call_state": 3,
    "crm.create_task": 3,
    "crm.update_task": 3,
    "crm.create_artifact": 3,
    "crm.convert_lead": 5,
    "crm.create_opportunity": 5,
    "crm.advance_opportunity": 5,
    "crm.find_opportunity_for_email": 5,
    "crm.close_opportunity_from_payment": 5,
    "crm.close_payment_to_opportunity": 5,
    "optimizer.select_arm": 5,
    "optimizer.update_arm": 5,
    "fulfillment.ingest_result": 3,
    "fulfillment.validate_plan": 5,
    "fulfillment.finalize_plan": 5,
    "proposal.validate_proposal": 5,
    # ---- ML / moderate compute (5–15 tokens) ------------------------------
    "leadgen.score_lead": 5,
    "optimizer.optimize_portfolio": 8,
    "prospecting.score_deal": 5,
    "prospecting.analyze_business": 10,
    "prospecting.generate_dynamic_script": 6,
    "prospecting.generate_dynamic_script_with_pivot": 8,
    # ---- LLM-heavy (10–20 tokens) -----------------------------------------
    "prospecting.discover": 15,
    "scaffold.generate_assets": 12,
    "proposal.generate_proposal": 10,
    "seo.generate_content": 10,
    "seo.optimize_page": 12,
    "seo.audit_site": 20,
    "fulfillment.plan_execution": 12,
    "fulfillment.resume_plan": 20,
    # ---- multi-stage pipeline (20–40 tokens) ------------------------------
    "fulfillment.execute_plan": 30,
    "cash_engine.process_job": 40,
}


def get_budget(service: str, action: str, *, override: int | None = None) -> int:
    """Resolve the token budget for a (service, action) pair.

    Priority: envelope override > registry > fallback default.
    ``override`` comes from ``envelope.metadata.get("action_budget")``; pass
    it here so callers never need to import the registry directly.
    """
    if override is not None and override > 0:
        return int(override)
    key = f"{service}.{action}"
    budget = ACTION_BUDGET_REGISTRY.get(key, _FALLBACK_BUDGET)
    return budget


# ---------------------------------------------------------------------------
# Idle poll throttle
# ---------------------------------------------------------------------------


class PollThrottle:
    """Exponential idle-backoff for SQS poll loops.

    When consecutive empty polls exceed ``idle_threshold`` the throttle begins
    injecting extra sleep *after* each empty long-poll.  The extra sleep grows
    by ``factor`` each idle tier up to ``max_extra_s``.  When a message
    arrives the throttle resets immediately.

    Idle convergence timeline (defaults: threshold=3, base=5s, factor=2)::

        polls 1–3   empty  → 0s extra   (normal 20s SQS long-poll only)
        poll  4     empty  → 5s extra   (25s cycle)
        poll  5     empty  → 10s extra  (30s cycle)
        poll  6     empty  → 20s extra  (40s cycle)
        poll  7+    empty  → 40s extra  (60s cycle)  ← cap at max_extra_s=60
        any poll    work   → reset to 0s extra immediately
    """

    def __init__(
        self,
        *,
        idle_threshold: int = 3,
        base_extra_s: float = 5.0,
        max_extra_s: float = 60.0,
        factor: float = 2.0,
    ) -> None:
        self._threshold = max(1, idle_threshold)
        self._base = base_extra_s
        self._max = max_extra_s
        self._factor = factor
        self._idle_count: int = 0
        self._extra_s: float = 0.0

    def on_idle(self) -> float:
        """Call when SQS returned empty.  Returns seconds of extra sleep to take."""
        self._idle_count += 1
        if self._idle_count <= self._threshold:
            self._extra_s = 0.0
            return 0.0
        tier = self._idle_count - self._threshold  # 1-based tier index
        self._extra_s = min(self._base * (self._factor ** (tier - 1)), self._max)
        return self._extra_s

    def on_work(self) -> None:
        """Call when one or more messages were received.  Resets to normal pacing."""
        if self._idle_count > 0:
            _LOG.debug(
                "worker_poll_throttle_reset",
                extra={"idle_count_was": self._idle_count},
            )
        self._idle_count = 0
        self._extra_s = 0.0

    @property
    def idle_count(self) -> int:
        return self._idle_count

    @property
    def extra_sleep_s(self) -> float:
        return self._extra_s
