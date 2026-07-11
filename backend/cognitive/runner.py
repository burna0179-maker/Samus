"""Cognition composition root — the Phase-F assembly + single live caller.

This module is the **composition root** for the Samus cognitive stack
(build-plan **Phase F [GATE → built DORMANT]**). It is the one place that wires
the pieces the earlier phases built into a runnable runtime:

    LiveDomainProvider (PERCEIVE, read-only)  ─┐
    build_llm_reasoner (REASON, budget-gated) ─┤
    EthicalFailureHandler (CLASSIFY gate)     ─┼─►  CognitiveLoop
    record_proposal sink (ACT, propose-only)  ─┘        │
                                                        ▼
                              MetaCognitionEngine (META-PERCEPTION / REFLECTION)
                              + the 6 backend.autonomy.* subsystems
                              + PersonaSystem (Stage-1) over PersonaMemory

Why a new module (justified per the build plan): assembling the stack pulls in
the heavy live services (CRM / finance / LLM client / EFH axioms). Keeping that
wiring OUT of ``cognitive_loop.py`` / ``meta_cognition_engine.py`` preserves
their dormancy guarantee — those modules import nothing heavy and have no live
importer. The composition lives here, and is itself only ever reached through
the Phase-F caller, which is gated.

DORMANCY (the whole point of Phase F):

* **Master switch.** ``settings.cognitive_loop_enabled`` (env
  ``SAMUS_COGNITIVE_LOOP_ENABLED``) defaults **False**. The caller checks it
  FIRST and, when False, constructs NOTHING and runs NOTHING — see
  :func:`loop_enabled`. The gateway route returns a structured ``{enabled:
  false}`` no-op (HTTP 503) in that state.
* **No import-time construction.** Importing this module builds no loop, opens
  no provider, makes no LLM call. Every heavy import (the loop, the engine, the
  live domain provider, the LLM reasoner, the EFH, persona) is LAZY — performed
  inside :func:`build_cognition_runtime` / :func:`run_one_cycle`, never at module
  top level. So a transitive import (e.g. the dormancy test importing the
  gateway) touches no backend.
* **Each sub-behaviour keeps its own flag.** Arming the master switch does NOT
  arm the sub-behaviours: the wrapped ``MetaCognitionEngine`` still reads
  ``autonomy_meta_enabled`` (OFF ⇒ pure passthrough to ``loop.cycle``); the
  loop's ACT still reads ``cognitive_act_proposals_enabled`` (OFF ⇒ writes no
  proposal); persona still reads ``persona_frame_enabled`` /
  ``persona_self_model_enabled``. So the loop can run with the master switch on
  while every commercial sub-behaviour stays independently dormant.

PROPOSE-ONLY (the safety property): even when the master switch AND every
sub-flag are armed, ACT is propose-only — it records a structured proposal to
``state_path("cognition","proposals.jsonl")`` and calls NO effector / gate /
send / network. The READ side (PERCEIVE) only ever calls the read-only domain
surfaces enumerated in :mod:`backend.cognitive.domain_perception`; the only
egress is the single budget-gated REASON LLM completion. Promotion of a recorded
proposal toward live revenue influence is the SEPARATE, still-UNBUILT,
operator-gated **Phase G**.

CADENCE: this phase exposes ONE caller — a manual gateway route. It deliberately
does NOT schedule an autonomous cadence (no control-tick hook). The future
cadence attach point is documented at the bottom of this module
(:func:`_future_cadence_attach_point`) but intentionally left unwired — that is a
later operator-gated step.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, List, Optional

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Master-switch gate (default OFF). Fail-closed to OFF on any settings error.
# ---------------------------------------------------------------------------
def loop_enabled() -> bool:
    """Resolve the Phase-F master switch ``cognitive_loop_enabled`` (default OFF).

    This is the single authority the live caller consults BEFORE constructing
    anything. Any failure reading settings degrades to OFF (the dormant posture)
    so a settings hiccup can never accidentally arm the live caller.
    """
    try:
        from backend.common.config import get_settings

        return bool(getattr(get_settings(), "cognitive_loop_enabled", False))
    except Exception:  # noqa: BLE001 — fail-closed to dormant
        return False


# ---------------------------------------------------------------------------
# Result summary the live caller returns (route-friendly, JSON-able).
# ---------------------------------------------------------------------------
@dataclass
class CycleResultSummary:
    """Compact, JSON-able summary of one cognitive cycle for a live caller.

    Built from the loop's :class:`backend.cognitive.cycle_models.CycleResult`.
    Carries exactly what the build-plan asks the Phase-F route to return: the
    reply, ``ok``, ``compliance_blocked``, the stage names, and the id/record of
    any propose-only proposal written by ACT.
    """

    enabled: bool = True
    ok: bool = False
    reply: str = ""
    compliance_blocked: bool = False
    plan_token: str = ""
    elapsed_ms: float = 0.0
    reflect_score: float = 1.0
    stages: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    # Propose-only ACT artifact (Phase E). ``proposal_written`` is True iff ACT
    # appended exactly one proposal this cycle; ``proposal`` is that record (or
    # None). ACT is propose-only — a written proposal is NOT an actuation.
    proposal_written: bool = False
    proposal: Optional[dict] = None
    # Sub-flag posture echoed back so an operator can see, at a glance, that the
    # loop ran with meta/ACT independently gated even though the master switch
    # was on.
    meta_enabled: bool = False
    act_proposals_enabled: bool = False
    # Phase G — proposal promotion verdict (the FIRST path that routes propose-
    # only artifacts through the cash-engine front door / authoritative gates).
    # ``promotion_attempted`` is True iff the runner seam invoked the promoter
    # this cycle (gated by ``cognition_proposal_promotion_enabled`` AND a non-
    # compliance-blocked cycle AND a written proposal). ``promotion_result`` is
    # the projected :class:`PromotionResult` dict (status: pass | blocked |
    # skipped | error + reason / required_protocol / etc.) or None when not
    # attempted. ``promotion_enabled`` echoes the kill-switch posture.
    promotion_enabled: bool = False
    promotion_attempted: bool = False
    promotion_result: Optional[dict] = None

    def to_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "ok": self.ok,
            "reply": self.reply,
            "compliance_blocked": self.compliance_blocked,
            "plan_token": self.plan_token,
            "elapsed_ms": round(self.elapsed_ms, 3),
            "reflect_score": round(self.reflect_score, 4),
            "stages": list(self.stages),
            "errors": list(self.errors),
            "proposal_written": self.proposal_written,
            "proposal": self.proposal,
            "meta_enabled": self.meta_enabled,
            "act_proposals_enabled": self.act_proposals_enabled,
            "promotion_enabled": self.promotion_enabled,
            "promotion_attempted": self.promotion_attempted,
            "promotion_result": self.promotion_result,
        }


# A capturing proposal sink so the live caller can return the proposal record
# (and its id) ACT wrote, while STILL persisting it to the durable JSONL ledger.
# It tees: append() forwards to the real ledger (best-effort) AND keeps the row.
class _CapturingProposalSink:
    """Tee proposal sink — persists to the durable ledger AND captures the row.

    ``record_proposal`` appends to whichever sink the loop holds. We hand the
    loop one of these so the route can read back the exact proposal record ACT
    wrote (for the response summary) without losing the durable on-disk append.
    A test can instead inject its own spy sink via ``build_cognition_runtime``.
    """

    def __init__(self, durable: Optional[Any] = None) -> None:
        # ``durable`` is the real JSONL ledger; None => resolve the default
        # lazily on first append so importing this module opens no file.
        self._durable = durable
        self.rows: List[dict] = []

    def append(self, record: dict) -> None:
        self.rows.append(record)
        try:
            ledger = self._durable
            if ledger is None:
                # Lazy default: the same ledger record_proposal would use.
                from backend.cognitive.domain_perception import _proposals_ledger

                ledger = _proposals_ledger()
                self._durable = ledger
            ledger.append(record)
        except Exception as exc:  # noqa: BLE001 — durable append is best-effort
            log.warning("cognition runner: durable proposal append failed: %s", exc)

    @property
    def last(self) -> Optional[dict]:
        return self.rows[-1] if self.rows else None


@dataclass
class CognitionRuntime:
    """The assembled, ready-to-run cognitive runtime (Phase-F composition).

    Holds the :class:`MetaCognitionEngine` (which wraps the
    :class:`CognitiveLoop`) plus the capturing proposal sink so a caller can read
    back the ACT proposal. Construct via :func:`build_cognition_runtime`; run via
    :func:`run_one_cycle`. Nothing here is constructed unless the master switch
    is on (the caller checks :func:`loop_enabled` first).
    """

    engine: Any
    proposal_sink: Any


def build_cognition_runtime(
    *,
    domain: Optional[Any] = None,
    reasoner: Optional[Any] = None,
    classifier: Optional[Any] = None,
    proposal_sink: Optional[Any] = None,
    persona: Optional[Any] = None,
    enabled: Optional[bool] = None,
) -> CognitionRuntime:
    """Assemble the full cognitive stack (the composition root).

    Constructs, with every heavy import performed LAZILY here (never at module
    import time):

    * **PERCEIVE** — a read-only :class:`LiveDomainProvider`
      (``build_live_domain_provider``) over the CRM / decay / finance reads,
      unless a ``domain`` override is supplied (tests inject a stub).
    * **REASON** — the budget-gated LLM reasoner (``build_llm_reasoner``) that
      routes ONE completion through the ``"cognition"`` workcell, unless a
      ``reasoner`` override is supplied. The reasoner is the only egress.
    * **CLASSIFY** — the :class:`EthicalFailureHandler` gate (fail-closed to
      BLOCK inside the loop), unless a ``classifier`` override is supplied.
    * **ACT (propose-only)** — a :class:`_CapturingProposalSink` so the caller
      can read back the proposal, unless a ``proposal_sink`` override is given.
      ACT only ever APPENDS a proposal; it calls no effector.

    The :class:`CognitiveLoop` is then wrapped by a :class:`MetaCognitionEngine`
    (which brings the 6 ``backend.autonomy.*`` subsystems + ``PersonaSystem``).
    Each component still honours its OWN flag — the engine reads
    ``autonomy_meta_enabled`` (OFF ⇒ passthrough), ACT reads
    ``cognitive_act_proposals_enabled`` (OFF ⇒ no write), persona reads its
    flags — so arming the master switch alone does not arm the sub-behaviours.

    ``enabled`` is the MetaCognitionEngine's explicit override (tests pass True
    to exercise the meta path deterministically); ``None`` ⇒ the engine sources
    its own ``autonomy_meta_enabled`` flag, which is the production posture.

    NOTE: callers MUST gate on :func:`loop_enabled` BEFORE calling this — the
    master switch is enforced at the call site (the route), not here, so that a
    test can build a runtime directly while the live route stays dormant.
    """
    # ---- lazy, heavy imports (kept out of module import for dormancy) -------
    from backend.cognitive.cognitive_loop import CognitiveLoop, build_llm_reasoner
    from backend.cognitive.meta_cognition_engine import MetaCognitionEngine

    # PERCEIVE provider (read-only). Default = the live façade; tests pass a stub.
    if domain is None:
        from backend.cognitive.domain_perception import build_live_domain_provider

        domain = build_live_domain_provider()

    # REASON collaborator (budget-gated single LLM call). Default = production.
    # The strategic intelligence cycle standpoint is loaded as system_prefix
    # so every reasoning call is framed by the operator's intelligence doctrine,
    # AND the currently-accepted strategic guidance is appended so each
    # autonomous tick reasons toward what the operator's advisor recommended.
    # This is the OpenAI-guidance -> ingested -> accepted -> steers-reasoning
    # feedback loop. Both reads are fail-safe (degrade to directive-only).
    if reasoner is None:
        from backend.cognitive.directives import strategic_intelligence_cycle

        system_prefix = strategic_intelligence_cycle()
        try:
            from backend.cognitive.intelligence_cycle import active_guidance_context

            guidance_ctx = active_guidance_context()
            if guidance_ctx:
                system_prefix = f"{system_prefix}\n\n{guidance_ctx}"
        except Exception as exc:  # noqa: BLE001 — guidance is advisory, never fatal
            log.warning("cognition runner: guidance context unavailable: %s", exc)

        # Concept 1 — precedent injection. Same pattern as guidance: pull the
        # compact precedent block (top belief precedents + top prior decisions
        # keyed on the loop's operating context) and append to the system_prefix
        # so REASON grounds against "have we seen this before?" alongside
        # "what did the advisor say today?". Fail-safe — no precedent leaves
        # the system_prefix at guidance-only.
        try:
            from backend.cognitive.intelligence_cycle import active_precedent_context

            precedent_ctx = active_precedent_context("cognition loop reasoning")
            if precedent_ctx:
                system_prefix = f"{system_prefix}\n\n{precedent_ctx}"
        except Exception as exc:  # noqa: BLE001 — precedent is advisory, never fatal
            log.warning("cognition runner: precedent context unavailable: %s", exc)

        reasoner = build_llm_reasoner(system_prefix=system_prefix)

    # CLASSIFY gate (EFH). Default = the real ethical gate. Constructing it reads
    # the axiom YAMLs; if that fails we degrade to NO classifier (the loop then
    # records a SKIPPED CLASSIFY and proceeds — the downstream governance gates
    # the real send path passes through remain authoritative regardless).
    if classifier is None:
        try:
            from backend.governance.efh_evaluator import EthicalFailureHandler

            classifier = EthicalFailureHandler()
        except Exception as exc:  # noqa: BLE001 — never let EFH wiring break boot
            log.warning("cognition runner: EFH classifier unavailable: %s", exc)
            classifier = None

    # ACT propose-only sink. Default = capturing tee onto the durable ledger.
    sink = proposal_sink if proposal_sink is not None else _CapturingProposalSink()

    # Stage-1 persona (compute-only, flag-gated). When not supplied we let the
    # MetaCognitionEngine construct its own neutral PersonaSystem (its default),
    # so persona is wired but inert unless persona_frame_enabled is armed.
    loop = CognitiveLoop(
        classifier=classifier,
        reasoner=reasoner,
        domain=domain,
        proposal_sink=sink,
        persona=persona,
    )
    engine = MetaCognitionEngine(loop, enabled=enabled)
    return CognitionRuntime(engine=engine, proposal_sink=sink)


async def run_one_cycle(
    inp: Any,
    *,
    runtime: Optional[CognitionRuntime] = None,
    promotion_review: Optional[Any] = None,
    promotion_telemetry: Optional[Any] = None,
    **build_kwargs: Any,
) -> CycleResultSummary:
    """Run EXACTLY ONE cognitive cycle and return a JSON-able summary.

    Assembles the runtime (unless one is supplied) and invokes the
    :class:`MetaCognitionEngine` once. NEVER raises — the underlying
    ``CognitiveLoop.cycle`` is fail-closed and returns a ``CycleResult`` even on
    a degraded cycle; this wrapper additionally guards construction so a wiring
    fault returns a degraded summary rather than propagating.

    The returned :class:`CycleResultSummary` carries the reply, ``ok``,
    ``compliance_blocked``, the stage names, and the propose-only proposal
    record/id ACT wrote (read back off the capturing sink). ACT is propose-only:
    the summary's ``proposal_written`` records that a proposal was APPENDED, not
    that anything was actuated.

    **Phase G — post-cycle promotion seam.** After the cycle completes, when
    (a) ``cognition_proposal_promotion_enabled`` is True (default; the operator's
    kill-switch), (b) the cycle was NOT compliance-blocked (a CLASSIFY-vetoed
    cycle's proposal is never promoted — there is no proposal to begin with),
    and (c) ACT wrote exactly one proposal this cycle, the runner invokes
    :func:`backend.cognitive.proposal_promoter.promote_one_for_runner` on the
    just-written proposal. The promoter routes the proposal through
    ``review_opportunity`` — the cash-engine front door — which itself runs
    Stake Sentence + Codex + karma + downstream compliance/EFH (every
    authoritative gate); Phase G honours the verdict, marks the row actioned,
    and NEVER retries past a BLOCK. The promotion call is fail-closed: a gate
    exception becomes ``promotion_result.status='error'`` and never escapes.

    ``promotion_review`` / ``promotion_telemetry`` are test seams (an
    injectable ``review_opportunity`` stub and an injectable telemetry sink) —
    in production these stay None and the live :func:`review_opportunity` +
    the durable JSONL telemetry ledger are used.

    This is the single live entrypoint. A caller is expected to have checked
    :func:`loop_enabled` first; passing the master switch is the route's job.
    """
    summary = CycleResultSummary(enabled=True)
    try:
        rt = runtime if runtime is not None else build_cognition_runtime(**build_kwargs)
    except Exception as exc:  # noqa: BLE001 — construction fault ⇒ degraded summary
        log.exception("cognition runner: runtime construction failed")
        summary.ok = False
        summary.errors = [f"runtime_build:{type(exc).__name__}:{exc}"]
        return summary

    # Echo the (independent) sub-flag posture for operator visibility.
    try:
        from backend.common.config import get_settings

        _s = get_settings()
        summary.meta_enabled = bool(getattr(_s, "autonomy_meta_enabled", False))
        summary.act_proposals_enabled = bool(
            getattr(_s, "cognitive_act_proposals_enabled", False)
        )
        summary.promotion_enabled = bool(
            getattr(_s, "cognition_proposal_promotion_enabled", False)
        )
    except Exception:  # noqa: BLE001 — visibility only; never fail the cycle
        pass

    result = await rt.engine.cycle(inp)

    # Project the loop's CycleResult onto the route-friendly summary.
    summary.ok = bool(getattr(result, "ok", False))
    summary.reply = getattr(result, "reply", "") or ""
    summary.compliance_blocked = bool(getattr(result, "compliance_blocked", False))
    summary.plan_token = getattr(result, "plan_token", "") or ""
    summary.elapsed_ms = float(getattr(result, "elapsed_ms", 0.0) or 0.0)
    summary.reflect_score = float(getattr(result, "reflect_score", 1.0) or 0.0)
    summary.stages = [getattr(s, "name", "") for s in getattr(result, "stages", [])]
    summary.errors = list(getattr(result, "errors", []) or [])

    # Read back the propose-only proposal ACT wrote (if any) off the sink.
    sink = getattr(rt, "proposal_sink", None)
    proposal = getattr(sink, "last", None)
    if proposal is not None:
        summary.proposal_written = True
        summary.proposal = proposal

    # Phase-G promotion seam. Three guards, each load-bearing:
    #   (1) the kill-switch is ON (we re-resolve via the promoter, which is the
    #       single authority and fails closed to OFF on any settings error);
    #   (2) the cycle was NOT compliance-blocked (CLASSIFY veto: no proposal,
    #       no promotion — never route a blocked-cycle proposal);
    #   (3) a proposal was actually written this cycle (ACT enabled + not a
    #       no-op stub).
    # The seam itself NEVER raises: ``promote_one_for_runner`` is fail-closed.
    if summary.proposal_written and not summary.compliance_blocked and summary.proposal is not None:
        try:
            from backend.cognitive.proposal_promoter import promote_one_for_runner

            verdict = promote_one_for_runner(
                summary.proposal,
                review=promotion_review,
                telemetry=promotion_telemetry,
            )
            if verdict is not None:
                summary.promotion_attempted = True
                summary.promotion_result = verdict.to_dict()
        except Exception as exc:  # noqa: BLE001 — seam itself is fail-closed
            log.warning("cognition runner: promotion seam errored: %s", exc)

    return summary


# ---------------------------------------------------------------------------
# Cadence attach point — WIRED (updated 2026-07-06, Slice B).
# ---------------------------------------------------------------------------
def _cadence_attach_point() -> None:
    """Documentation marker: where the autonomous cadence attaches to this runner.

    HISTORICAL NAME (kept for grep-continuity): this was
    ``_future_cadence_attach_point`` when the cadence was still deferred. As of
    the Slice B activation the cadence IS wired.

    LIVE WIRING (do not re-mark this "future"):

      * The autonomous cadence is implemented at
        ``backend/cognitive/cadence.py::CognitionCadenceTask`` and started by the
        gateway lifespan at ``backend/gateway/app.py`` around line 354 via
        ``start_cognition_cadence(app)`` / stopped via
        ``stop_cognition_cadence(app)``.
      * Each tick the cadence calls :func:`run_one_cycle` here, re-checking
        :func:`loop_enabled` every tick so a runtime arm/disarm does not require
        a gateway restart. It swallows per-tick faults so a bad tick never kills
        the loop.
      * ``cognition_cadence_enabled`` (env
        ``SAMUS_COGNITION_CADENCE_ENABLED``) default True — the gateway lifespan
        starts the loop. ``cognitive_loop_enabled`` (env
        ``SAMUS_COGNITIVE_LOOP_ENABLED``) default True as of Slice B — each tick
        actually executes rather than no-op'ing.

    This function itself does nothing at runtime — it exists only as a
    documentation anchor for readers tracing the cadence→runner→loop path.
    """
    return None


# Back-compat alias for any importer that still references the old name.
_future_cadence_attach_point = _cadence_attach_point


__all__ = [
    "loop_enabled",
    "build_cognition_runtime",
    "run_one_cycle",
    "CognitionRuntime",
    "CycleResultSummary",
]
