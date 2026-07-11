"""Phase G — propose-only artifacts promoted INTO review_opportunity.

Covers the build-plan Phase-G test bullets:

  (a) **Flag OFF -> pure no-op.** With ``cognition_proposal_promotion_enabled``
      False, ``ProposalPromoter.run()`` reads nothing, calls nothing, writes
      nothing — asserted with a RAISING review-spy and a RAISING telemetry-spy
      that fail loudly if invoked.

  (b) **Flag ON + PASS verdict.** ``review_opportunity`` returns
      ``status=='enqueued'`` -> the proposal is marked ``actioned=True`` with
      ``promotion_result.status=='pass'``; one telemetry row written.

  (c) **Flag ON + BLOCK verdict.** ``review_opportunity`` returns an
      ``escalated`` / Codex-rule / Stake-Sentence verdict -> proposal marked
      actioned + ``status=='blocked'`` + reason + ``violated_rule_id`` /
      ``required_protocol`` echoed. A second ``run()`` does NOT retry it
      (BLOCK is sticky).

  (d) **Flag ON + ``review_opportunity`` RAISES.** Fail-closed:
      ``promotion_result.status=='error'``, ``actioned=True``, and the
      exception NEVER propagates out of the promoter.

  (e) **Idempotency.** Running the promoter twice over the same proposals.jsonl
      never double-promotes any row.

  (f) **Compliance-blocked cycles do NOT promote.** A cycle whose
      ``compliance_blocked`` is True writes no proposal to begin with, so the
      runner seam invokes the promoter zero times; the spy proves it.

  (g) **Runner integration.** ``run_one_cycle`` with the flag True + ACT
      enabled + a written proposal triggers EXACTLY ONE promotion attempt
      (review-spy call-count == 1); with the flag False the same cycle
      triggers ZERO attempts (spy-asserted).

Isolation: every test uses an injected stub for ``review_opportunity`` so the
live cash-engine + Codex + EFH + karma are NEVER touched. The proposals JSONL
ledger lives under ``tmp_path`` (``SAMUS_STATE_ROOT`` override), the telemetry
sink is a spy. No DDB / network / LM Studio.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from backend.cognitive.cycle_models import CycleInput


# ---------------------------------------------------------------------------
# Flag helpers — arm/disarm the Phase-G kill-switch + the runner master + ACT.
# ---------------------------------------------------------------------------
def _arm_promotion(monkeypatch, state_root=None):
    if state_root is not None:
        monkeypatch.setenv("SAMUS_STATE_ROOT", str(state_root))
    monkeypatch.setenv("SAMUS_COGNITION_PROPOSAL_PROMOTION_ENABLED", "true")
    from backend.common.settings import reload_settings

    reload_settings()


def _disarm_promotion(monkeypatch, state_root=None):
    if state_root is not None:
        monkeypatch.setenv("SAMUS_STATE_ROOT", str(state_root))
    monkeypatch.setenv("SAMUS_COGNITION_PROPOSAL_PROMOTION_ENABLED", "false")
    from backend.common.settings import reload_settings

    reload_settings()


def _arm_loop(monkeypatch, state_root=None):
    if state_root is not None:
        monkeypatch.setenv("SAMUS_STATE_ROOT", str(state_root))
    monkeypatch.setenv("SAMUS_COGNITIVE_LOOP_ENABLED", "true")
    monkeypatch.setenv("SAMUS_COGNITIVE_ACT_PROPOSALS_ENABLED", "true")
    from backend.common.settings import reload_settings

    reload_settings()


# ---------------------------------------------------------------------------
# Stubs / spies
# ---------------------------------------------------------------------------
class _RaisingReviewSpy:
    """Any invocation fails the test loudly — proves the disabled path stays cold."""

    def __init__(self):
        self.calls = 0

    def __call__(self, *_a, **_k):
        self.calls += 1
        raise AssertionError(
            "review_opportunity MUST NOT be called when promotion is disabled"
        )


class _RaisingSink:
    """A telemetry sink that fails the test if anything appends to it."""

    def append(self, *_a, **_k):
        raise AssertionError(
            "telemetry MUST NOT be appended when promotion is disabled"
        )


class _RecordingSink:
    """Capturing spy sink — records every appended row in order."""

    def __init__(self):
        self.rows = []

    def append(self, record):
        self.rows.append(record)


class _FakeRevenueTriggerResult:
    """Duck-typed stand-in for ``cash_engine.models.RevenueTriggerResult``.

    The promoter only ever reads attributes off the result; no Pydantic
    construction in tests so we can shape arbitrary verdicts (PASS / BLOCK /
    invalid / disabled) cleanly.
    """

    def __init__(
        self,
        *,
        status,
        prospect_id="prospect-1",
        opportunity_id="opp-1",
        task_id="",
        required_protocol=None,
        violated_rule_id=None,
        reason=None,
    ):
        self.accepted = status == "enqueued"
        self.status = status
        self.prospect_id = prospect_id
        self.opportunity_id = opportunity_id
        self.task_id = task_id
        self.required_protocol = required_protocol
        self.violated_rule_id = violated_rule_id
        self.reason = reason


class _PassReview:
    """Stub ``review_opportunity`` returning a PASS (enqueued) verdict."""

    def __init__(self):
        self.calls = []

    def __call__(self, req, **_k):
        self.calls.append(req)
        return _FakeRevenueTriggerResult(
            status="enqueued",
            prospect_id=req.prospect_id,
            task_id="ce-pass-001",
        )


class _BlockReview:
    """Stub ``review_opportunity`` returning a BLOCK (Codex-violation) verdict."""

    def __init__(self):
        self.calls = []

    def __call__(self, req, **_k):
        self.calls.append(req)
        return _FakeRevenueTriggerResult(
            status="escalated",
            prospect_id=req.prospect_id,
            violated_rule_id="VR-G2",
            required_protocol=None,
            reason="banned phrase in body",
        )


class _StakeMissingReview:
    """Stub ``review_opportunity`` returning a stake-sentence escalation."""

    def __init__(self):
        self.calls = []

    def __call__(self, req, **_k):
        self.calls.append(req)
        return _FakeRevenueTriggerResult(
            status="escalated",
            prospect_id=req.prospect_id,
            required_protocol="stake_sentence",
            reason="Opportunity has no operator-authored Stake Sentence",
        )


class _RaisingReview:
    """Stub ``review_opportunity`` that raises (CodexUnavailable etc.)."""

    def __init__(self, exc=None):
        self.exc = exc or RuntimeError("simulated CodexUnavailable")
        self.calls = []

    def __call__(self, req, **_k):
        self.calls.append(req)
        raise self.exc


# ---------------------------------------------------------------------------
# Proposal-row factories
# ---------------------------------------------------------------------------
def _proposal_row(*, plan_token="tok-1", intent_type="stake_then_outreach",
                  prospect_id="prospect-1", actioned=False, ts=None):
    """A Phase-E shaped cognition_proposal row, possibly with a prospect_id."""
    from backend.common.dates import iso_now

    intent = {"type": intent_type, "reason": "test"}
    if prospect_id is not None:
        intent["prospect_id"] = prospect_id
    return {
        "ts": ts or iso_now(),
        "kind": "cognition_proposal",
        "status": "proposed",
        "actioned": actioned,
        "plan_token": plan_token,
        "channel": "chat",
        "intent": intent,
        "reply_preview": "(stub reply)",
        "reflect_score": 1.0,
        "perception_summary": {"available": [], "degraded": []},
        "note": "PROPOSAL ONLY — recorded by the cognitive loop",
    }


def _write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def _read_jsonl(path):
    out = []
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        out.append(json.loads(line))
    return out


# ===========================================================================
# (a) flag OFF -> ProposalPromoter.run() is a pure no-op
# ===========================================================================
def test_a_flag_off_promoter_run_is_pure_noop(tmp_path, monkeypatch):
    _disarm_promotion(monkeypatch, tmp_path / "state")

    # Seed a proposals ledger that, IF read, would obviously try to promote.
    ledger = tmp_path / "state" / "cognition" / "proposals.jsonl"
    _write_jsonl(ledger, [_proposal_row(plan_token="tok-off")])

    from backend.cognitive.proposal_promoter import ProposalPromoter

    review_spy = _RaisingReviewSpy()
    telemetry_spy = _RaisingSink()
    promoter = ProposalPromoter(
        review=review_spy,
        ledger_path=ledger,
        telemetry=telemetry_spy,
    )

    results = promoter.run()

    # Pure no-op.
    assert results == []
    assert review_spy.calls == 0
    # No telemetry write (the raising sink would have raised).
    # No mutation of the ledger.
    rows = _read_jsonl(ledger)
    assert len(rows) == 1
    assert rows[0]["actioned"] is False


# ===========================================================================
# (b) flag ON + PASS -> actioned=True, promotion_result.status='pass', ledger row
# ===========================================================================
def test_b_flag_on_pass_marks_actioned_and_writes_telemetry(tmp_path, monkeypatch):
    _arm_promotion(monkeypatch, tmp_path / "state")
    ledger = tmp_path / "state" / "cognition" / "proposals.jsonl"
    _write_jsonl(ledger, [_proposal_row(plan_token="tok-pass", prospect_id="p-101")])

    from backend.cognitive.proposal_promoter import ProposalPromoter

    review = _PassReview()
    telemetry = _RecordingSink()
    promoter = ProposalPromoter(
        review=review,
        ledger_path=ledger,
        telemetry=telemetry,
    )

    results = promoter.run()

    assert len(results) == 1
    assert results[0].status == "pass"
    assert results[0].review_status == "enqueued"
    assert results[0].task_id == "ce-pass-001"
    assert results[0].prospect_id == "p-101"

    # Exactly one ``review_opportunity`` call, with a well-formed request.
    assert len(review.calls) == 1
    req = review.calls[0]
    assert req.prospect_id == "p-101"
    assert req.trigger_source == "manual_review"
    assert req.trigger_reason.startswith("cognition_promotion:")

    # Durable mark appended (the original "proposed" row stays AND a flipped
    # row with actioned=True is appended).
    rows = _read_jsonl(ledger)
    assert len(rows) == 2
    assert rows[0]["actioned"] is False
    assert rows[1]["actioned"] is True
    assert rows[1]["promotion_result"]["status"] == "pass"

    # One telemetry row.
    assert len(telemetry.rows) == 1
    from backend.cognitive.proposal_promoter import EVENT_NAME
    assert telemetry.rows[0]["event"] == EVENT_NAME
    assert telemetry.rows[0]["verdict"]["status"] == "pass"


# ===========================================================================
# (c) flag ON + BLOCK -> actioned + status='blocked' + reason; NOT retried
# ===========================================================================
def test_c_flag_on_block_records_and_never_retries(tmp_path, monkeypatch):
    _arm_promotion(monkeypatch, tmp_path / "state")
    ledger = tmp_path / "state" / "cognition" / "proposals.jsonl"
    _write_jsonl(ledger, [_proposal_row(plan_token="tok-block", prospect_id="p-block")])

    from backend.cognitive.proposal_promoter import ProposalPromoter

    review = _BlockReview()
    telemetry = _RecordingSink()
    promoter = ProposalPromoter(
        review=review,
        ledger_path=ledger,
        telemetry=telemetry,
    )

    # First run — Codex BLOCK comes back.
    first = promoter.run()
    assert len(first) == 1
    assert first[0].status == "blocked"
    assert first[0].review_status == "escalated"
    assert first[0].violated_rule_id == "VR-G2"
    assert first[0].reason and "banned phrase" in first[0].reason

    # Ledger now has the original + flipped (actioned) row.
    rows_after_first = _read_jsonl(ledger)
    assert len(rows_after_first) == 2
    assert rows_after_first[1]["actioned"] is True
    assert rows_after_first[1]["promotion_result"]["status"] == "blocked"

    # Telemetry has one row.
    assert len(telemetry.rows) == 1

    # Second run — BLOCK is sticky. Promoter MUST NOT re-call review_opportunity.
    second = promoter.run()
    assert second == []
    assert len(review.calls) == 1, (
        "BLOCK is sticky — the promoter must not retry already-actioned proposals"
    )
    # No new telemetry row.
    assert len(telemetry.rows) == 1


# ===========================================================================
# (c') flag ON + stake-sentence BLOCK -> required_protocol echoed
# ===========================================================================
def test_c2_stake_sentence_block_echoes_required_protocol(tmp_path, monkeypatch):
    _arm_promotion(monkeypatch, tmp_path / "state")
    ledger = tmp_path / "state" / "cognition" / "proposals.jsonl"
    _write_jsonl(ledger, [_proposal_row(plan_token="tok-stake", prospect_id="p-stake")])

    from backend.cognitive.proposal_promoter import ProposalPromoter

    review = _StakeMissingReview()
    telemetry = _RecordingSink()
    promoter = ProposalPromoter(
        review=review,
        ledger_path=ledger,
        telemetry=telemetry,
    )
    results = promoter.run()
    assert len(results) == 1
    assert results[0].status == "blocked"
    assert results[0].required_protocol == "stake_sentence"


# ===========================================================================
# (d) flag ON + review_opportunity RAISES -> fail-closed to status='error'
# ===========================================================================
def test_d_flag_on_review_raises_is_fail_closed_to_error(tmp_path, monkeypatch):
    _arm_promotion(monkeypatch, tmp_path / "state")
    ledger = tmp_path / "state" / "cognition" / "proposals.jsonl"
    _write_jsonl(ledger, [_proposal_row(plan_token="tok-err", prospect_id="p-err")])

    from backend.cognitive.proposal_promoter import ProposalPromoter

    review = _RaisingReview(exc=RuntimeError("CodexUnavailable: registry not loaded"))
    telemetry = _RecordingSink()
    promoter = ProposalPromoter(
        review=review,
        ledger_path=ledger,
        telemetry=telemetry,
    )

    # The exception MUST NOT propagate out — the cadence keeps running.
    results = promoter.run()

    assert len(results) == 1
    assert results[0].status == "error"
    assert "CodexUnavailable" in (results[0].reason or "")
    # Row marked actioned (so a re-run won't retry the same row, mirroring the
    # BLOCK-is-sticky posture for hard errors).
    rows = _read_jsonl(ledger)
    assert len(rows) == 2
    assert rows[1]["actioned"] is True
    assert rows[1]["promotion_result"]["status"] == "error"


# ===========================================================================
# (e) idempotency — running twice never double-promotes
# ===========================================================================
def test_e_idempotent_double_run_does_not_double_promote(tmp_path, monkeypatch):
    _arm_promotion(monkeypatch, tmp_path / "state")
    ledger = tmp_path / "state" / "cognition" / "proposals.jsonl"
    _write_jsonl(ledger, [
        _proposal_row(plan_token="tok-a", prospect_id="p-A"),
        _proposal_row(plan_token="tok-b", prospect_id="p-B"),
    ])

    from backend.cognitive.proposal_promoter import ProposalPromoter

    review = _PassReview()
    telemetry = _RecordingSink()
    promoter = ProposalPromoter(
        review=review,
        ledger_path=ledger,
        telemetry=telemetry,
    )

    first = promoter.run()
    assert len(first) == 2
    assert len(review.calls) == 2

    second = promoter.run()
    assert second == [], "second run must be a no-op — every row is now actioned"
    assert len(review.calls) == 2, "review_opportunity must not be called again"
    # Telemetry stayed at 2 rows from the first run.
    assert len(telemetry.rows) == 2


# ===========================================================================
# (f) compliance-blocked cycles do NOT promote
# ===========================================================================
def test_f_compliance_blocked_cycle_never_invokes_promoter(tmp_path, monkeypatch):
    """A CLASSIFY-vetoed cycle writes no proposal (ACT is skipped) -> the
    runner seam SHOULD invoke the promoter zero times."""
    _arm_promotion(monkeypatch, tmp_path / "state")
    _arm_loop(monkeypatch, tmp_path / "state")

    # Stub an EFH classifier that BLOCKS (returns a non-None veto record).
    class _BlockingEFH:
        def evaluate(self, _action):
            return {"inviolable_axioms_breached": ["axiom.inviolable.human_dignity_floor"]}

    # Stubs for the other deps (no-op reasoner / domain / sink).
    class _NoopReasoner:
        def generate(self, **_k):
            return "(stub reply)"

    class _NoopDomain:
        def follow_ups_due(self):
            return {"count": 0}

    class _CaptureSink:
        def __init__(self):
            self.rows = []

        def append(self, r):
            self.rows.append(r)

        @property
        def last(self):
            return self.rows[-1] if self.rows else None

    # Raising review-spy: any promotion attempt fails the test loudly.
    raising_review = _RaisingReviewSpy()
    raising_telemetry = _RaisingSink()

    from backend.cognitive.runner import run_one_cycle

    sink = _CaptureSink()
    summary = asyncio.run(
        run_one_cycle(
            CycleInput(user_text="manipulate the prospect", plan_token="tok-blocked"),
            domain=_NoopDomain(),
            reasoner=_NoopReasoner(),
            classifier=_BlockingEFH(),
            proposal_sink=sink,
            promotion_review=raising_review,
            promotion_telemetry=raising_telemetry,
        )
    )

    # Cycle was compliance-blocked.
    assert summary.compliance_blocked is True
    # ACT was short-circuited -> no proposal was written.
    assert summary.proposal_written is False
    assert sink.rows == []
    # The promoter was NOT invoked (raising spies would have raised).
    assert raising_review.calls == 0
    # Summary echoes "no promotion was attempted".
    assert summary.promotion_attempted is False
    assert summary.promotion_result is None


# ===========================================================================
# (g) runner integration: flag ON + ACT enabled -> exactly one promotion attempt
# ===========================================================================
def test_g_runner_integration_flag_on_yields_one_promotion_attempt(tmp_path, monkeypatch):
    _arm_promotion(monkeypatch, tmp_path / "state")
    _arm_loop(monkeypatch, tmp_path / "state")

    # Stubs (mirrors Phase-F stubs).
    class _StubReasoner:
        def __init__(self):
            self.calls = 0

        def generate(self, **_k):
            self.calls += 1
            return "[stub] go"

    class _StubClassifier:
        def evaluate(self, _action):
            return None  # clears

    class _StubDomain:
        # A single pending-stake deal -> intent.type=='stake_then_outreach'.
        def follow_ups_due(self):
            return {"count": 0}

        def opportunities_pending_stake(self):
            return {"count": 1}

        def open_operator_tasks(self):
            return {"count": 0}

        def decay_risk(self, *, threshold=0.6):
            return {"crossing": 0, "worst_risk": 0.0, "threshold": threshold, "assessed": 0}

        def finance_runway(self):
            return {"days_of_runway": 90.0, "alert_triggered": False, "available_balance_usd": 500.0}

        def finance_actions(self):
            return {"open_total": 0, "overdue_count": 0, "due_today_count": 0}

        def cash_distress(self):
            return {"cash_distress": "ok", "distress_reasons": []}

    # An injected proposal sink + an attached prospect_id (Phase-E intent today
    # carries only counts; a forward caller may stamp prospect_id directly).
    # The runner seam reads ``summary.proposal`` straight from the sink, so we
    # monkey-stamp prospect_id before the runner reads it back via a wrapping
    # sink.
    class _StampingSink:
        def __init__(self):
            self.rows = []

        def append(self, record):
            # Stamp a prospect_id onto the intent so Phase G has something to
            # route. In live use the upgraded Phase-E intent derivation will
            # carry this through; here we patch in-place for the seam test.
            intent = dict(record.get("intent") or {})
            intent["prospect_id"] = "p-runner"
            record["intent"] = intent
            self.rows.append(record)

        @property
        def last(self):
            return self.rows[-1] if self.rows else None

    review = _PassReview()
    telemetry = _RecordingSink()

    from backend.cognitive.runner import run_one_cycle

    summary = asyncio.run(
        run_one_cycle(
            CycleInput(user_text="what next?", plan_token="tok-runner", channel="chat"),
            domain=_StubDomain(),
            reasoner=_StubReasoner(),
            classifier=_StubClassifier(),
            proposal_sink=_StampingSink(),
            promotion_review=review,
            promotion_telemetry=telemetry,
        )
    )

    # The cycle ran end-to-end and a proposal was written.
    assert summary.ok is True
    assert summary.compliance_blocked is False
    assert summary.proposal_written is True
    # Phase G fired EXACTLY ONCE for the just-written proposal.
    assert summary.promotion_attempted is True
    assert summary.promotion_enabled is True
    assert summary.promotion_result is not None
    assert summary.promotion_result["status"] == "pass"
    assert len(review.calls) == 1
    assert len(telemetry.rows) == 1


def test_g2_runner_integration_flag_off_yields_zero_promotion_attempts(tmp_path, monkeypatch):
    """Same cycle as (g) but with the Phase-G kill-switch OFF -> zero attempts."""
    _disarm_promotion(monkeypatch, tmp_path / "state")
    _arm_loop(monkeypatch, tmp_path / "state")

    class _StubReasoner:
        def generate(self, **_k):
            return "[stub] go"

    class _StubClassifier:
        def evaluate(self, _action):
            return None

    class _StubDomain:
        def follow_ups_due(self):
            return {"count": 0}

        def opportunities_pending_stake(self):
            return {"count": 1}

        def open_operator_tasks(self):
            return {"count": 0}

        def decay_risk(self, *, threshold=0.6):
            return {"crossing": 0, "worst_risk": 0.0, "threshold": threshold, "assessed": 0}

        def finance_runway(self):
            return {"days_of_runway": 90.0, "alert_triggered": False, "available_balance_usd": 500.0}

        def finance_actions(self):
            return {"open_total": 0, "overdue_count": 0, "due_today_count": 0}

        def cash_distress(self):
            return {"cash_distress": "ok", "distress_reasons": []}

    class _CaptureSink:
        def __init__(self):
            self.rows = []

        def append(self, r):
            self.rows.append(r)

        @property
        def last(self):
            return self.rows[-1] if self.rows else None

    raising_review = _RaisingReviewSpy()
    raising_telemetry = _RaisingSink()

    from backend.cognitive.runner import run_one_cycle

    sink = _CaptureSink()
    summary = asyncio.run(
        run_one_cycle(
            CycleInput(user_text="what next?", plan_token="tok-off-runner", channel="chat"),
            domain=_StubDomain(),
            reasoner=_StubReasoner(),
            classifier=_StubClassifier(),
            proposal_sink=sink,
            promotion_review=raising_review,
            promotion_telemetry=raising_telemetry,
        )
    )

    assert summary.ok is True
    assert summary.proposal_written is True
    # Phase-G flag OFF -> the promoter is a pure no-op even though the cycle wrote.
    assert summary.promotion_enabled is False
    assert summary.promotion_attempted is False
    assert summary.promotion_result is None
    # Raising review-spy proves zero call attempts.
    assert raising_review.calls == 0


# ===========================================================================
# Per-prospect BLOCK cooldown — stop re-promoting the SAME blocked prospect
# every tick. After a BLOCK, the same prospect_id is suppressed (SKIP without
# re-calling review_opportunity) for cognition_promotion_block_cooldown_sec.
# ===========================================================================
def _reset_block_cooldown():
    """The cooldown cache is MODULE-LEVEL (persists across promoter instances,
    and across tests in a process). Reset it for test isolation."""
    from backend.cognitive import proposal_promoter as pp

    pp._BLOCK_COOLDOWN.clear()


def _promote_one_pending(ledger, review, telemetry, *, plan_token, prospect_id):
    """Seed a single pending-stake proposal for `prospect_id` and promote it.

    Each tick of the live cadence re-derives the SAME proposal for a pending-
    stake prospect, so we model a tick by writing a fresh unactioned row and
    calling promote_one on it directly (bypassing the per-row idempotency that
    a re-read of the same physical row would otherwise short-circuit on)."""
    from backend.cognitive.proposal_promoter import ProposalPromoter

    promoter = ProposalPromoter(review=review, ledger_path=ledger, telemetry=telemetry)
    row = _proposal_row(plan_token=plan_token, prospect_id=prospect_id)
    return promoter.promote_one(row, _bypass_flag_check=True)


def test_i_block_cooldown_suppresses_same_prospect_then_expires(tmp_path, monkeypatch):
    """3 ticks for the SAME prospect: (1) BLOCK, review called, cooldown set;
    (2) within cooldown -> SKIP 'block_cooldown', review NOT called again;
    (3) after monotonic advances past the cooldown -> review called again."""
    _reset_block_cooldown()
    # Short, deterministic cooldown window for the test.
    monkeypatch.setenv("SAMUS_COGNITION_PROMOTION_BLOCK_COOLDOWN_SEC", "600")
    _arm_promotion(monkeypatch, tmp_path / "state")

    ledger = tmp_path / "state" / "cognition" / "proposals.jsonl"
    review = _BlockReview()
    telemetry = _RecordingSink()

    import backend.cognitive.proposal_promoter as pp

    # Pin a controllable clock.
    clock = {"t": 1000.0}
    monkeypatch.setattr(pp.time, "monotonic", lambda: clock["t"])

    # --- Tick 1: first promotion -> BLOCK, review called once, cooldown armed.
    r1 = _promote_one_pending(
        ledger, review, telemetry, plan_token="tok-cd-1", prospect_id="p-cd",
    )
    assert r1.status == "blocked"
    assert len(review.calls) == 1
    assert "p-cd" in pp._BLOCK_COOLDOWN

    # --- Tick 2: still inside the cooldown window -> SKIP, review NOT re-called.
    clock["t"] = 1000.0 + 300.0  # 300s < 600s window
    r2 = _promote_one_pending(
        ledger, review, telemetry, plan_token="tok-cd-2", prospect_id="p-cd",
    )
    assert r2.status == "skipped"
    assert r2.reason == "block_cooldown"
    assert r2.prospect_id == "p-cd"
    assert len(review.calls) == 1, "review_opportunity MUST NOT be called within cooldown"
    # The cooldown skip is still recorded in telemetry (audit trail).
    assert telemetry.rows[-1]["verdict"]["status"] == "skipped"
    assert telemetry.rows[-1]["verdict"]["reason"] == "block_cooldown"

    # --- Tick 3: clock advances PAST the cooldown -> review called again.
    clock["t"] = 1000.0 + 600.0 + 1.0  # just past the 600s window
    r3 = _promote_one_pending(
        ledger, review, telemetry, plan_token="tok-cd-3", prospect_id="p-cd",
    )
    assert r3.status == "blocked"
    assert len(review.calls) == 2, "after the cooldown expires the gate is consulted again"


def test_j_block_cooldown_is_per_prospect_not_global(tmp_path, monkeypatch):
    """A BLOCK on prospect A must NOT suppress a DIFFERENT prospect B."""
    _reset_block_cooldown()
    monkeypatch.setenv("SAMUS_COGNITION_PROMOTION_BLOCK_COOLDOWN_SEC", "600")
    _arm_promotion(monkeypatch, tmp_path / "state")

    ledger = tmp_path / "state" / "cognition" / "proposals.jsonl"
    review = _BlockReview()
    telemetry = _RecordingSink()

    import backend.cognitive.proposal_promoter as pp

    clock = {"t": 5000.0}
    monkeypatch.setattr(pp.time, "monotonic", lambda: clock["t"])

    # Prospect A blocks -> cooldown armed for A only.
    rA = _promote_one_pending(
        ledger, review, telemetry, plan_token="tok-A", prospect_id="p-A",
    )
    assert rA.status == "blocked"
    assert len(review.calls) == 1
    assert "p-A" in pp._BLOCK_COOLDOWN
    assert "p-B" not in pp._BLOCK_COOLDOWN

    # Prospect B (different id) within A's window is NOT suppressed -> reviewed.
    rB = _promote_one_pending(
        ledger, review, telemetry, plan_token="tok-B", prospect_id="p-B",
    )
    assert rB.status == "blocked", "a different prospect is not gated by A's cooldown"
    assert len(review.calls) == 2, "prospect B must reach review_opportunity"

    # Re-promoting A within its window still SKIPs (sanity: A is still cooling).
    rA2 = _promote_one_pending(
        ledger, review, telemetry, plan_token="tok-A2", prospect_id="p-A",
    )
    assert rA2.status == "skipped"
    assert rA2.reason == "block_cooldown"
    assert len(review.calls) == 2, "A is still in cooldown -> no extra review call"


def test_k_pass_verdict_does_not_arm_block_cooldown(tmp_path, monkeypatch):
    """Only a BLOCK arms the cooldown. A PASS must leave _BLOCK_COOLDOWN empty so
    a healthy prospect is never suppressed."""
    _reset_block_cooldown()
    _arm_promotion(monkeypatch, tmp_path / "state")

    ledger = tmp_path / "state" / "cognition" / "proposals.jsonl"
    review = _PassReview()
    telemetry = _RecordingSink()

    import backend.cognitive.proposal_promoter as pp

    r1 = _promote_one_pending(
        ledger, review, telemetry, plan_token="tok-pass-cd", prospect_id="p-pass",
    )
    assert r1.status == "pass"
    assert "p-pass" not in pp._BLOCK_COOLDOWN

    # A second tick for the same prospect is NOT suppressed -> review re-called.
    r2 = _promote_one_pending(
        ledger, review, telemetry, plan_token="tok-pass-cd-2", prospect_id="p-pass",
    )
    assert r2.status == "pass"
    assert len(review.calls) == 2


# ===========================================================================
# Defensive bonus — proposal without prospect_id -> SKIPPED, not promoted
# ===========================================================================
def test_h_proposal_without_prospect_id_is_skipped_not_promoted(tmp_path, monkeypatch):
    _arm_promotion(monkeypatch, tmp_path / "state")
    ledger = tmp_path / "state" / "cognition" / "proposals.jsonl"
    # Proposal whose intent carries no prospect_id (the current Phase-E shape
    # for many intent types). Phase G cannot synthesise a RevenueTriggerRequest
    # without it, so SKIPs (not BLOCKED — gates would have nothing to gate).
    _write_jsonl(ledger, [_proposal_row(plan_token="tok-skip", prospect_id=None)])

    from backend.cognitive.proposal_promoter import ProposalPromoter

    review = _RaisingReviewSpy()
    telemetry = _RecordingSink()
    promoter = ProposalPromoter(
        review=review,
        ledger_path=ledger,
        telemetry=telemetry,
    )
    results = promoter.run()
    assert len(results) == 1
    assert results[0].status == "skipped"
    assert results[0].reason == "no_prospect_id"
    # review_opportunity was NOT called (would have raised).
    assert review.calls == 0
    # Telemetry recorded the skip.
    assert len(telemetry.rows) == 1
    assert telemetry.rows[0]["verdict"]["status"] == "skipped"
    # Row marked actioned -> not retried on a second run.
    rows = _read_jsonl(ledger)
    assert len(rows) == 2
    assert rows[1]["actioned"] is True
