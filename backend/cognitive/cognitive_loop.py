"""Samus ``CognitiveLoop`` — Phase A skeleton (the core MetaCognitionEngine wraps).

This is the distinct THIRD autonomy construct named in the build plan
(separate from ``backend/common/autonomy.py``'s MAPE-K planner and
``backend/identity/autonomy_layer.py``'s ``_shared.autonomy`` wiring). It
implements **Contract 1**: ``async def cycle(self, inp) -> CycleResult``.

Design, mirrored from Anita's ``cognitive_loop.py`` at the *minimal*
Optimus altitude (``backend/standard/cognition/loop.py``):

* Every heavy dependency is constructor-injected and OPTIONAL — a bare
  ``CognitiveLoop()`` with no deps runs the full stage list with each
  optional stage degrading to a no-op (the "bare-loop" test pattern).
* Stages: PERCEIVE -> CLASSIFY -> REASON -> DECIDE -> ACT -> REFLECT ->
  PERSIST -> EMIT. In THIS phase REASON and ACT are no-op stubs (REASON
  echoes the user text as the candidate reply; ACT records a no-op).
  CLASSIFY can short-circuit: when a wired classifier flags a compliance
  breach it sets ``result.compliance_blocked`` and the loop skips
  REASON/DECIDE/ACT/REFLECT/PERSIST straight to EMIT (mirrors Anita's
  ``blocked_reply`` short-circuit).
* Fail-closed EVERYWHERE: a stage that raises never propagates — the loop
  converts it to an ERROR ``StageResult``, appends a summary to
  ``result.errors``, and continues degraded. ``cycle`` itself never raises.
* ``result.elapsed_ms`` is stamped from a ``time.perf_counter`` span around
  the loop body.

The optional-dependency *protocols* are intentionally duck-typed (held as
``Optional[Any]``) so this module imports nothing from the heavy live
subsystems and stays dormant. Later phases wire concrete deps:
  * ``classifier``    — CLASSIFY: ``classify(text=,channel=,plan_token=) -> dict``
    AND/OR a compliance gate exposing ``compliance_blocked``-style verdicts.
  * ``reasoner``      — REASON: produces the candidate reply (Phase C: LLM).
  * ``actuator``      — ACT: proposes-only effector (Phase E: proposals.jsonl).
  * ``reflector``     — REFLECT: ``score(...) -> float`` self-critique.
  * ``persona``       — Stage-1 ``PersonaSystem`` (``transform`` / ``snapshot``).
  * ``persister``     — PERSIST: durable cycle snapshot (architecture persistence).

DORMANCY: NO live importer. Nothing in the running Samus stack constructs
or calls this loop; it is wired to a caller only in Phase F (operator-gated).
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

from .cycle_models import (
    CycleInput,
    CycleResult,
    StageResult,
    StageStatus,
)

log = logging.getLogger(__name__)

STAGE_NAMES = (
    "PERCEIVE",
    "CLASSIFY",
    "REASON",
    "DECIDE",
    "ACT",
    "REFLECT",
    "PERSIST",
    "EMIT",
)

# Default reply when REASON produces nothing (mirrors Anita's DECIDE sentinel).
_NO_REPLY = "(no reply generated)"

# Fail-closed reply emitted when REASON could not produce a candidate because
# the LLM call was budget-denied / errored. Mirrors the Optimus loop's
# "fail-closed to HOLD" posture: the cycle does not raise and does not fabricate
# a reply — it surfaces a safe HOLD and records the error on result.errors.
_HOLD_REPLY = "(held: reasoning unavailable)"

# Reply emitted when CLASSIFY short-circuits the cycle on a compliance/axiom
# veto. A fixed, safe refusal — the loop proposes only and never explains around
# a governance block.
_BLOCKED_REPLY = "(blocked: compliance gate vetoed this action)"

# Directive question REASON appends after the grounded perception context. Used
# for the cadence/cognition_tick path (no caller user_text) so the prompt is
# always non-empty and asks for the single highest-leverage next move grounded
# in the rendered state. The caller's user_text (manual route) is honoured and
# this is NOT appended in that case — the perception context is prepended to it.
_REASON_DIRECTIVE = (
    "Given the current business state above, name the single highest-leverage "
    "next action and the specific prospect it targets (by prospect_id when one "
    "is named). Answer in one or two concise sentences."
)

# Workcell name the REASON LLM call accounts against (build-plan Phase C).
_REASON_WORKCELL = "cognition"
# Conservative output ceiling for the single REASON completion. Kept modest so a
# tick's generation stays well under the LLM read timeout on the slow local 19B
# model — an open-ended prompt at 512 tokens was exceeding the 30s default and
# failing the autonomous cadence tick closed (lm_studio_transport_error: timed out).
_REASON_MAX_TOKENS = 256
# Explicit read timeout for the single cognition completion. The llm_client local
# default is 30s, which a slow generation can blow; give the bounded 256-token
# completion headroom without letting a wedged backend stall a tick indefinitely.
_REASON_TIMEOUT = 45.0


def build_llm_reasoner(
    *,
    system_prefix: str = "",
    max_tokens: int = _REASON_MAX_TOKENS,
    timeout: float = _REASON_TIMEOUT,
    model: Optional[str] = None,
):
    """Construct the production REASON collaborator (budget-gated LM Studio call).

    Returns an object exposing ``generate(system=, user_text=, channel=,
    plan_token=, metadata=) -> str`` that routes ONE completion through
    :func:`backend.common.llm_client.anthropic_messages` on the ``"cognition"``
    workcell so the per-workcell token budget, the global $-cap, and the circuit
    breaker all gate it. This is wired only by a (later, operator-gated) Phase-F
    caller — the bare loop and the unit tests inject a stub instead, so importing
    this module never touches the LLM backend.

    The reasoner itself does NOT swallow LLM exceptions — it lets
    ``BudgetExceeded`` / ``GlobalBudgetExceeded`` / ``LlmCallError`` propagate so
    the loop's REASON stage is the single fail-closed-to-HOLD boundary.

    Model resolution: when ``model`` is None (the default), the reasoner uses the
    LLM client's resolved backend default — ``"local"`` on LM Studio, the
    configured OpenAI model (``SAMUS_OPENAI_MODEL``, default ``gpt-4.1-mini``) on
    the OpenAI backend. Hardcoding ``"local"`` would send the literal string
    ``"local"`` to OpenAI's API and 400 on Cloud Run, so the cadence MUST resolve
    the real model name per backend.
    """
    from backend.common import llm_client as _llm

    class _LlmReasoner:
        def generate(
            self,
            *,
            system: str = "",
            user_text: str = "",
            channel: str = "",
            plan_token: str = "",
            metadata: Optional[dict] = None,
        ) -> str:
            sys_text = (system_prefix + (system or "")).strip() or None
            resolved_model = model or getattr(_llm, "_DEFAULT_MODEL", "local")
            text, _usage = _llm.anthropic_messages(
                workcell=_REASON_WORKCELL,
                api_key="",  # LM Studio needs no auth (accepted for back-compat)
                prompt=user_text or "",
                system=sys_text,
                model=resolved_model,
                max_tokens=max_tokens,
                timeout=timeout,
                security_label="cognition",
            )
            return text

    return _LlmReasoner()


class CognitiveLoop:
    """Phase-A cognitive pipeline. All deps optional; fail-closed; dormant.

    Construct bare (``CognitiveLoop()``) for the contract / unit-test path,
    or inject any subset of the optional collaborators. A ``None`` collaborator
    means its stage degrades to a recorded no-op — never an error.
    """

    def __init__(
        self,
        *,
        classifier: Optional[Any] = None,
        reasoner: Optional[Any] = None,
        actuator: Optional[Any] = None,
        reflector: Optional[Any] = None,
        persona: Optional[Any] = None,
        persister: Optional[Any] = None,
        domain: Optional[Any] = None,
        proposal_sink: Optional[Any] = None,
    ) -> None:
        self._classifier = classifier
        self._reasoner = reasoner
        self._actuator = actuator
        self._reflector = reflector
        self._persona = persona
        self._persister = persister
        # Phase E — OPTIONAL read-only domain perception provider. None (the
        # bare-loop / pre-Phase-E default) => PERCEIVE falls back to persona-only,
        # byte-identical to the prior phase. A wired ``domain`` (a duck-typed
        # DomainProvider façade) makes PERCEIVE read live CRM/decay/finance state
        # through ONLY read-only entrypoints (see backend.cognitive.domain_perception).
        self._domain = domain
        # Phase E — OPTIONAL proposal sink for the propose-only ACT stage. None =>
        # the default JSONL ledger at state_path("cognition","proposals.jsonl");
        # tests inject a spy / temp ledger. ACT only ever *appends* a proposal.
        self._proposal_sink = proposal_sink

    # ---- public entry --------------------------------------------------
    async def cycle(self, inp: CycleInput) -> CycleResult:
        """Run one cognitive cycle. Never raises; always returns a CycleResult.

        Stamps ``elapsed_ms`` around the whole body. On the happy path runs
        all eight stages; on a CLASSIFY compliance block, skips the middle
        stages straight to EMIT. Any stage exception is captured as an ERROR
        StageResult + an ``errors`` entry and the cycle continues degraded.
        """
        started = time.perf_counter()
        # Echo the plan_token onto the result up-front so even a degraded /
        # short-circuited cycle satisfies the contract (the persistence
        # snapshot keys on it).
        result = CycleResult(plan_token=getattr(inp, "plan_token", "") or "")

        try:
            # ---- PERCEIVE -------------------------------------------------
            # Returns the read-only domain perception dict ({} when no domain
            # provider is wired) so the propose-only ACT stage can derive an
            # intent from it without a new CycleResult field.
            perception = self._stage_perceive(inp, result)

            # ---- CLASSIFY (may short-circuit on compliance block) --------
            blocked = self._stage_classify(inp, result)
            if blocked:
                # Compliance / safety veto: skip REASON..PERSIST, go to EMIT.
                result.compliance_blocked = True
                for skipped in ("REASON", "DECIDE", "ACT", "REFLECT", "PERSIST"):
                    self._record_skipped(result, skipped, "blocked_by_classify")
                self._stage_emit(inp, result)
            else:
                # ---- REASON (Phase-A no-op stub: echo a reply) -----------
                # Perception is threaded in so REASON can GROUND the prompt in
                # the live business state (the top opportunity / follow-up + ids).
                reply = self._stage_reason(inp, perception, result)
                # ---- DECIDE ----------------------------------------------
                reply = self._stage_decide(reply, result)
                # ---- ACT (Phase-E propose-only: record a proposal artifact) -
                self._stage_act(inp, reply, perception, result)
                # ---- REFLECT ---------------------------------------------
                self._stage_reflect(inp, reply, result)
                # ---- PERSIST ---------------------------------------------
                self._stage_persist(inp, result)
                # ---- EMIT ------------------------------------------------
                result.reply = reply
                self._stage_emit(inp, result)
        except Exception as exc:  # pragma: no cover — belt-and-braces
            # The per-stage helpers already fail closed; this outer guard is
            # the final fail-closed envelope so ``cycle`` itself can never
            # raise even on an unexpected control-flow error.
            log.exception("cognitive_loop.cycle unexpected error")
            result.errors.append(f"cycle:{type(exc).__name__}:{exc}")

        # ``ok`` is True iff no stage errored. ``elapsed_ms`` is stamped last.
        result.ok = not result.errors and all(
            s.status is not StageStatus.ERROR for s in result.stages
        )
        result.elapsed_ms = round((time.perf_counter() - started) * 1000.0, 3)
        return result

    # ---- stage recorders ----------------------------------------------
    def _record(
        self,
        result: CycleResult,
        name: str,
        started: float,
        status: StageStatus,
        output: Optional[dict] = None,
        error: Optional[str] = None,
    ) -> StageResult:
        sr = StageResult(
            name=name,
            status=status,
            duration_ms=(time.perf_counter() - started) * 1000.0,
            output=dict(output or {}),
            error=error,
        )
        result.stages.append(sr)
        return sr

    def _record_skipped(self, result: CycleResult, name: str, reason: str) -> None:
        self._record(result, name, time.perf_counter(), StageStatus.SKIPPED, {"reason": reason})

    def _record_error(self, result: CycleResult, name: str, started: float, exc: Exception) -> None:
        summary = f"{name}:{type(exc).__name__}:{exc}"
        result.errors.append(summary)
        self._record(result, name, started, StageStatus.ERROR, {}, error=summary)

    # ---- stages --------------------------------------------------------
    def _stage_perceive(self, inp: CycleInput, result: CycleResult) -> dict:
        """PERCEIVE — Stage-1 persona frame + (Phase E) read-only domain state.

        Two independent, both-OPTIONAL perception sources, each fail-safe:

        * **persona** (Stage-1) — when a ``persona`` collaborator exposing
          ``transform(stimulus) -> dict`` is wired, derive the operating frame
          and stash it on ``result.persona_frame``.
        * **domain** (Phase E) — when a ``domain`` provider is wired, pull a
          read-only perception of live CRM/decay/finance state via
          :func:`backend.cognitive.domain_perception.gather_perception`, which
          wraps every source so a backend/DDB/Stripe failure degrades to a
          partial/empty perception and never raises. ``None`` (the default) =>
          an EMPTY perception ``{}``, i.e. persona-only — byte-identical to the
          prior phase, so the bare loop + all existing tests are unchanged.

        Returns the domain perception dict (``{}`` when no provider) so the
        propose-only ACT stage can read it. Fail-closed: any error leaves
        ``persona_frame`` None / perception ``{}`` and records a degraded (still
        non-erroring) stage — PERCEIVE perception is non-fatal in this phase.
        """
        started = time.perf_counter()
        perception: dict = {}
        try:
            frame = None
            if self._persona is not None and hasattr(self._persona, "transform"):
                stimulus = {
                    "user_text": getattr(inp, "user_text", "") or "",
                    "channel": getattr(inp, "channel", "") or "",
                }
                enriched = self._persona.transform(stimulus)
                if isinstance(enriched, dict):
                    # PersonaSystem stashes the frame under FRAME_KEY ("_persona_frame").
                    frame = enriched.get("_persona_frame")
            result.persona_frame = frame

            # Phase E — read-only domain perception (best-effort, never raises).
            # ``gather_perception(None)`` returns {} so the no-provider path adds
            # nothing but a cheap call; importing the helper touches no backend.
            if self._domain is not None:
                from .domain_perception import gather_perception

                perception = gather_perception(self._domain)

            meta = perception.get("_meta", {}) if isinstance(perception, dict) else {}
            self._record(
                result,
                "PERCEIVE",
                started,
                StageStatus.OK,
                {
                    "persona": frame is not None,
                    "chars": len(getattr(inp, "user_text", "") or ""),
                    "domain": self._domain is not None,
                    "domain_available": list(meta.get("available", [])),
                    "domain_degraded": list(meta.get("degraded", [])),
                },
            )
        except Exception as exc:
            # Perception failure must not error the cycle — record a SKIPPED
            # degraded stage and continue (no errors entry). Perception is {}.
            perception = {}
            self._record(
                result, "PERCEIVE", started, StageStatus.SKIPPED, {"degraded": type(exc).__name__}
            )
        return perception

    def _stage_classify(self, inp: CycleInput, result: CycleResult) -> bool:
        """CLASSIFY — returns True to short-circuit (compliance/safety block).

        When a ``classifier`` is wired, consult it. THREE duck-typed shapes are
        accepted, checked in priority order:

        * ``evaluate(proposed_action: dict) -> dict|None`` — the
          ``EthicalFailureHandler`` shape (Phase C). A returned veto **dict**
          (a non-None ``EthicalReviewVeto`` record carrying
          ``inviolable_axioms_breached``) is an axiom breach and BLOCKS; ``None``
          clears. This path is **fail-closed to BLOCK**: an EFH that raises is
          treated as a compliance veto (the safety gate failing open would let an
          un-evaluated commercial action through). The proposed-action dict is
          built from ``inp`` (+ any ``inp.metadata['proposed_action']``).
        * ``is_blocked(text=,channel=,plan_token=) -> bool`` — explicit veto.
        * ``classify(text=,channel=,plan_token=) -> dict`` — a returned dict
          whose ``blocked``/``compliance_blocked`` key is truthy vetoes.

        For the ``is_blocked`` / ``classify`` shapes the **skeleton** posture is
        preserved (a raising classifier degrades to a SKIPPED non-blocking stage)
        so the Phase-A contract tests are unchanged; only the EFH ``evaluate``
        shape carries the fail-closed-to-BLOCK posture. No classifier (the
        bare-loop default) never blocks.

        When the EFH path clears (or is absent) and a concrete action is present
        under ``inp.metadata['codex_action']`` (a duck-typed object Codex's
        ``check_action`` accepts), the Codex gate is additionally consulted and a
        disallowed verdict also BLOCKS — fail-closed on a Codex error too.
        """
        started = time.perf_counter()
        if self._classifier is None:
            meta = getattr(inp, "metadata", {}) or {}
            if meta.get("codex_action") is None:
                # No classifier and no concrete action: record one SKIPPED
                # CLASSIFY stage and proceed (never blocks). Phase-A behaviour.
                self._record(
                    result, "CLASSIFY", started, StageStatus.SKIPPED, {"reason": "no_classifier"}
                )
                return False
            # No EFH but a concrete action is present -> let Codex vote (it
            # writes the single CLASSIFY record itself).
            return self._classify_codex(inp, result, started, verdict={"gate": "codex_only"})

        # --- EFH shape (Phase C): fail-closed to BLOCK -----------------------
        if hasattr(self._classifier, "evaluate"):
            try:
                action = self._proposed_action(inp)
                veto = self._classifier.evaluate(action)
                blocked = veto is not None
                verdict = {
                    "gate": "efh",
                    "blocked": blocked,
                    "breached": list((veto or {}).get("inviolable_axioms_breached", []))
                    if isinstance(veto, dict)
                    else [],
                }
                if blocked:
                    self._record(result, "CLASSIFY", started, StageStatus.OK, verdict)
                    return True
                # EFH cleared. If a concrete Codex action is present, let Codex
                # write the single (combined) CLASSIFY record; otherwise record
                # the EFH-clear verdict here so there is exactly one CLASSIFY stage.
                meta = getattr(inp, "metadata", {}) or {}
                if meta.get("codex_action") is None:
                    self._record(result, "CLASSIFY", started, StageStatus.OK, verdict)
                    return False
                return self._classify_codex(inp, result, started, verdict=verdict, append=True)
            except Exception as exc:
                # Fail-CLOSED: an EFH failure is a compliance veto, not a pass.
                summary = f"CLASSIFY:efh_error:{type(exc).__name__}:{exc}"
                result.errors.append(summary)
                self._record(
                    result,
                    "CLASSIFY",
                    started,
                    StageStatus.ERROR,
                    {"gate": "efh", "blocked": True, "fail_closed": True},
                    error=summary,
                )
                return True

        # --- skeleton shapes (Phase A posture preserved) ---------------------
        try:
            blocked = False
            verdict = {}
            if hasattr(self._classifier, "is_blocked"):
                blocked = bool(
                    self._classifier.is_blocked(
                        text=getattr(inp, "user_text", "") or "",
                        channel=getattr(inp, "channel", "") or "",
                        plan_token=getattr(inp, "plan_token", "") or "",
                    )
                )
            elif hasattr(self._classifier, "classify"):
                verdict = (
                    self._classifier.classify(
                        text=getattr(inp, "user_text", "") or "",
                        channel=getattr(inp, "channel", "") or "",
                        plan_token=getattr(inp, "plan_token", "") or "",
                    )
                    or {}
                )
                blocked = bool(verdict.get("blocked") or verdict.get("compliance_blocked"))
            self._record(
                result,
                "CLASSIFY",
                started,
                StageStatus.OK,
                {"blocked": blocked, "verdict": verdict},
            )
            return blocked
        except Exception as exc:
            # Skeleton posture: a classifier error degrades to non-blocking.
            self._record(
                result, "CLASSIFY", started, StageStatus.SKIPPED, {"degraded": type(exc).__name__}
            )
            return False

    # ---- CLASSIFY helpers ----------------------------------------------
    @staticmethod
    def _proposed_action(inp: CycleInput) -> dict:
        """Build the EFH ``proposed_action`` dict from the cycle input.

        Prefers an explicit ``inp.metadata['proposed_action']`` (a caller that
        already shaped the action); otherwise synthesises a minimal record whose
        free-text body is the user text + system prompt so the EFH heuristic can
        scan it. Shape mirrors ``EthicalFailureHandler.evaluate`` expectations.
        """
        meta = getattr(inp, "metadata", {}) or {}
        explicit = meta.get("proposed_action")
        if isinstance(explicit, dict):
            return explicit
        return {
            "proposing_agent": "samus",
            "kind": meta.get("action_kind", "cognition_intent"),
            "channel": getattr(inp, "channel", "") or "",
            "body": {
                "user_text": getattr(inp, "user_text", "") or "",
                "system_prompt": getattr(inp, "system_prompt", "") or "",
            },
        }

    def _classify_codex(
        self,
        inp: CycleInput,
        result: CycleResult,
        started: float,
        *,
        verdict: dict,
        append: bool = False,
    ) -> bool:
        """Optional Codex ``check_action`` gate; fail-closed to BLOCK on a veto.

        Only fires when ``inp.metadata['codex_action']`` is present (a duck-typed
        ``ProposedAction``-shaped object). Returns True to BLOCK. A disallowed
        Codex verdict OR a Codex error (e.g. ``CodexUnavailable``) blocks —
        losing the Codex vote on a concrete action is itself a safety failure.
        When no Codex action is present this is a no-op returning False, and (when
        ``append`` is False, i.e. the no-classifier branch) it records the CLASSIFY
        stage as SKIPPED so the audit trail still has exactly one CLASSIFY entry.
        """
        meta = getattr(inp, "metadata", {}) or {}
        codex_action = meta.get("codex_action")
        if codex_action is None:
            # Defensive: callers only reach here with an action present. No
            # action -> nothing for Codex to gate; never blocks, records nothing
            # (the caller owns the single CLASSIFY record in that case).
            return False
        try:
            from backend.common.codex.validator import check_action

            v = check_action(codex_action)
            blocked = not getattr(v, "allowed", True)
            rec = {
                **verdict,
                "codex_blocked": blocked,
                "codex_rule": getattr(v, "violated_rule_id", None),
            }
            self._record(result, "CLASSIFY", started, StageStatus.OK, rec)
            return blocked
        except Exception as exc:
            # Fail-CLOSED: a Codex failure on a concrete action is a veto.
            summary = f"CLASSIFY:codex_error:{type(exc).__name__}:{exc}"
            result.errors.append(summary)
            self._record(
                result,
                "CLASSIFY",
                started,
                StageStatus.ERROR,
                {**verdict, "gate": "codex", "blocked": True, "fail_closed": True},
                error=summary,
            )
            return True

    def _stage_reason(
        self,
        inp: CycleInput,
        perception: dict,
        result: CycleResult,
    ) -> str:
        """REASON — single budget-gated LLM call (Phase C); fail-closed to HOLD.

        GROUNDED prompt: the read-only ``perception`` is rendered into a compact,
        readable CONTEXT block (runway / cash distress + the SPECIFIC top
        opportunity-pending-stake and next follow-up WITH their prospect_ids) via
        :func:`backend.cognitive.domain_perception.render_perception_context`, so
        REASON answers from the live business state rather than guessing.

        Prompt assembly (kept NON-EMPTY always):

        * **Caller supplied user_text** (the manual route) — honour it, but
          PREPEND the perception context: ``"<context>\\n\\n<user_text>"``. When
          there is no perception, the prompt is just the user_text (prior
          behaviour, byte-identical).
        * **No user_text** (the cadence / cognition_tick path) — the prompt is
          the perception context + the ``_REASON_DIRECTIVE`` question; when there
          is no perception either, the directive alone is the prompt (so an
          autonomous tick with a degraded domain still asks a non-empty question
          instead of echoing "").

        Two execution paths, chosen by whether a ``reasoner`` was injected:

        * **No reasoner** (the bare-loop / Phase-A default) — echo the assembled
          prompt as the candidate reply (deterministic, no LLM). With no
          perception and no user_text this is ``_NO_REPLY``, preserving the
          contract/dormancy tests' echo behaviour.
        * **Reasoner wired** (Phase C/F) — call ``reasoner.generate(system=,
          user_text=<assembled prompt>, channel=, plan_token=, metadata=)`` which
          routes ONE budget-gated completion through ``backend.common.llm_client``
          on the ``"cognition"`` workcell. Duck-typed so a stub is injected in
          tests without touching LM Studio.

        Fail-closed to HOLD: ANY exception out of the reasoner — the budget
        denials ``BudgetExceeded`` / ``GlobalBudgetExceeded``, the transport/parse
        ``LlmCallError``, or anything else — is caught here, appended to
        ``result.errors`` via an ERROR StageResult, and converted into the safe
        ``_HOLD_REPLY``. REASON never raises and never fabricates a reply on
        failure, so DECIDE/EMIT still run and the cycle degrades cleanly.
        """
        started = time.perf_counter()
        user_text = (getattr(inp, "user_text", "") or "").strip()
        prompt = self._build_reason_prompt(user_text, perception)
        grounded = prompt != user_text  # perception was folded in

        if self._reasoner is None or not hasattr(self._reasoner, "generate"):
            # Bare-loop / Phase-A path: deterministic echo, no LLM.
            try:
                reply = prompt or _NO_REPLY
                self._record(
                    result,
                    "REASON",
                    started,
                    StageStatus.OK,
                    {"stub": True, "grounded": grounded, "chars": len(reply)},
                )
                return reply
            except Exception as exc:  # pragma: no cover — echo can't really fail
                self._record_error(result, "REASON", started, exc)
                return _NO_REPLY

        # Wired-reasoner path: one budget-gated LLM call, fail-closed to HOLD.
        try:
            reply = self._reasoner.generate(
                system=getattr(inp, "system_prompt", "") or "",
                user_text=prompt,
                channel=getattr(inp, "channel", "") or "",
                plan_token=getattr(inp, "plan_token", "") or "",
                metadata=dict(getattr(inp, "metadata", {}) or {}),
            )
            reply = (reply or "").strip() or _NO_REPLY
            self._record(
                result,
                "REASON",
                started,
                StageStatus.OK,
                {"stub": False, "grounded": grounded, "chars": len(reply)},
            )
            return reply
        except Exception as exc:
            # Budget-denied (BudgetExceeded/GlobalBudgetExceeded), transport/parse
            # (LlmCallError), or any other error -> HOLD. Recorded as an ERROR
            # stage + an errors entry; NEVER re-raised.
            self._record_error(result, "REASON", started, exc)
            return _HOLD_REPLY

    @staticmethod
    def _build_reason_prompt(user_text: str, perception: dict) -> str:
        """Assemble the grounded REASON prompt; ALWAYS non-empty when there is
        anything to say. Fail-safe: a rendering error degrades to the bare
        user_text / directive rather than raising.

        * caller user_text present -> ``"<perception context>\\n\\n<user_text>"``
          (or just ``user_text`` when no perception).
        * no user_text (cadence tick) -> ``"<perception context>\\n\\n<directive>"``
          (or just the directive when no perception).
        """
        try:
            from .domain_perception import render_perception_context

            context = render_perception_context(perception if isinstance(perception, dict) else {})
        except Exception as exc:  # noqa: BLE001 — grounding is best-effort
            log.warning("REASON prompt grounding failed: %s", exc)
            context = ""

        if user_text:
            if context:
                return f"{context}\n\n{user_text}"
            return user_text
        # Cadence / cognition_tick path: directive question (grounded when we can).
        if context:
            return f"{context}\n\n{_REASON_DIRECTIVE}"
        return _REASON_DIRECTIVE

    def _stage_decide(self, reply: str, result: CycleResult) -> str:
        """DECIDE — select the reply candidate (Phase A: single candidate)."""
        started = time.perf_counter()
        try:
            chosen = reply or _NO_REPLY
            self._record(result, "DECIDE", started, StageStatus.OK, {"chars": len(chosen)})
            return chosen
        except Exception as exc:
            self._record_error(result, "DECIDE", started, exc)
            return _NO_REPLY

    def _stage_act(
        self,
        inp: CycleInput,
        reply: str,
        perception: dict,
        result: CycleResult,
    ) -> None:
        """ACT — Phase-E PROPOSE-ONLY: record a structured proposal artifact.

        This is the propose-only effector boundary. When ARMED it emits ONE
        structured proposal — a would-be commercial intent derived from the
        read-only ``perception`` + the cycle's ``reply`` / ``reflect_score`` —
        and APPENDS it to ``state_path("cognition","proposals.jsonl")`` (or the
        injected ``proposal_sink`` spy in tests). It calls NO effector and NO
        gate: not ``review_opportunity``, not the cash_engine gate, not the
        outreach send path, not any finance entrypoint, no network. ACT ONLY
        records a proposal — disposition stays with the governance gates and
        (Phase G, operator-gated) a separate promoter.

        Gated to a PURE NO-OP when disabled (the default): if the
        ``cognitive_act_proposals_enabled`` flag is False the stage records an
        inert ``actuated=False`` result and writes nothing — byte-identical to
        the prior phase's no-op stub. Fail-closed: any error is an ERROR stage,
        never a raise, and never a partial/duplicate side effect beyond the one
        best-effort append.
        """
        started = time.perf_counter()
        try:
            if not self._act_proposals_enabled():
                # Disabled => pure no-op (no proposal written). Dormant default.
                self._record(
                    result,
                    "ACT",
                    started,
                    StageStatus.OK,
                    {"stub": True, "actuated": False, "proposed": False},
                )
                return

            # Armed: build + record exactly one proposal artifact. Imports here
            # so the disabled path (and module import) never loads the writer.
            from .domain_perception import build_proposal, record_proposal

            proposal = build_proposal(
                plan_token=result.plan_token,
                channel=getattr(inp, "channel", "") or "",
                reply=reply,
                perception=perception if isinstance(perception, dict) else {},
                reflect_score=result.reflect_score,
            )
            wrote = record_proposal(proposal, sink=self._proposal_sink)
            self._record(
                result,
                "ACT",
                started,
                StageStatus.OK,
                {
                    "stub": False,
                    # actuated stays False: a PROPOSAL is not an actuation. The
                    # contract test asserts ACT calls no effector; "proposed"
                    # records that a proposal artifact was written.
                    "actuated": False,
                    "proposed": wrote,
                    "intent": proposal.get("intent", {}).get("type"),
                },
            )
        except Exception as exc:
            self._record_error(result, "ACT", started, exc)

    @staticmethod
    def _act_proposals_enabled() -> bool:
        """Read the propose-only-ACT flag (default OFF). Fail-closed to OFF.

        Resolves ``cognitive_act_proposals_enabled`` off the live settings. Any
        failure reading settings degrades to OFF (the dormant, no-write posture),
        so a settings hiccup can never accidentally arm the proposal writer.
        """
        try:
            from backend.common.config import get_settings

            return bool(getattr(get_settings(), "cognitive_act_proposals_enabled", False))
        except Exception:  # noqa: BLE001 — fail-closed to dormant
            return False

    def _stage_reflect(self, inp: CycleInput, reply: str, result: CycleResult) -> None:
        """REFLECT — self-critique score when a reflector is wired; else 1.0.

        A wired ``reflector`` exposing ``score(user_text=,reply=) -> float``
        sets ``result.reflect_score``. No reflector => the default converged
        score (1.0) stands. Fail-closed: an error leaves the prior score and
        records an ERROR stage.
        """
        started = time.perf_counter()
        try:
            if self._reflector is not None and hasattr(self._reflector, "score"):
                score = float(
                    self._reflector.score(
                        user_text=getattr(inp, "user_text", "") or "",
                        reply=reply,
                    )
                )
                result.reflect_score = score
            self._record(
                result,
                "REFLECT",
                started,
                StageStatus.OK,
                {"reflect_score": result.reflect_score},
            )
        except Exception as exc:
            self._record_error(result, "REFLECT", started, exc)

    def _stage_persist(self, inp: CycleInput, result: CycleResult) -> None:
        """PERSIST — durable cycle snapshot when a persister is wired; else no-op.

        A wired ``persister`` exposing ``snapshot(plan_token=,compliance_blocked=,
        error_count=)`` (the ArchitecturePersistence Contract-2 shape) is
        invoked best-effort. Fail-closed: an error records an ERROR stage but
        never breaks the cycle.
        """
        started = time.perf_counter()
        try:
            if self._persister is not None and hasattr(self._persister, "snapshot"):
                self._persister.snapshot(
                    plan_token=result.plan_token,
                    compliance_blocked=result.compliance_blocked,
                    error_count=len(result.errors),
                )
            self._record(
                result,
                "PERSIST",
                started,
                StageStatus.OK,
                {"persisted": self._persister is not None},
            )
        except Exception as exc:
            self._record_error(result, "PERSIST", started, exc)

    def _stage_emit(self, inp: CycleInput, result: CycleResult) -> None:
        """EMIT — finalise the reply.

        On the blocked path (CLASSIFY short-circuit) the happy-path stages never
        ran, so ``result.reply`` is still the constructor default (""). EMIT now
        supplies a safe, fixed compliance-refusal reply (Phase C). On the happy
        path the caller has already set ``result.reply`` (the REASON/DECIDE
        candidate, or the ``_HOLD_REPLY`` when REASON failed closed). Fail-closed.
        """
        started = time.perf_counter()
        try:
            if result.compliance_blocked and not result.reply:
                result.reply = _BLOCKED_REPLY
            self._record(
                result,
                "EMIT",
                started,
                StageStatus.OK,
                {"reply_chars": len(result.reply or ""), "blocked": result.compliance_blocked},
            )
        except Exception as exc:
            self._record_error(result, "EMIT", started, exc)


__all__ = ["CognitiveLoop", "STAGE_NAMES", "build_llm_reasoner"]
