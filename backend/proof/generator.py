"""Case-study generation + social-proof aggregation.

``generate_case_study`` assembles the structured Challenge -> Tried -> Used ->
Result -> Quote format (the shape that drives closes and that AI engines cite),
renders it to markdown, and attaches Article JSON-LD via
:mod:`backend.seo.schema_builder`. An optional budget-gated LLM call polishes
the connective narrative; it always degrades to a deterministic assembly.
"""
from __future__ import annotations

import logging
import os

from backend.proof.models import CaseStudy, CaseStudyInput, ProofPoint, ProofWall
from backend.seo import schema_builder

_LOG = logging.getLogger("samus.proof.generator")

_BUDGET_WORKCELL = "proof"
_MAX_TOKENS = 600


def generate_case_study(
    data: CaseStudyInput, *, use_llm: bool = True, publisher: str = "Hustleforge"
) -> CaseStudy:
    """Build a publishable case study from structured outcome data."""
    title = _title(data)
    narrative = ""
    used_llm = False
    if use_llm:
        narrative = _narrative_via_llm(data)
        used_llm = bool(narrative)
    if not narrative:
        narrative = _narrative_templated(data)

    cs = CaseStudy(
        title=title,
        company=data.company,
        industry=data.industry,
        challenge=data.challenge,
        tried_before=data.tried_before,
        solution_used=data.solution_used,
        results=list(data.results),
        quote=data.quote,
        quote_author=data.quote_author,
        quote_title=data.quote_title,
        narrative=narrative,
        used_llm=used_llm,
    )
    cs.markdown = to_markdown(cs)
    cs.schema_jsonld = schema_builder.article(
        headline=title,
        description=_first_result(data) or data.challenge,
        url=data.url,
        author=publisher,
        publisher=publisher,
    )
    return cs


def _title(data: CaseStudyInput) -> str:
    first = _first_result(data)
    if first:
        return f"How {data.company} achieved {first}"
    return f"How {data.company} got results with {data.solution_used or 'Hustleforge'}"


def _first_result(data: CaseStudyInput) -> str:
    return data.results[0].strip() if data.results else ""


def _narrative_templated(data: CaseStudyInput) -> str:
    parts = []
    if data.challenge:
        parts.append(
            f"{data.company}{f', a {data.size},' if data.size else ''} faced a clear problem: {data.challenge}"
        )
    if data.tried_before:
        parts.append(f"Before Hustleforge, they tried {data.tried_before} — without the result they needed.")
    if data.solution_used:
        parts.append(f"With Hustleforge, they {data.solution_used}.")
    if data.results:
        parts.append("The outcome: " + "; ".join(data.results) + ".")
    return " ".join(parts).strip()


def _narrative_via_llm(data: CaseStudyInput) -> str:
    """Polish the structured fields into tight case-study prose. Fail-closed."""
    try:
        from backend.common.llm_client import anthropic_messages

        prompt = (
            "Write a tight 3-4 sentence case-study narrative from these facts. "
            "Lead with the result. Be concrete, no hype, no invented numbers.\n\n"
            f"Company: {data.company} ({data.size})\n"
            f"Industry: {data.industry}\n"
            f"Challenge: {data.challenge}\n"
            f"Tried before: {data.tried_before}\n"
            f"Used: {data.solution_used}\n"
            f"Results: {'; '.join(data.results)}"
        )
        text, _usage = anthropic_messages(
            workcell=_BUDGET_WORKCELL, api_key="unused", prompt=prompt, max_tokens=_MAX_TOKENS
        )
        return text.strip()
    except Exception as exc:  # noqa: BLE001 — fail-closed to template
        _LOG.info("case-study llm unavailable (%s), using template", type(exc).__name__)
        return ""


def to_markdown(cs: CaseStudy) -> str:
    parts = [f"# {cs.title}"]
    if cs.industry:
        parts.append(f"*{cs.company} · {cs.industry}*")
    if cs.narrative:
        parts.append(cs.narrative)
    if cs.challenge:
        parts.append(f"## Challenge\n\n{cs.challenge}")
    if cs.tried_before:
        parts.append(f"## What they tried before\n\n{cs.tried_before}")
    if cs.solution_used:
        parts.append(f"## How they used Hustleforge\n\n{cs.solution_used}")
    if cs.results:
        parts.append("## Results\n\n" + "\n".join(f"- {r}" for r in cs.results))
    if cs.quote:
        attribution = " — ".join(p for p in (cs.quote_author, cs.quote_title) if p)
        parts.append(f"> “{cs.quote}”" + (f"\n>\n> — {attribution}" if attribution else ""))
    return "\n\n".join(parts).strip()


# ---------------------------------------------------------------------------
# Social-proof aggregation (wall of love)
# ---------------------------------------------------------------------------


def build_proof_wall(points: list[ProofPoint]) -> ProofWall:
    """Aggregate proof points into a wall, de-duping industries in order."""
    industries: list[str] = []
    for p in points:
        ind = (p.industry or "").strip()
        if ind and ind not in industries:
            industries.append(ind)
    return ProofWall(points=list(points), count=len(points), industries=industries)


def proof_point_from_case_study(cs: CaseStudy) -> ProofPoint:
    """Distil a case study into a single proof point for the wall feed."""
    return ProofPoint(
        company=cs.company,
        result=cs.results[0] if cs.results else cs.title,
        quote=cs.quote,
        author=cs.quote_author,
        industry=cs.industry,
    )


# ---------------------------------------------------------------------------
# HTTP-adapter handlers (dict in, dict out)
# ---------------------------------------------------------------------------


def handle_generate_case_study(payload: dict) -> dict:
    """Build a case study from payload. ``use_llm`` defaults to False so the
    wired action incurs no LLM spend unless the caller opts in."""
    data = CaseStudyInput(
        company=str(payload.get("company") or ""),
        industry=str(payload.get("industry") or ""),
        size=str(payload.get("size") or ""),
        challenge=str(payload.get("challenge") or ""),
        tried_before=str(payload.get("tried_before") or ""),
        solution_used=str(payload.get("solution_used") or ""),
        results=[str(r) for r in (payload.get("results") or [])],
        quote=str(payload.get("quote") or ""),
        quote_author=str(payload.get("quote_author") or ""),
        quote_title=str(payload.get("quote_title") or ""),
        url=str(payload.get("url") or ""),
    )
    cs = generate_case_study(data, use_llm=bool(payload.get("use_llm", False)))
    return {
        "title": cs.title,
        "company": cs.company,
        "narrative": cs.narrative,
        "results": cs.results,
        "markdown": cs.markdown,
        "schema_jsonld": cs.schema_jsonld,
        "used_llm": cs.used_llm,
    }


def handle_build_proof_wall(payload: dict) -> dict:
    points = [
        ProofPoint(
            company=str(p.get("company") or ""),
            result=str(p.get("result") or ""),
            quote=str(p.get("quote") or ""),
            author=str(p.get("author") or ""),
            industry=str(p.get("industry") or ""),
        )
        for p in (payload.get("points") or [])
        if isinstance(p, dict)
    ]
    wall = build_proof_wall(points)
    return {
        "count": wall.count,
        "industries": wall.industries,
        "points": [
            {"company": p.company, "result": p.result, "quote": p.quote, "author": p.author, "industry": p.industry}
            for p in wall.points
        ],
    }
