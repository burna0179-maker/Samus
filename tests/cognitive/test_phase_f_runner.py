"""Phase F — the FIRST LIVE CALLER of the cognitive stack, built DORMANT.

Covers the build-plan Phase-F test bullets:

  (a) **DORMANCY (flag OFF).** With ``cognitive_loop_enabled`` False the live
      caller is a pure no-op/503: the gateway route returns ``{enabled: false}``
      / HTTP 503 and constructs NOTHING — asserted by spying the runner's
      assembly (``build_cognition_runtime``) AND the loop/engine classes with
      RAISING spies and proving none are touched; no LLM / CRM / finance backend
      is reached. ``runner.loop_enabled()`` resolves False by default and there
      is NO import-time construction.

  (b) **ARMED (flag ON, STUB deps).** With the master switch on and STUB
      llm/EFH/domain providers injected, ``run_one_cycle`` executes the stack
      end-to-end, returns a ``CycleResultSummary``, writes EXACTLY ONE
      propose-only proposal, and invokes ZERO effectors — asserted via the
      Phase-E spy pattern (raising spies on the cash_engine gate /
      review_opportunity / ``_outreach_stage``, CRM + finance mutators, and the
      signed-post network fn; none may be called).

Isolation: every armed test injects STUB providers (no real CRM / DDB / finance /
LM Studio / EFH-axiom backend is touched) and points ``state_path`` at a temp
root. The dormant tests assert the heavy stack is never even imported/constructed.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from backend.cognitive.cycle_models import CycleInput


# ---------------------------------------------------------------------------
# flag helpers — arm / disarm the Phase-F master switch + temp state root
# ---------------------------------------------------------------------------
def _arm_loop(monkeypatch, state_root=None):
    """Arm the master switch ``cognitive_loop_enabled`` (and optional state root)."""
    if state_root is not None:
        monkeypatch.setenv("SAMUS_STATE_ROOT", str(state_root))
    monkeypatch.setenv("SAMUS_COGNITIVE_LOOP_ENABLED", "true")
    from backend.common.settings import reload_settings

    reload_settings()


def _disarm_loop(monkeypatch):
    monkeypatch.setenv("SAMUS_COGNITIVE_LOOP_ENABLED", "false")
    from backend.common.settings import reload_settings

    reload_settings()


def _arm_act(monkeypatch):
    """Arm the propose-only ACT flag so the armed-cycle test exercises ACT writing."""
    monkeypatch.setenv("SAMUS_COGNITIVE_ACT_PROPOSALS_ENABLED", "true")
    from backend.common.settings import reload_settings

    reload_settings()


# ---------------------------------------------------------------------------
# STUB collaborators (NO real LM Studio / EFH / CRM / finance backend)
# ---------------------------------------------------------------------------
class _StubReasoner:
    """A stub REASON collaborator — returns a canned reply, no LLM call."""

    def __init__(self):
        self.calls = 0

    def generate(self, *, system="", user_text="", channel="", plan_token="", metadata=None):
        self.calls += 1
        return f"[stub-reasoned] {user_text}".strip()


class _StubClassifierClears:
    """A stub EFH-shaped CLASSIFY gate that always CLEARS (returns None)."""

    def __init__(self):
        self.calls = 0

    def evaluate(self, proposed_action):
        self.calls += 1
        return None  # no veto


class _StubDomainProvider:
    """Healthy stub domain provider surfacing every perception slice."""

    def __init__(self):
        self.calls = []

    def follow_ups_due(self):
        self.calls.append("follow_ups_due")
        return {"count": 3, "ddb_error": None}

    def opportunities_pending_stake(self):
        self.calls.append("opportunities_pending_stake")
        return {"count": 2}

    def open_operator_tasks(self):
        self.calls.append("open_operator_tasks")
        return {"count": 1, "ddb_error": None}

    def decay_risk(self, *, threshold=0.6):
        self.calls.append(("decay_risk", threshold))
        return {"assessed": 5, "crossing": 1, "worst_risk": 0.82, "threshold": threshold}

    def finance_runway(self):
        self.calls.append("finance_runway")
        return {"days_of_runway": 41.0, "alert_triggered": False, "available_balance_usd": 1200.0}

    def finance_actions(self):
        self.calls.append("finance_actions")
        return {"open_total": 4, "overdue_count": 1, "due_today_count": 0}

    def cash_distress(self):
        self.calls.append("cash_distress")
        return {"cash_distress": "ok", "distress_reasons": []}


class _RecordingSink:
    """A spy proposal sink — captures appended rows instead of writing disk."""

    def __init__(self):
        self.rows = []

    def append(self, record):
        self.rows.append(record)

    @property
    def last(self):
        return self.rows[-1] if self.rows else None


def _run(coro):
    return asyncio.run(coro)


# ===========================================================================
# (a) DORMANCY — master switch OFF ⇒ pure no-op, constructs NOTHING
# ===========================================================================
def test_loop_enabled_defaults_false(monkeypatch):
    # The master switch resolves False by default (wire-not-arm).
    _disarm_loop(monkeypatch)
    from backend.cognitive import runner

    assert runner.loop_enabled() is False


def test_importing_runner_constructs_nothing():
    # Importing the composition root must build no loop / engine / provider and
    # touch no backend (dormancy: no import-time construction). The import here
    # is a no-op if it already happened; the assertion is that the module
    # exposes the API without having side-constructed anything. We additionally
    # assert the heavy deps are NOT imported as a side effect of importing runner
    # alone (they are lazy-imported inside the functions).
    import importlib
    import sys

    # Drop any cached heavy modules so we can detect a side-effect import.
    for mod in (
        "backend.cognitive.cognitive_loop",
        "backend.cognitive.meta_cognition_engine",
        "backend.cognitive.domain_perception",
    ):
        sys.modules.pop(mod, None)

    importlib.reload(importlib.import_module("backend.cognitive.runner"))

    # The runner module imported cleanly but did NOT drag in the heavy stack.
    assert "backend.cognitive.cognitive_loop" not in sys.modules
    assert "backend.cognitive.meta_cognition_engine" not in sys.modules
    assert "backend.cognitive.domain_perception" not in sys.modules


def test_route_flag_off_is_503_noop_constructs_nothing(monkeypatch):
    """Route with the master switch OFF returns 503 {enabled:false} and builds
    NOTHING — proven by raising spies on the runner assembly + loop class."""
    monkeypatch.setenv("SAMUS_SHARED_HMAC_KEY", "test-hmac-key")
    _disarm_loop(monkeypatch)

    # Spy the assembly + the loop/engine: if the disabled route constructs the
    # stack, these raise and fail the test loudly.
    import backend.cognitive.runner as runner_mod

    def _boom_build(*_a, **_k):
        raise AssertionError("build_cognition_runtime must NOT be called when disabled")

    async def _boom_run(*_a, **_k):
        raise AssertionError("run_one_cycle must NOT be called when disabled")

    monkeypatch.setattr(runner_mod, "build_cognition_runtime", _boom_build)
    monkeypatch.setattr(runner_mod, "run_one_cycle", _boom_run)

    from fastapi.testclient import TestClient

    from backend.gateway.app import create_app

    client = TestClient(create_app(), raise_server_exceptions=True)
    resp = client.post("/api/samus/cognition/cycle", json={"user_text": "hi"})

    assert resp.status_code == 503
    detail = resp.json().get("detail", {})
    assert detail.get("enabled") is False
    assert detail.get("reason") == "cognitive_loop_disabled"


def test_run_one_cycle_not_reachable_without_flag_is_callers_job(monkeypatch):
    # The runner itself does not self-gate (the route owns the master switch);
    # but loop_enabled() is the authority and is False by default, so a correct
    # caller never reaches construction. This asserts the gate value the route
    # relies on.
    _disarm_loop(monkeypatch)
    from backend.cognitive import runner

    assert runner.loop_enabled() is False


# ===========================================================================
# (b) ARMED — master switch ON + STUB deps ⇒ end-to-end, one proposal, no effector
# ===========================================================================
def test_run_one_cycle_armed_executes_end_to_end_with_stubs(tmp_path, monkeypatch):
    _arm_loop(monkeypatch, tmp_path / "state")
    _arm_act(monkeypatch)

    from backend.cognitive.runner import run_one_cycle

    reasoner = _StubReasoner()
    sink = _RecordingSink()
    summary = _run(
        run_one_cycle(
            CycleInput(user_text="what next?", plan_token="tok-f", channel="chat"),
            domain=_StubDomainProvider(),
            reasoner=reasoner,
            classifier=_StubClassifierClears(),
            proposal_sink=sink,
        )
    )

    # The summary is the route-friendly CycleResultSummary, end-to-end OK.
    from backend.cognitive.runner import CycleResultSummary

    assert isinstance(summary, CycleResultSummary)
    assert summary.enabled is True
    assert summary.ok is True
    assert summary.compliance_blocked is False
    assert summary.plan_token == "tok-f"
    # Reply came from the STUB reasoner (REASON ran; no LLM backend touched).
    # With a domain provider wired, REASON now GROUNDS the prompt in the live
    # business state and PREPENDS it to the caller's user_text — the stub echoes
    # the assembled prompt back, so the reply carries the context + the user_text.
    assert summary.reply.startswith("[stub-reasoned] Current business state")
    assert "what next?" in summary.reply
    assert reasoner.calls == 1
    # Full stage list present (PERCEIVE..EMIT).
    assert summary.stages == [
        "PERCEIVE", "CLASSIFY", "REASON", "DECIDE", "ACT", "REFLECT", "PERSIST", "EMIT",
    ]
    # Exactly one propose-only proposal written + surfaced in the summary.
    assert summary.proposal_written is True
    assert len(sink.rows) == 1
    assert summary.proposal is sink.rows[0]
    assert summary.proposal["status"] == "proposed"
    assert summary.proposal["actioned"] is False
    assert "PROPOSAL ONLY" in summary.proposal["note"]
    # Intent derived from perception (2 pending-stake deals -> stake_then_outreach).
    assert summary.proposal["intent"]["type"] == "stake_then_outreach"


def test_run_one_cycle_armed_invokes_no_effector_or_gate(tmp_path, monkeypatch):
    """The load-bearing safety property (Phase-E spy pattern): the armed live
    caller records a proposal and calls NONE of the effector / gate / send /
    finance entrypoints, and no network."""
    _arm_loop(monkeypatch, tmp_path / "state")
    _arm_act(monkeypatch)

    import backend.cash_engine.gate as gate_mod
    import backend.cash_engine.service as cash_service_mod
    import backend.cash_engine.stages as stages_mod
    import backend.crm.service as crm_mod
    import backend.finance.service as finance_mod
    import backend.common.http_client as http_mod

    called: list[str] = []

    def _spy(name):
        def _f(*_a, **_k):
            called.append(name)
            raise AssertionError(f"effector {name} must NOT be called by the cognitive loop")
        return _f

    # cash_engine front-door gate + review_opportunity + live-send double-gate.
    monkeypatch.setattr(gate_mod, "evaluate_gate", _spy("cash_engine.evaluate_gate"))
    monkeypatch.setattr(cash_service_mod, "review_opportunity", _spy("cash_engine.review_opportunity"))
    monkeypatch.setattr(stages_mod, "_outreach_stage", _spy("cash_engine._outreach_stage"))
    # CRM mutating entrypoints.
    monkeypatch.setattr(crm_mod, "advance_opportunity", _spy("crm.advance_opportunity"))
    monkeypatch.setattr(crm_mod, "create_opportunity", _spy("crm.create_opportunity"))
    # finance mutation (webhook handler).
    monkeypatch.setattr(finance_mod, "handle_stripe_webhook", _spy("finance.handle_stripe_webhook"))
    # any signed network call.
    monkeypatch.setattr(http_mod, "signed_post_json_sync", _spy("http.signed_post_json_sync"))

    from backend.cognitive.runner import run_one_cycle

    sink = _RecordingSink()
    summary = _run(
        run_one_cycle(
            CycleInput(user_text="should I act?", plan_token="t"),
            domain=_StubDomainProvider(),
            reasoner=_StubReasoner(),
            classifier=_StubClassifierClears(),
            proposal_sink=sink,
        )
    )

    assert summary.ok is True
    assert called == [], f"the cognitive loop illegally invoked effector(s): {called}"
    # A proposal was recorded but nothing was actuated.
    assert len(sink.rows) == 1
    assert sink.rows[0]["actioned"] is False
    assert summary.proposal_written is True


def test_run_one_cycle_armed_writes_one_proposal_to_state_path(tmp_path, monkeypatch):
    # With no injected sink, the capturing tee persists exactly one proposal row
    # to state_path('cognition','proposals.jsonl') AND surfaces it in the summary.
    # NOTE: Phase G's promotion seam (default-ON) would append a flipped marker
    # to the same ledger after a successful cycle — that is the documented Phase-G
    # interaction with the propose-only ledger. This test is specifically about
    # Phase E's "exactly one PROPOSAL write per cycle" guarantee, so we disarm
    # Phase G here to isolate the Phase-E ACT semantics.
    _arm_loop(monkeypatch, tmp_path / "state")
    _arm_act(monkeypatch)
    monkeypatch.setenv("SAMUS_COGNITION_PROPOSAL_PROMOTION_ENABLED", "false")
    from backend.common.settings import reload_settings

    reload_settings()

    from backend.cognitive.runner import run_one_cycle

    summary = _run(
        run_one_cycle(
            CycleInput(user_text="status?", plan_token="tok-disk", channel="chat"),
            domain=_StubDomainProvider(),
            reasoner=_StubReasoner(),
            classifier=_StubClassifierClears(),
            # no proposal_sink -> default capturing tee onto the durable ledger
        )
    )
    assert summary.ok is True
    proposals = tmp_path / "state" / "cognition" / "proposals.jsonl"
    assert proposals.exists(), "ACT must append to state_path('cognition','proposals.jsonl')"
    lines = [ln for ln in proposals.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == 1, "exactly one proposal row per cycle"
    rec = json.loads(lines[0])
    assert rec["plan_token"] == "tok-disk"
    assert rec["actioned"] is False
    # The summary's proposal matches what landed on disk.
    assert summary.proposal_written is True
    assert summary.proposal["plan_token"] == "tok-disk"


def test_run_one_cycle_armed_but_act_disabled_writes_no_proposal(tmp_path, monkeypatch):
    # Master switch ON but the ACT sub-flag OFF: the loop runs end-to-end, but
    # ACT is a pure no-op (writes nothing) — proving sub-behaviours stay
    # independently gated even when the loop is armed.
    _arm_loop(monkeypatch, tmp_path / "state")
    monkeypatch.setenv("SAMUS_COGNITIVE_ACT_PROPOSALS_ENABLED", "false")
    from backend.common.settings import reload_settings

    reload_settings()

    from backend.cognitive.runner import run_one_cycle

    sink = _RecordingSink()
    summary = _run(
        run_one_cycle(
            CycleInput(user_text="hi", plan_token="t"),
            domain=_StubDomainProvider(),
            reasoner=_StubReasoner(),
            classifier=_StubClassifierClears(),
            proposal_sink=sink,
        )
    )
    assert summary.ok is True
    assert summary.act_proposals_enabled is False
    # ACT wrote nothing (independent sub-gate held).
    assert sink.rows == []
    assert summary.proposal_written is False
    assert not (tmp_path / "state" / "cognition" / "proposals.jsonl").exists()


def test_route_flag_on_runs_one_cycle(tmp_path, monkeypatch):
    """End-to-end through the gateway route with the master switch ON.

    The route builds the live runtime; we monkeypatch the runner's assembly to
    inject STUB providers so no real LM Studio / CRM / finance / EFH backend is
    touched, then assert the route returns the cycle summary with one proposal."""
    monkeypatch.setenv("SAMUS_SHARED_HMAC_KEY", "test-hmac-key")
    _arm_loop(monkeypatch, tmp_path / "state")
    _arm_act(monkeypatch)

    import backend.cognitive.runner as runner_mod

    # Force the route's run_one_cycle to use STUB deps (the route calls it with
    # no build_kwargs, so we wrap it to inject stubs).
    real_run = runner_mod.run_one_cycle
    sink = _RecordingSink()

    async def _stub_run(inp, **_kwargs):
        return await real_run(
            inp,
            domain=_StubDomainProvider(),
            reasoner=_StubReasoner(),
            classifier=_StubClassifierClears(),
            proposal_sink=sink,
        )

    # Patch the symbol the route imported (it does `from ...runner import run_one_cycle`
    # inside the handler, so patch on the module it imports from).
    monkeypatch.setattr(runner_mod, "run_one_cycle", _stub_run)

    from fastapi.testclient import TestClient

    from backend.gateway.app import create_app

    client = TestClient(create_app(), raise_server_exceptions=True)
    resp = client.post(
        "/api/samus/cognition/cycle",
        json={"user_text": "what next?", "plan_token": "route-tok", "channel": "chat"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is True
    assert body["ok"] is True
    assert body["plan_token"] == "route-tok"
    # Grounded REASON: the stub echoes the perception-context-prepended prompt.
    assert body["reply"].startswith("[stub-reasoned] Current business state")
    assert "what next?" in body["reply"]
    assert body["stages"][0] == "PERCEIVE" and body["stages"][-1] == "EMIT"
    assert body["proposal_written"] is True
    assert body["proposal"]["status"] == "proposed"
    assert body["proposal"]["actioned"] is False


# ---------------------------------------------------------------------------
# Concept 1 wiring — runner's system_prefix appends precedent context alongside
# guidance context when a live belief precedent surfaces.
# ---------------------------------------------------------------------------


def test_build_cognition_runtime_appends_precedent_to_system_prefix(
    tmp_path, monkeypatch,
):
    """When a high-confidence belief matches the runner's precedent context,
    ``build_cognition_runtime`` composes it into the ``system_prefix`` passed
    to ``build_llm_reasoner`` — alongside strategic-intelligence directive and
    accepted guidance. The reasoner is the seam we capture via a spy.
    """
    from backend.cognitive import belief_ledger as bl
    from backend.cognitive import runner as runner_mod
    from backend.cognitive import cognitive_loop as cl

    _arm_loop(monkeypatch, state_root=tmp_path)

    def _ev(source, weight=1.0):
        return {"source": source, "detail": "d", "weight": weight, "ts": ""}

    # Seed a matching belief on the runner's precedent context.
    bl.record_belief(
        "cognition loop reasoning benefits from precedent injection",
        belief_id="cog_pref_A",
        supporting=[_ev("a"), _ev("b"), _ev("c")],
        situation_key=bl.situation_key_for("cognition loop reasoning"),
    )

    captured: dict = {}

    def _spy_build_llm_reasoner(*, system_prefix: str = ""):
        captured["system_prefix"] = system_prefix
        return _StubReasoner()

    monkeypatch.setattr(cl, "build_llm_reasoner", _spy_build_llm_reasoner)

    rt = runner_mod.build_cognition_runtime(domain=_StubDomainProvider(),
                                            classifier=_StubClassifierClears(),
                                            enabled=True)
    assert rt is not None

    prefix = captured.get("system_prefix", "")
    assert "Precedent" in prefix  # active_precedent_context header
    assert "cognition loop reasoning benefits" in prefix
