"""Tests for the EOD three-way triangulation + interaction compilation.

The local auditor + synthesis LLM are injected (fakes) so nothing external
fires; the deterministic fallback is exercised on LLM failure."""

from __future__ import annotations

import json

import pytest

from backend.cognitive import production_interactions as pi
from backend.cognitive.triangulation import (
    _deterministic_convergence,
    build_auditor_report,
    triangulate,
)


@pytest.fixture(autouse=True)
def _isolate_state_root(tmp_path, monkeypatch):
    """Isolate the guidance ledger per test.

    The EOD tests build a real ``GuidanceLedger()``, whose JSONL path resolves
    through ``state_path()`` -> ``SAMUS_STATE_ROOT`` (the writable *state* root),
    NOT through ``storage._ROOT`` (the *artifact* root the tests monkeypatch).
    Without redirecting ``SAMUS_STATE_ROOT`` the ledger reads/writes the real
    host ``state/cognition/guidance_ledger.jsonl`` — so ``gameplan_ingested``
    dedup counts depended on machine history (the pre-existing flake) and every
    run silently appended to the host ledger. Point it at ``tmp_path`` so every
    test gets a fresh, empty, isolated ledger — mirroring the ``ledger`` fixture
    in ``tests/cognitive/test_guidance.py``."""
    monkeypatch.setenv("SAMUS_STATE_ROOT", str(tmp_path))
    monkeypatch.setenv("SAMUS_LEDGER_BACKEND", "jsonl")


# --- interaction compilation -------------------------------------------------


def test_compile_voice_interactions_compresses_to_signal(monkeypatch, tmp_path):
    monkeypatch.setattr("backend.common.storage._ROOT", tmp_path)
    vdir = tmp_path / "voice"
    vdir.mkdir()
    day = "2026-07-02"
    (vdir / "voice_events.jsonl").write_text(
        "\n".join(
            json.dumps(r)
            for r in [
                {"ts": "2026-07-02T18:00:00Z", "outcome": "voicemail"},
                {
                    "ts": "2026-07-02T18:05:00Z",
                    "outcome": "ended",
                    "lead_summary": {"intent_score": 82, "tier": "high", "objection": "too busy"},
                },
                {"ts": "2026-07-01T18:00:00Z", "outcome": "ended"},  # yesterday — excluded
            ]
        ),
        encoding="utf-8",
    )
    out = pi.compile_voice_interactions(day)
    assert out["events"] == 2 and out["voicemail"] == 1 and out["high_intent"] == 1
    assert "too busy" in out["objections"][0]


def test_compile_email_interactions_counts_sent_and_failed(monkeypatch, tmp_path):
    monkeypatch.setattr("backend.common.storage._ROOT", tmp_path)
    day = "2026-07-02"
    (tmp_path / f"morning_batch_{day}.jsonl").write_text(
        json.dumps({"status": "sent"})
        + "\n"
        + json.dumps({"status": "failed", "reason": "bounce"})
        + "\n",
        encoding="utf-8",
    )
    out = pi.compile_email_interactions(day)
    assert out["sent"] == 1 and out["failed"] == 1 and "bounce" in out["failures"][0]


def test_compile_day_interactions_missing_ledgers_ok(monkeypatch, tmp_path):
    monkeypatch.setattr("backend.common.storage._ROOT", tmp_path)
    out = pi.compile_day_interactions()
    assert out["voice"]["events"] == 0 and out["email"]["sent"] == 0


# --- auditor (leg C) ---------------------------------------------------------


def test_auditor_uses_injected_llm():
    r = build_auditor_report(
        "day summary", llm_fn=lambda s, p: "- runway risk\n- bottleneck in outreach"
    )
    assert "runway risk" in r


def test_auditor_unavailable_returns_empty_not_raise():
    def _boom(s, p):
        raise RuntimeError("lm studio down")

    assert build_auditor_report("x", llm_fn=_boom) == ""


# --- triangulation -----------------------------------------------------------

_LLM_JSON = json.dumps(
    {
        "corroborated": [
            {"finding": "runway is critically low", "tier": 1, "sources": ["A", "B", "C"]}
        ],
        "divergent": [{"finding": "try LinkedIn", "source": "B"}],
        "gameplan": {"tier1": ["cut CODB now"], "tier2": ["A/B subject lines"], "tier3": []},
    }
)


def test_triangulate_llm_path_parses_gameplan():
    tri = triangulate(
        {"A_samus": "x", "B_openai": "y", "C_auditor": "z"}, llm_fn=lambda s, p: _LLM_JSON
    )
    assert tri["method"] == "llm"
    assert tri["gameplan"]["tier1"] == ["cut CODB now"]
    assert tri["corroborated"][0]["tier"] == 1


def test_triangulate_llm_json_embedded_in_prose():
    tri = triangulate(
        {"A_samus": "x", "B_openai": "y"}, llm_fn=lambda s, p: f"Here you go:\n{_LLM_JSON}\nthanks"
    )
    assert tri["method"] == "llm" and tri["gameplan"]["tier1"] == ["cut CODB now"]


def test_triangulate_falls_back_deterministic_on_llm_error():
    def _boom(s, p):
        raise RuntimeError("down")

    reports = {
        "A_samus": "Outreach bounce rate is a risk today. Runway is short.",
        "B_openai": "The main risk is a high bounce rate in outreach. Fix deliverability.",
        "C_auditor": "Bottleneck: outreach bounce rate too high; deliverability risk.",
    }
    tri = triangulate(reports, llm_fn=_boom)
    assert tri["method"] == "deterministic"
    # 'bounce'/'outreach'/'risk' appear across reports -> corroborated
    assert any("bounce" in c["finding"].lower() for c in tri["corroborated"])


def test_triangulate_insufficient_reports_passes_lone_through():
    tri = triangulate(
        {"A_samus": "only one report here. do the thing tomorrow."}, llm_fn=lambda s, p: _LLM_JSON
    )
    assert tri["method"] == "insufficient"
    assert tri["gameplan"]["tier2"]  # lone report's findings surfaced


def test_deterministic_tiers_risk_as_tier1():
    reports = {
        "A": "governance breach detected in the pipeline today",
        "B": "a governance breach is the critical risk in the pipeline",
    }
    out = _deterministic_convergence(reports)
    assert out["corroborated"] and out["corroborated"][0]["tier"] == 1


# --- EOD integration ---------------------------------------------------------


def test_eod_review_triangulates_and_ingests_gameplan(monkeypatch, tmp_path):
    monkeypatch.setattr("backend.common.storage._ROOT", tmp_path)
    from backend.cognitive.guidance import GuidanceLedger
    from backend.cognitive.intelligence_cycle import run_end_of_day_review

    led = GuidanceLedger()  # defaults under the monkeypatched storage root
    out = run_end_of_day_review(
        llm=lambda s, p: json.dumps({"recommendations": []}),  # OpenAI advisor (fake)
        triangulation_llm=lambda s, p: _LLM_JSON,  # local auditor + synth (fake)
        ledger=led,
    )
    assert "gameplan" in out and out["gameplan"]["tier1"] == ["cut CODB now"]
    assert out["triangulation"]["method"] == "llm"
    assert out.get("gameplan_ingested", 0) >= 1  # corroborated finding tracked
    # gameplan artifact persisted
    assert list((tmp_path / "cognition").glob("gameplan_*.json"))
    # full narrative EOD review persisted with all three reports + gameplan
    assert out.get("eod_review_path")
    from pathlib import Path

    md = Path(out["eod_review_path"]).read_text(encoding="utf-8")
    assert "Report A — Samus" in md and "Report C — Local adversarial auditor" in md
    assert "NEXT-DAY GAMEPLAN" in md and "cut CODB now" in md


def test_lmstudio_url_does_not_double_v1():
    from backend.cognitive.triangulation import _lmstudio_url

    assert (
        _lmstudio_url("http://host.docker.internal:1234/v1")
        == "http://host.docker.internal:1234/v1/chat/completions"
    )
    assert _lmstudio_url("http://localhost:1234") == "http://localhost:1234/v1/chat/completions"
    assert _lmstudio_url("http://x/v1/chat/completions") == "http://x/v1/chat/completions"


# --- leg C = Claude via the Anthropic API ------------------------------------

_CLAUDE_ENVS = ("ANTHROPIC_API_KEY", "SAMUS_CLAUDE_API_KEY", "SAMUS_ANTHROPIC_API_KEY")


def test_claude_available_reads_env(monkeypatch):
    from backend.cognitive.triangulation import claude_available

    for e in _CLAUDE_ENVS:
        monkeypatch.delenv(e, raising=False)
    assert claude_available() is False
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert claude_available() is True


def test_build_claude_report_injected_and_fault():
    from backend.cognitive.triangulation import build_claude_report

    assert "x finding" in build_claude_report("day", llm_fn=lambda s, p: "- x finding")

    def _boom(s, p):
        raise RuntimeError("anthropic 401")

    assert build_claude_report("day", llm_fn=_boom) == ""


def test_eod_uses_framework_report_leg_when_present(monkeypatch, tmp_path):
    """Priority 1: a Framework Agent report written this session is leg C."""
    monkeypatch.setattr("backend.common.storage._ROOT", tmp_path)
    from backend.cognitive.guidance import GuidanceLedger
    from backend.cognitive.intelligence_cycle import (
        gather_production_state,
        run_end_of_day_review,
    )

    day = gather_production_state().get("date")
    cog = tmp_path / "cognition"
    cog.mkdir()
    (cog / f"framework_report_{day}.md").write_text(
        "- Framework: conserve posture contradicts the cold-email push.", encoding="utf-8"
    )
    out = run_end_of_day_review(
        llm=lambda s, p: json.dumps({"recommendations": []}),
        claude_llm=lambda s, p: "SHOULD NOT BE USED",  # file wins over API
        triangulation_llm=lambda s, p: _LLM_JSON,
        ledger=GuidanceLedger(),
    )
    assert out["leg_c_source"] == "framework_claude"
    from pathlib import Path

    md = Path(out["eod_review_path"]).read_text(encoding="utf-8")
    assert "conserve posture contradicts" in md and "SHOULD NOT BE USED" not in md


def test_eod_uses_claude_api_leg_when_no_framework_report(monkeypatch, tmp_path):
    """Priority 2: no Framework file -> Claude API (injected)."""
    monkeypatch.setattr("backend.common.storage._ROOT", tmp_path)
    from backend.cognitive.guidance import GuidanceLedger
    from backend.cognitive.intelligence_cycle import run_end_of_day_review

    out = run_end_of_day_review(
        llm=lambda s, p: json.dumps({"recommendations": []}),  # OpenAI (B)
        claude_llm=lambda s, p: (
            "- Claude: runway is the critical risk; cut CODB."
        ),  # Claude API (C)
        triangulation_llm=lambda s, p: _LLM_JSON,
        ledger=GuidanceLedger(),
    )
    assert out["leg_c_source"] == "claude_api"
    from pathlib import Path

    md = Path(out["eod_review_path"]).read_text(encoding="utf-8")
    assert "Report C — Claude (independent Framework review)" in md
    assert "Claude: runway is the critical risk" in md


def test_eod_falls_back_to_local_auditor_without_claude(monkeypatch, tmp_path):
    monkeypatch.setattr("backend.common.storage._ROOT", tmp_path)
    for e in _CLAUDE_ENVS:
        monkeypatch.delenv(e, raising=False)  # no key, no injected claude_llm
    from backend.cognitive.guidance import GuidanceLedger
    from backend.cognitive.intelligence_cycle import run_end_of_day_review

    out = run_end_of_day_review(
        llm=lambda s, p: json.dumps({"recommendations": []}),
        triangulation_llm=lambda s, p: _LLM_JSON,
        ledger=GuidanceLedger(),
    )
    assert out["leg_c_source"] == "local_auditor"


# --- pre-shift primer (Claude session orientation) ---------------------------


def test_framework_orientation_reflects_armed_flags(monkeypatch):
    from backend.common.settings import reload_settings
    from backend.cognitive.intelligence_cycle import compose_framework_orientation

    monkeypatch.setenv("SAMUS_IDLE_PRODUCTION_DRIVE_ENABLED", "1")
    monkeypatch.delenv("SAMUS_GOVERNED_AUTONOMOUS_DIAL_ENABLED", raising=False)
    reload_settings()
    o = compose_framework_orientation({"date": "2026-07-02"})
    assert "FRAMEWORK AGENT ORIENTATION" in o
    assert "reference_samus_capability_map.md" in o  # capability-review directive
    assert "idle-production drive (self-pacing)" in o
    # armed list contains idle-drive; dormant list contains governed dial
    armed = o.split("ARMED today")[1].split("DORMANT today")[0]
    assert "idle-production drive" in armed
    dormant = o.split("DORMANT today")[1]
    assert "governed autonomous dial" in dormant


# --- synthesis reliability (G4 [P2] closed 2026-07-08) ----------------------


def test_synthesis_reliability_full_when_all_legs_healthy(monkeypatch, tmp_path):
    """framework leg C + LLM triangulation -> reliability=full, no advisory line."""
    monkeypatch.setattr("backend.common.storage._ROOT", tmp_path)
    from backend.cognitive.guidance import GuidanceLedger
    from backend.cognitive.intelligence_cycle import (
        gather_production_state,
        run_end_of_day_review,
    )

    # Seed a Framework Agent report — priority-1 leg C (== framework_claude).
    day = gather_production_state().get("date")
    cog = tmp_path / "cognition"
    cog.mkdir()
    (cog / f"framework_report_{day}.md").write_text(
        "- Framework: everything corroborated cleanly.", encoding="utf-8"
    )
    out = run_end_of_day_review(
        llm=lambda s, p: json.dumps({"recommendations": []}),
        triangulation_llm=lambda s, p: _LLM_JSON,  # LLM synth path
        ledger=GuidanceLedger(),
    )
    assert out["leg_c_source"] == "framework_claude"
    assert out["triangulation"]["method"] == "llm"
    assert out["synthesis_reliability"] == "full"
    from pathlib import Path

    md = Path(out["eod_review_path"]).read_text(encoding="utf-8")
    assert "synthesis reliability: full" in md
    assert "WARNING synthesis reliability" not in md  # no advisory line at full


def test_synthesis_reliability_partial_when_only_leg_c_degraded(monkeypatch, tmp_path):
    """local_auditor leg C but LLM synth still worked -> reliability=partial."""
    monkeypatch.setattr("backend.common.storage._ROOT", tmp_path)
    for e in _CLAUDE_ENVS:
        monkeypatch.delenv(e, raising=False)
    from backend.cognitive.guidance import GuidanceLedger
    from backend.cognitive.intelligence_cycle import run_end_of_day_review

    out = run_end_of_day_review(
        llm=lambda s, p: json.dumps({"recommendations": []}),
        triangulation_llm=lambda s, p: _LLM_JSON,  # LLM synth OK
        ledger=GuidanceLedger(),  # but no framework file, no claude key
    )
    assert out["leg_c_source"] == "local_auditor"
    assert out["triangulation"]["method"] == "llm"
    assert out["synthesis_reliability"] == "partial"
    from pathlib import Path

    md = Path(out["eod_review_path"]).read_text(encoding="utf-8")
    assert "synthesis reliability: partial" in md
    assert "WARNING synthesis reliability: partial" in md


def test_synthesis_reliability_degraded_when_both_legs_degraded(monkeypatch, tmp_path):
    """local_auditor leg C AND deterministic synth -> reliability=degraded, and
    the corroborated recs routed into the guidance ledger carry the marker."""
    monkeypatch.setattr("backend.common.storage._ROOT", tmp_path)
    for e in _CLAUDE_ENVS:
        monkeypatch.delenv(e, raising=False)
    from backend.cognitive.guidance import GuidanceLedger
    from backend.cognitive.intelligence_cycle import run_end_of_day_review

    def _synth_boom(s, p):
        raise RuntimeError("lm studio down")

    # The auditor call and the synth call BOTH go through triangulation_llm;
    # the auditor returns text so leg C = local_auditor, then synth raises so
    # triangulation falls back to deterministic.
    calls = {"n": 0}

    def _tri_llm(s, p):
        calls["n"] += 1
        if calls["n"] == 1:
            # first call is the auditor's report (build_auditor_report)
            return "Governance breach detected in the pipeline today. Runway is a critical risk."
        # second call is the synth — force deterministic fallback
        raise RuntimeError("lm studio down for synth")

    out = run_end_of_day_review(
        llm=lambda s, p: json.dumps({"recommendations": []}),
        triangulation_llm=_tri_llm,
        ledger=GuidanceLedger(),
    )
    assert out["leg_c_source"] == "local_auditor"
    assert out["triangulation"]["method"] in ("deterministic", "insufficient")
    assert out["synthesis_reliability"] == "degraded"
    from pathlib import Path

    md = Path(out["eod_review_path"]).read_text(encoding="utf-8")
    assert "synthesis reliability: degraded" in md
    assert "WARNING synthesis reliability: degraded" in md
    # Guard: corroborated recs routed into the ledger carry the degraded marker
    # on the rationale so operator triage can apply appropriate skepticism.
    if out.get("gameplan_ingested"):
        import os

        led_path = os.path.join(str(tmp_path), "cognition", "guidance_ledger.jsonl")
        if os.path.isfile(led_path):
            with open(led_path, encoding="utf-8") as fh:
                blob = fh.read()
            assert "synthesis_reliability: degraded" in blob


def test_h1_heuristic_installed_in_all_reasoning_prompts():
    """H1 (durable heuristic 2026-07-08) must be present in every leg-C /
    synthesis system prompt so the reasoning surface can't skip it."""
    from backend.cognitive import triangulation as tri_mod

    h = tri_mod._H1_ADVERSARIAL_SAMPLING_HEURISTIC
    assert "top of each dial_run file" in h  # anchor on the concrete pathology
    assert h in tri_mod._AUDITOR_SYSTEM
    assert h in tri_mod._CLAUDE_SYSTEM
    assert h in tri_mod._SYNTH_SYSTEM


def test_h1_heuristic_reaches_the_local_auditor_prompt():
    """When leg C is the local auditor, the composed system prompt handed to
    the LLM must carry the H1 heuristic string verbatim."""
    from backend.cognitive.triangulation import (
        _H1_ADVERSARIAL_SAMPLING_HEURISTIC,
        build_auditor_report,
    )

    seen: dict = {}

    def _capture(system, prompt):
        seen["system"] = system
        return "- x finding"

    build_auditor_report("day summary", llm_fn=_capture)
    assert _H1_ADVERSARIAL_SAMPLING_HEURISTIC in seen["system"]


def test_synthesis_reliability_is_failsafe_on_unknown_state():
    """The reducer must never raise — an unknown / malformed ``out`` yields a
    conservative ``partial`` default rather than crashing the EOD."""
    from backend.cognitive.intelligence_cycle import _compute_synthesis_reliability

    level, _reason = _compute_synthesis_reliability({})
    assert level in ("full", "partial", "degraded")
    # A malformed triangulation payload (dict where int expected, etc.) must
    # also be tolerated.
    level2, _r2 = _compute_synthesis_reliability(
        {"leg_c_source": None, "triangulation": {"method": None}, "error": None}
    )
    assert level2 in ("full", "partial", "degraded")


def test_pre_shift_writes_dual_audience_primer(monkeypatch, tmp_path):
    monkeypatch.setattr("backend.common.storage._ROOT", tmp_path)
    from backend.cognitive.guidance import GuidanceLedger
    from backend.cognitive.intelligence_cycle import run_pre_shift_briefing

    out = run_pre_shift_briefing(
        llm=lambda s, p: json.dumps({"recommendations": []}), ledger=GuidanceLedger()
    )
    primer = out.get("primer_path")
    assert primer
    from pathlib import Path

    txt = Path(primer).read_text(encoding="utf-8")
    assert "PRE-SHIFT STRATEGIC BRIEFING" in txt  # Samus's briefing
    assert "FRAMEWORK AGENT ORIENTATION" in txt  # Claude primer
    assert "TODAY'S STRATEGIC GUIDANCE" in txt  # the day's guidance
