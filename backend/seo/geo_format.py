"""GEO (Generative Engine Optimization) content formatting.

Restructures content so AI answer engines can extract and cite it: an
answer-first "golden answer" block (40-60 words, self-contained), short
paragraphs, and a FAQ section (the format AIO pipelines surface). 44% of LLM
citations come from the first 30% of a page, so the answer must lead.

LLM-assisted where it helps (FAQ generation) but **always** degrades to a
deterministic template — like the rest of the SEO workcell, a GEO enrichment
never raises and never blocks on the LLM. Pairs with
:mod:`backend.seo.schema_builder` to emit the matching FAQPage/Article JSON-LD.
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field

_LOG = logging.getLogger("samus.seo.geo_format")

GOLDEN_MIN_WORDS = 40
GOLDEN_MAX_WORDS = 60
FAQ_MIN = 5
FAQ_MAX = 10
_BUDGET_WORKCELL = "seo"
_MAX_TOKENS = 900


@dataclass
class GeoContent:
    """The GEO-formatted view of a content draft."""

    golden_answer: str
    faq: list[dict[str, str]] = field(default_factory=list)  # [{"q":, "a":}]
    geo_markdown: str = ""
    used_llm: bool = False


# ---------------------------------------------------------------------------
# Golden answer (deterministic)
# ---------------------------------------------------------------------------

_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")


def _words(text: str) -> list[str]:
    return [w for w in re.split(r"\s+", text.strip()) if w]


def golden_answer_from(question: str, source_text: str) -> str:
    """Build a 40-60 word, self-contained answer block from ``source_text``.

    Accumulates whole sentences until the word budget is reached, so the block
    reads naturally and can be lifted as a standalone citation. Falls back to a
    word-truncation if the source has no sentence boundaries.
    """
    text = " ".join((source_text or "").split())
    if not text:
        return ""
    sentences = _SENTENCE_RE.split(text)
    out: list[str] = []
    count = 0
    for sent in sentences:
        n = len(_words(sent))
        if count and count + n > GOLDEN_MAX_WORDS:
            break
        out.append(sent.strip())
        count += n
        if count >= GOLDEN_MIN_WORDS:
            break
    answer = " ".join(out).strip()
    words = _words(answer)
    if len(words) > GOLDEN_MAX_WORDS:
        answer = " ".join(words[:GOLDEN_MAX_WORDS]).rstrip(",;:") + "."
    return answer


# ---------------------------------------------------------------------------
# FAQ generation (LLM-assisted, fail-closed to templates)
# ---------------------------------------------------------------------------


def build_faq_templated(keywords: list[str], context_lines: list[str]) -> list[dict[str, str]]:
    """Deterministic 5-question FAQ derived from keywords + context. Zero cost."""
    primary = (keywords[0] if keywords else "this service").strip()
    secondary = keywords[1].strip() if len(keywords) > 1 else primary
    ctx = " ".join(context_lines).strip() or f"We help you with {primary}."
    faq = [
        {
            "q": f"What is {primary}?",
            "a": f"{primary.capitalize()} is the service this page covers. {ctx[:200]}".strip(),
        },
        {
            "q": f"How much does {primary} cost?",
            "a": "Pricing depends on scope. We give a transparent quote up front with no hidden fees, so you know the cost before any work begins.",
        },
        {
            "q": f"How long does {primary} take?",
            "a": "Timelines vary by project size. Most engagements start within a few days of agreement, and we share a clear schedule before we begin.",
        },
        {
            "q": f"How is {primary} different from {secondary}?",
            "a": f"{primary.capitalize()} and {secondary} solve related but distinct problems. We scope the right fit for your situation rather than upselling.",
        },
        {
            "q": "How do I get started?",
            "a": "Request a free quote or assessment. We review your needs, outline the plan, and you decide whether to proceed — no obligation.",
        },
    ]
    return faq


def build_faq_via_llm(
    topic: str, keywords: list[str], context: str, *, workcell: str = _BUDGET_WORKCELL
) -> list[dict[str, str]]:
    """Generate a 5-10 entry FAQ via one budget-gated LLM call. Returns ``[]``
    on any failure (no key, budget, transport, parse). Never raises."""
    try:
        from backend.common.llm_client import anthropic_messages

        prompt = (
            "Write a customer FAQ for the page below. Return ONLY a JSON array of "
            f"{FAQ_MIN}-{FAQ_MAX} objects, each {{\"q\": question, \"a\": answer}}. "
            "Each answer is a self-contained 40-60 word direct response (no fluff, "
            "answer-first).\n\n"
            f"TOPIC: {topic}\nKEYWORDS: {', '.join(keywords)}\nCONTEXT: {context[:800]}"
        )
        text, _usage = anthropic_messages(
            workcell=workcell, api_key="unused", prompt=prompt, max_tokens=_MAX_TOKENS
        )
        parsed = _tolerant_json_array(text)
        out: list[dict[str, str]] = []
        for item in parsed or []:
            if isinstance(item, dict):
                q = str(item.get("q") or "").strip()
                a = str(item.get("a") or "").strip()
                if q and a:
                    out.append({"q": q, "a": a})
        return out[:FAQ_MAX]
    except Exception as exc:  # noqa: BLE001 — fail-closed to templates
        _LOG.info("geo faq llm unavailable (%s), using template", type(exc).__name__)
        return []


_JSON_ARRAY_RE = re.compile(r"```(?:json)?\s*(\[.*?\])\s*```", re.DOTALL)


def _tolerant_json_array(text: str) -> list | None:
    text = (text or "").strip()
    if not text:
        return None
    fenced = _JSON_ARRAY_RE.search(text)
    if fenced:
        text = fenced.group(1)
    else:
        start, end = text.find("["), text.rfind("]")
        if start != -1 and end > start:
            text = text[start : end + 1]
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, list) else None
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Composition
# ---------------------------------------------------------------------------


def geo_enrich(
    drafts: dict[str, str],
    keywords: list[str],
    *,
    use_llm: bool = True,
    workcell: str = _BUDGET_WORKCELL,
) -> GeoContent:
    """Produce the GEO-formatted view of a set of content drafts.

    ``drafts`` is the SEO workcell's draft dict (title/meta_description/h1/
    body_intro/body_main/cta). Returns a :class:`GeoContent` with the golden
    answer, a FAQ (LLM or template), and an answer-first markdown rendering.
    """
    h1 = (drafts.get("h1") or drafts.get("title") or (keywords[0] if keywords else "")).strip()
    question = h1 if h1.endswith("?") else (f"What is {h1}?" if h1 else "")
    body = " ".join(
        s for s in (drafts.get("body_intro", ""), drafts.get("body_main", "")) if s
    ).strip()
    summary_source = body or drafts.get("meta_description", "")

    golden = golden_answer_from(question, summary_source)

    faq: list[dict[str, str]] = []
    used_llm = False
    if use_llm:
        faq = build_faq_via_llm(h1, keywords, summary_source, workcell=workcell)
        used_llm = bool(faq)
    if not faq:
        faq = build_faq_templated(keywords, [drafts.get("body_intro", "")])

    markdown = render_answer_first(h1=h1, golden=golden, drafts=drafts, faq=faq)
    return GeoContent(golden_answer=golden, faq=faq, geo_markdown=markdown, used_llm=used_llm)


def render_answer_first(
    *, h1: str, golden: str, drafts: dict[str, str], faq: list[dict[str, str]]
) -> str:
    """Render the GEO-optimized markdown: H1 question, golden-answer block,
    body, then an FAQ section. Answer leads; supporting context follows."""
    parts: list[str] = []
    if h1:
        parts.append(f"# {h1}")
    if golden:
        # The golden answer leads, bolded as the citable claim.
        parts.append(f"**{golden}**")
    body_main = drafts.get("body_main", "").strip()
    if body_main:
        parts.append(body_main)
    cta = drafts.get("cta", "").strip()
    if cta:
        parts.append(cta)
    if faq:
        parts.append("## Frequently Asked Questions")
        for item in faq:
            parts.append(f"### {item['q']}")
            parts.append(item["a"])
    return "\n\n".join(parts).strip()


# ---------------------------------------------------------------------------
# HTTP-adapter handler (dict in, dict out)
# ---------------------------------------------------------------------------


def handle_geo_format(payload: dict) -> dict:
    """GEO-format a set of drafts + emit the matching FAQPage JSON-LD.

    ``use_llm`` defaults to False so the wired action incurs no LLM spend
    unless the caller explicitly opts in."""
    from backend.seo import schema_builder

    drafts = payload.get("drafts") if isinstance(payload.get("drafts"), dict) else {}
    keywords = [str(k) for k in (payload.get("keywords") or [])]
    geo = geo_enrich(drafts, keywords, use_llm=bool(payload.get("use_llm", False)))
    return {
        "golden_answer": geo.golden_answer,
        "faq": geo.faq,
        "geo_markdown": geo.geo_markdown,
        "used_llm": geo.used_llm,
        "faq_schema": schema_builder.faq_page(geo.faq),
    }
