"""Tests for backend.proof.generator — case studies + proof wall (no live LLM)."""
from __future__ import annotations

from backend.proof.generator import (
    build_proof_wall,
    generate_case_study,
    proof_point_from_case_study,
    to_markdown,
)
from backend.proof.models import CaseStudyInput, ProofPoint


def _input() -> CaseStudyInput:
    return CaseStudyInput(
        company="RiverCity Plumbing",
        industry="Home services",
        size="12-person shop",
        challenge="Leads from the website went unanswered for hours.",
        tried_before="a virtual receptionist that just took messages",
        solution_used="put Samus on inbound calls and same-day follow-up",
        results=["3x more booked jobs in 60 days", "first-response time cut from 4h to 2min"],
        quote="We stopped losing jobs to whoever called back first.",
        quote_author="Dana Ruiz",
        quote_title="Owner",
        url="https://hustleforge.ai/cases/rivercity",
    )


def test_generate_case_study_templated(monkeypatch):
    from backend.common.llm_client import LlmCallError
    def _llm_unavailable(**kw):
        raise LlmCallError("unavailable")
    monkeypatch.setattr("backend.common.llm_client.anthropic_messages", _llm_unavailable)
    cs = generate_case_study(_input(), use_llm=True)
    assert cs.used_llm is False
    assert "RiverCity Plumbing" in cs.title
    assert "3x more booked jobs in 60 days" in cs.title  # leads with first result
    assert cs.narrative  # templated narrative non-empty
    assert cs.schema_jsonld["@type"] == "Article"
    assert cs.schema_jsonld["headline"]


def test_case_study_markdown_sections():
    cs = generate_case_study(_input(), use_llm=False)
    md = cs.markdown
    assert md.startswith("# How RiverCity Plumbing")
    assert "## Challenge" in md
    assert "## What they tried before" in md
    assert "## How they used Hustleforge" in md
    assert "## Results" in md
    assert "Dana Ruiz" in md
    assert "- 3x more booked jobs in 60 days" in md


def test_case_study_minimal_input_safe():
    cs = generate_case_study(CaseStudyInput(company="Acme"), use_llm=False)
    assert cs.company == "Acme"
    assert isinstance(cs.markdown, str) and cs.markdown
    assert cs.schema_jsonld["@type"] == "Article"


def test_to_markdown_without_quote():
    cs = generate_case_study(
        CaseStudyInput(company="Acme", results=["2x revenue"]), use_llm=False
    )
    assert "“" not in to_markdown(cs)  # no quote block when no quote


def test_build_proof_wall_dedups_industries():
    points = [
        ProofPoint(company="A", result="2x leads", industry="SaaS"),
        ProofPoint(company="B", result="3x demos", industry="SaaS"),
        ProofPoint(company="C", result="50% less churn", industry="Agency"),
        ProofPoint(company="D", result="win", industry=""),
    ]
    wall = build_proof_wall(points)
    assert wall.count == 4
    assert wall.industries == ["SaaS", "Agency"]  # deduped, in order, blanks dropped


def test_proof_point_from_case_study():
    cs = generate_case_study(_input(), use_llm=False)
    pp = proof_point_from_case_study(cs)
    assert pp.company == "RiverCity Plumbing"
    assert pp.result == "3x more booked jobs in 60 days"
    assert pp.industry == "Home services"
