"""Tests for backend.visibility.probe — orchestration (network seam stubbed)."""

from __future__ import annotations

import json

import backend.visibility.probe as probe_mod
from backend.visibility.probe import run_probes


def _stub_query(monkeypatch, answers: dict[tuple[str, str], tuple[str | None, str]]):
    """Stub query_platform with a {(platform, question): (text, error)} map."""

    def fake(platform, question, *, workcell="visibility"):
        return answers.get((platform, question), (None, "claude_not_configured"))

    monkeypatch.setattr(probe_mod, "query_platform", fake)


def test_run_probes_aggregates(monkeypatch, tmp_path):
    q1 = "what is the best AI SDR tool"
    q2 = "best autonomous prospecting software"
    _stub_query(
        monkeypatch,
        {
            ("claude", q1): ("Hustleforge and Apollo are options. https://apollo.io", ""),
            ("claude", q2): ("Apollo is the leader.", ""),
        },
    )
    ledger = tmp_path / "probes.jsonl"
    report = run_probes(
        [q1, q2],
        ["Hustleforge", "Samus"],
        ["Apollo", "Clay"],
        ["claude"],
        ledger_path=ledger,
    )
    assert len(report.probes) == 2
    assert report.sov.sample_n == 2
    assert report.sov.citation_rate == 0.5  # 1 of 2 cited the brand
    assert report.sov.brand_mentions == 1
    assert report.sov.competitor_mentions == 2
    assert report.probes[0].brand_cited is True
    assert report.probes[0].cited_domains == ["apollo.io"]

    # Ledger written: 1 header + 2 probe lines.
    lines = [ln for ln in ledger.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == 3
    assert json.loads(lines[0])["_kind"] == "visibility_run"


def test_run_probes_unanswered_excluded(monkeypatch, tmp_path):
    q = "some question"
    _stub_query(monkeypatch, {("claude", q): (None, "claude_not_configured")})
    report = run_probes([q], ["Hustleforge"], [], ["claude"], ledger_path=tmp_path / "p.jsonl")
    assert len(report.probes) == 1
    assert report.probes[0].answered is False
    assert report.probes[0].error == "claude_not_configured"
    assert report.sov.sample_n == 0
    assert report.sov.citation_rate == 0.0


def test_run_probes_no_persist(monkeypatch, tmp_path):
    q = "q"
    _stub_query(monkeypatch, {("claude", q): ("Hustleforge wins.", "")})
    ledger = tmp_path / "nope.jsonl"
    run_probes([q], ["Hustleforge"], [], ["claude"], ledger_path=ledger, persist=False)
    assert not ledger.exists()


def test_query_platform_unconfigured_no_network(monkeypatch):
    # Stub _query_claude to simulate LLM unavailability (LM Studio is local
    # so it would otherwise succeed without a key); delete keys so
    # openai/perplexity report not_configured without network.
    for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "PERPLEXITY_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(
        probe_mod,
        "_query_claude",
        lambda question, workcell="visibility": (None, "claude_not_configured"),
    )
    for platform in ("claude", "openai", "perplexity"):
        text, error = probe_mod.query_platform(platform, "q")  # type: ignore[arg-type]
        assert text is None
        assert error.endswith("not_configured")


def test_query_platform_unknown(monkeypatch):
    text, error = probe_mod.query_platform("bing", "q")  # type: ignore[arg-type]
    assert text is None
    assert error.startswith("unknown_platform")
